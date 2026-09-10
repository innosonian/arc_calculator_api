# [provenance] 원본 hstm_v2 calculators/waveform.py:1-152 전량 동일 이식 (V1 패리티 보존).
#
# 깊이 파형을 압박 이벤트 단위로 분절하는 순수 함수 모듈.
#
# 분절(어디까지가 한 압박인가)은 패킷별 max 파형의 진폭 기반 zigzag로 정하고
# (카운트 count_depth_peaks와 동일 기준), 이벤트 값(peak/recoil)은 원시
# 샘플 단위로 정밀 산출한다. 분절 안정성과 값 정밀도를 분리하기 위함이다.
#
# stdlib 외 의존성이 없어 VCC 계산기 등 다른 코드베이스로 그대로 이식 가능하다.
from dataclasses import dataclass

# 압박 peak 검출용 최소 진폭(raw depth). 깊이가 이 값 이상 상승했다 다시
# 이만큼 하강하면 1회 압박으로 센다. 절대 임계가 아니라 진폭이라, 이완이 불완전해
# 깊이가 baseline까지 안 돌아와도 peak가 병합되지 않는다(이완 정도에 강건).
COMP_PEAK_AMP = 25


@dataclass
class CompressionEvent:
    peak_depth: int  # 정점 깊이(샘플 단위 max)
    recoil_depth: int  # 정점 이후 다음 압박 정점 전까지의 최저 깊이(샘플 단위 min). 잔여 깊이가 낮을수록 이완이 완전하다
    peak_packet_index: int  # 정점이 속한 패킷 인덱스(입력 packets 기준)
    peak_sample_offset: int  # 정점 샘플의 패킷 내 오프셋


def segment_compressions(packets: list[list[int]], min_amp: int = COMP_PEAK_AMP) -> list[CompressionEvent]:
    """깊이 샘플을 패킷(샘플 묶음) 단위로 받아 압박 이벤트 리스트로 분절한다.

    골→마루→골(진폭 min_amp 이상)을 그릴 때마다 1회 압박. 분절은 패킷별 max
    파형으로 판단하고(경계 마커·하강 edge 같은 작은 흔들림은 이벤트가 되지 않음),
    peak/recoil 값은 원시 샘플에서 구한다. len(결과) == count_depth_peaks(패킷별 max)가
    항상 성립한다.
    """
    if not packets:
        return []

    pmax = [max(p) if p else 0 for p in packets]

    # 1) 패킷별 max 파형에서 zigzag로 정점 패킷 인덱스를 확정한다(카운트와 동일 로직).
    peak_packet_indexes = []
    valley = peak = pmax[0]
    peak_pi = 0
    seeking_peak = True
    for pi, d in enumerate(pmax):
        if seeking_peak:
            if d > peak:
                peak = d
                peak_pi = pi
            if peak - d >= min_amp:  # 마루에서 진폭만큼 하강 → 압박 1회 확정
                peak_packet_indexes.append(peak_pi)
                seeking_peak = False
                valley = d
        else:
            valley = min(valley, d)
            if d - valley >= min_amp:  # 골에서 진폭만큼 상승 → 다음 압박 시작
                seeking_peak = True
                peak = d
                peak_pi = pi
    # 마지막 압박: 마루까지 올랐으나 하강이 잘려 미확정인 경우 보정
    if seeking_peak and peak - valley >= min_amp:
        peak_packet_indexes.append(peak_pi)

    # 2) 이벤트 값은 원시 샘플에서 정밀 산출한다.
    samples = []
    packet_starts = []
    for p in packets:
        packet_starts.append(len(samples))
        samples.extend(p)

    events = []
    for k, pi in enumerate(peak_packet_indexes):
        offset = max(range(len(packets[pi])), key=lambda i: packets[pi][i])
        peak_idx = packet_starts[pi] + offset

        # recoil: 이 정점 이후 ~ 다음 정점 전 구간의 최저 잔여 깊이.
        # 정점 이후 관측이 없으면(기록이 정점에서 끝남) 이완 미관측 → 패널티를 주지 않는다.
        is_last_event = k + 1 == len(peak_packet_indexes)
        if not is_last_event:
            next_pi = peak_packet_indexes[k + 1]
            next_offset = max(range(len(packets[next_pi])), key=lambda i: packets[next_pi][i])
            span_end = packet_starts[next_pi] + next_offset
        else:
            span_end = len(samples)
        span = samples[peak_idx + 1 : span_end]
        recoil_depth = min(span) if span else 0
        # 마지막 이벤트: 최저점이 구간 끝 샘플이면(끝까지 하강·유지 중) 이완이 윈도우 밖에서
        # 이어진 것이라(예: 하강 꼬리가 다음 환기 액션으로 잘림) 미관측으로 보고 패널티를 주지 않는다.
        if is_last_event and span and recoil_depth == span[-1]:
            recoil_depth = 0

        events.append(
            CompressionEvent(
                peak_depth=packets[pi][offset],
                recoil_depth=recoil_depth,
                peak_packet_index=pi,
                peak_sample_offset=offset,
            )
        )

    return events


def count_depth_peaks(samples: list[int], min_amp: int = COMP_PEAK_AMP) -> int:
    """깊이 파형(패킷별 max)에서 골→마루→골(진폭 min_amp 이상)을 그릴 때마다 1회 압박으로 센다.

    절대 임계가 아닌 진폭 기반이라 이완이 불완전해 깊이가 baseline까지 안 돌아와도 peak가
    병합되지 않고, 경계 마커·하강 edge 같은 작은 흔들림은 세지 않는다.
    """
    return len(segment_compressions([[s] for s in samples], min_amp))


# 탭(짧은 누름) 필터: 마네킨 카운트와 이벤트 수가 크게 어긋난 구간에서만 동작하는 2차 보정.
# 압박 사이에 짧고 얕은 탭(예: 14mm·50~100ms)을 반복하는 패턴은 진폭 임계를 넘어 이벤트로
# 잡히지만 마네킨 펌웨어는 압박으로 인정하지 않는다(카운트 미증가). 카운트는 트리거(개수
# 비교)에만 쓰고 개별 이벤트 매칭에는 쓰지 않는다. 그래서 카운트 신호의 위상 차, 경계 마커,
# VP 구간의 카운트 부재에 둔감하다.
# production 561건 측정: 정상 압박 폭은 5~8패킷(>=4)에 집중, 탭은 1~2패킷으로 분리된다.
TAP_MAX_WIDTH_PACKETS = 2  # 정점이 속한 연속 구간이 이 길이(1패킷=50ms) 이하면 탭
TAP_TRIGGER_MIN_DIFF = 5  # 이벤트 수가 카운트 증가 수보다 이 값 이상 많을 때만
TAP_TRIGGER_RATIO = 1.3  # 그리고 이 배수 이상일 때만 (둘 다 충족해야 트리거)


def event_run_widths(
    packets: list[list[int]], events: list[CompressionEvent], min_amp: int = COMP_PEAK_AMP
) -> list[int]:
    """각 이벤트 정점이 속한 연속 구간(패킷별 max >= min_amp)의 길이(패킷 수)."""
    pmax = [max(p) if p else 0 for p in packets]
    widths = []
    for event in events:
        lo = hi = event.peak_packet_index
        while lo - 1 >= 0 and pmax[lo - 1] >= min_amp:
            lo -= 1
        while hi + 1 < len(pmax) and pmax[hi + 1] >= min_amp:
            hi += 1
        widths.append(hi - lo + 1)
    return widths


def drop_tap_events(
    packets: list[list[int]], events: list[CompressionEvent], count_increments: int
) -> list[CompressionEvent]:
    """이벤트 수가 마네킨 카운트 증가 수보다 비정상적으로 많을 때만 짧은 폭 이벤트(탭)를 걸러낸다.

    트리거가 안 걸리면 원본 그대로 반환하므로 정상 훈련의 결과는 바뀌지 않는다.
    같은 이유로 사이클당 탭이 트리거 최소치 미만(4개 이하)이면 걸러지지 않는다(알려진 한계).
    전부 걸러지는 이상 케이스에선 원본 유지(분모 0 방지).
    """
    if len(events) < count_increments + TAP_TRIGGER_MIN_DIFF:
        return events
    if len(events) < count_increments * TAP_TRIGGER_RATIO:
        return events
    widths = event_run_widths(packets, events)
    kept = [event for event, width in zip(events, widths) if width > TAP_MAX_WIDTH_PACKETS]
    return kept or events

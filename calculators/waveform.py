# 깊이 파형을 압박 이벤트 단위로 분절하는 순수 함수 모듈.
#
# 분절(어디까지가 한 압박인가)은 패킷별 max 파형의 진폭 기반 zigzag로 정하고
# 이벤트 값(peak/recoil)은 원시
# 샘플 단위로 정밀 산출한다. 분절 안정성과 값 정밀도를 분리하기 위함이다.
#
# stdlib 외 의존성이 없어 VCC 계산기 등 다른 코드베이스로 그대로 이식 가능하다.
from dataclasses import dataclass

# 압박 peak 검출용 최소 진폭(raw depth). 깊이가 이 값 이상 상승했다 다시
# 이만큼 하강하면 1회 압박으로 센다. 절대 임계가 아니라 진폭이라, 이완이 불완전해
# 깊이가 baseline까지 안 돌아와도 peak가 병합되지 않는다(이완 정도에 강건).
COMP_PEAK_AMP = 30


@dataclass
class CompressionEvent:
    peak_depth: int  # 정점 깊이(샘플 단위 max)
    recoil_depth: int  # 정점 이후 다음 압박 정점 전까지의 최저 깊이(샘플 단위 min). 잔여 깊이가 낮을수록 이완이 완전하다
    peak_packet_index: int  # 정점이 속한 패킷 인덱스(입력 packets 기준)
    peak_sample_offset: int  # 정점 샘플의 패킷 내 오프셋


class CompressionSegmenter:
    """DEPTH_V1의 패킷 max 지그재그. 반환값은 이번 Record에서 확정된 정점이다."""

    def __init__(self, min_amp: int = COMP_PEAK_AMP):
        self.min_amp = min_amp
        self.seeking_peak = True
        self.peak = None
        self.peak_pi = None
        self.valley = None
        self.peak_packet_indexes = []

    def feed(self, pi: int, pmax: int) -> int | None:
        if self.peak is None:
            self.valley = self.peak = pmax
            self.peak_pi = pi

        if self.seeking_peak:
            if pmax > self.peak:
                self.peak = pmax
                self.peak_pi = pi
            if self.peak - pmax >= self.min_amp:
                self.peak_packet_indexes.append(self.peak_pi)
                self.seeking_peak = False
                self.valley = pmax
                return self.peak_pi
        else:
            self.valley = min(self.valley, pmax)
            if pmax - self.valley >= self.min_amp:
                self.seeking_peak = True
                self.peak = pmax
                self.peak_pi = pi
        return None

    def finish(self) -> int | None:
        if self.peak is not None and self.seeking_peak and self.peak - self.valley >= self.min_amp:
            self.peak_packet_indexes.append(self.peak_pi)
            self.seeking_peak = False
            return self.peak_pi
        return None


def segment_compressions(packets: list[list[int]], min_amp: int = COMP_PEAK_AMP) -> list[CompressionEvent]:
    """패킷 max로 경계를 확정하고 원시 샘플로 peak/recoil을 계산한다."""
    segmenter = CompressionSegmenter(min_amp)
    for pi, packet in enumerate(packets):
        segmenter.feed(pi, max(packet) if packet else 0)
    segmenter.finish()
    return compression_events(packets, segmenter.peak_packet_indexes)


def compression_events(packets: list[list[int]], peak_packet_indexes: list[int]) -> list[CompressionEvent]:
    """이미 확정된 정점들의 샘플 속성을 구한다. 경계를 다시 검출하지 않는다."""

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

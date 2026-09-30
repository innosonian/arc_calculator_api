"""Q2=B 해시 재등록 근거: event_run_widths / mark_aed_overlapped_rtdata 의 O(n) 계열 재구현이
원래 구현과 결과가 완전히 같음을 고정 seed 무작위 입력으로 단언한다.

기준 함수(_reference_*)는 재구현 직전 원본(provenance 이전 해시 c5b144a2.../3c8f772e...)의
본문을 그대로 복사한 것이다. 기대값을 현재 출력으로 만들지 않는다.

동등성 범위: 바이너리 파서가 만드는 정수 타임스탬프·정수 깊이 값을 전제로 한다. 새 구현은
비교를 미리(이벤트·rtdata 가 없어도) 수행하므로, 서로 비교할 수 없는 값(None·문자열 등)이나 NaN 이
섞인 입력에서는 예외 시점이나 결과가 원본과 다를 수 있다. 현재 파서는 그런 값을 만들지 않는다.
"""

import random
import time
from copy import deepcopy

from calculators.waveform import COMP_PEAK_AMP, CompressionEvent, event_run_widths, segment_compressions
from transformers.part_divider import PartDivider

CASES = 3000


# --- 원본 그대로 복사한 기준 구현 ---------------------------------------------------------

def _reference_event_run_widths(
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


def _reference_mark_aed_overlapped_rtdata(rtdata_list: list[dict], aed_part_list: list[dict]) -> list[dict]:
    aed_part_timestamp_list = [
        (a["aed_part_data"][0]["timestamp"], a["aed_part_data"][-1]["timestamp"]) for a in aed_part_list
    ]

    for rtdata in rtdata_list:
        for a_ts in aed_part_timestamp_list:
            if a_ts[0] < rtdata["timestamp"] < a_ts[1]:
                rtdata["is_aed_overlapped"] = True

        if "is_aed_overlapped" not in rtdata:
            rtdata["is_aed_overlapped"] = False

    return rtdata_list


# --- 결과(또는 예외 종류) 비교 도우미 -----------------------------------------------------

def _outcome(function, *args):
    try:
        return "ok", function(*args)
    except Exception as exc:  # 범위 밖 인덱스 등은 예외 종류까지 같아야 한다.
        return "error", type(exc)


def _event(index):
    return CompressionEvent(peak_depth=0, recoil_depth=0, peak_packet_index=index, peak_sample_offset=0)


def _random_packets(rng, n, min_amp):
    packets = []
    for _ in range(n):
        roll = rng.random()
        if roll < 0.1:
            packets.append([])  # 빈 패킷(pmax 0)
        else:
            size = rng.randint(1, 4)
            # min_amp 경계 근처 값이 많이 나오도록 한다(>=, < 모두).
            packets.append([rng.choice((min_amp - 1, min_amp, min_amp + 1, rng.randint(-5, min_amp * 3)))
                            for _ in range(size)])
    return packets


# --- event_run_widths --------------------------------------------------------------------

def test_event_run_widths_matches_original_on_random_in_range_inputs():
    rng = random.Random(20260928)
    compared = 0
    for case in range(CASES):
        min_amp = COMP_PEAK_AMP if case % 2 else rng.randint(0, 40)
        n = rng.choice((0, 1, 2, 3, rng.randint(4, 60)))
        packets = _random_packets(rng, n, min_amp)
        indexes = [rng.randrange(n) for _ in range(rng.randint(0, 8))] if n else []
        if n:
            indexes += [0, n - 1]  # 경계 인덱스
        rng.shuffle(indexes)
        events = [_event(i) for i in indexes]
        args = (packets, events) if case % 2 else (packets, events, min_amp)
        expected = _reference_event_run_widths(*args)
        assert event_run_widths(*deepcopy(args)) == expected, (packets, indexes, min_amp)
        compared += len(expected)
    assert compared > CASES  # 빈 events 외에도 실제 폭을 충분히 비교했다.


def test_event_run_widths_matches_original_on_out_of_range_indexes():
    rng = random.Random(1_2026_0928)
    for _ in range(CASES):
        min_amp = rng.randint(0, 40)
        n = rng.randint(0, 12)
        packets = _random_packets(rng, n, min_amp)
        events = [_event(rng.randint(-n - 3, n + 3)) for _ in range(rng.randint(0, 4))]
        expected = _outcome(_reference_event_run_widths, packets, events, min_amp)
        assert _outcome(event_run_widths, packets, events, min_amp) == expected, (packets, events, min_amp)


def test_event_run_widths_matches_original_on_segmented_waveforms():
    rng = random.Random(2_2026_0928)
    for _ in range(CASES):
        packets = [[rng.randint(0, 90) for _ in range(rng.randint(0, 3))] for _ in range(rng.randint(0, 40))]
        events = segment_compressions(packets)
        assert event_run_widths(packets, events) == _reference_event_run_widths(packets, events)


def test_event_run_widths_empty_inputs():
    assert event_run_widths([], []) == _reference_event_run_widths([], []) == []
    assert event_run_widths([[30]], []) == []
    assert event_run_widths([], [_event(0)]) == _reference_event_run_widths([], [_event(0)]) == [1]


def test_event_run_widths_long_single_run_is_linear():
    # 5만 패킷이 끊김 없이 min_amp 이상(60/30 교대)인 적대 입력: 원래 구현은 이벤트마다 구간 전체를 걷는다.
    n = 50_000
    packets = [[60] * 10 if i % 2 else [30] * 10 for i in range(n)]
    events = segment_compressions(packets)
    assert len(events) > 20_000
    started = time.perf_counter()
    widths = event_run_widths(packets, events)
    elapsed = time.perf_counter() - started
    assert widths == [n] * len(events)
    assert elapsed < 10.0, elapsed  # 측정값은 수십 ms 수준. CPU 경합을 고려해 넉넉하게 둔다.


# --- mark_aed_overlapped_rtdata ----------------------------------------------------------

def _random_rtdata(rng, count, span):
    rows = []
    for _ in range(count):
        row = {"timestamp": rng.randint(0, span), "packet": rng.randint(0, 9)}
        roll = rng.random()
        if roll < 0.15:
            row["is_aed_overlapped"] = True  # 기존 키 보존 여부 확인
        elif roll < 0.3:
            row["is_aed_overlapped"] = False
        rows.append(row)
    return rows


def _random_parts(rng, count, span):
    parts = []
    for _ in range(count):
        start = rng.randint(0, span)
        shape = rng.random()
        if shape < 0.15:
            end = start  # 길이 0
        elif shape < 0.3:
            end = start - rng.randint(1, max(1, span // 4))  # 음수 길이
        else:
            end = start + rng.randint(1, max(1, span // 3))
        if rng.random() < 0.1:
            parts.append({"aed_part_data": [{"timestamp": start}]})  # 한 원소 파트(시작=끝)
            continue
        middle = [{"timestamp": rng.randint(0, span)} for _ in range(rng.randint(0, 2))]
        parts.append({"aed_part_data": [{"timestamp": start}] + middle + [{"timestamp": end}]})
    return parts


def test_mark_aed_overlapped_rtdata_matches_original_on_random_inputs():
    rng = random.Random(3_2026_0928)
    divider = PartDivider()
    overlapped = 0
    for _ in range(CASES):
        span = rng.choice((5, 20, 200))  # 작은 범위는 같은 타임스탬프·경계 일치를 자주 만든다.
        rtdata = _random_rtdata(rng, rng.randint(0, 30), span)
        parts = _random_parts(rng, rng.choice((0, 1, 2, rng.randint(3, 12))), span)
        expected_rows = deepcopy(rtdata)
        expected = _reference_mark_aed_overlapped_rtdata(expected_rows, deepcopy(parts))
        actual = divider.mark_aed_overlapped_rtdata(rtdata, parts)
        assert actual is rtdata  # 제자리 변경 후 같은 리스트 반환
        assert actual == expected, (rtdata, parts)
        assert [list(row) for row in actual] == [list(row) for row in expected]  # 키 순서까지 동일
        overlapped += sum(1 for row in actual if row["is_aed_overlapped"])
    assert overlapped > CASES  # True 로 덮어쓰는 경로를 충분히 거쳤다.


def test_mark_aed_overlapped_rtdata_strict_bounds_and_no_parts():
    divider = PartDivider()
    rows = [{"timestamp": t} for t in (10, 11, 20, 21)]
    parts = [{"aed_part_data": [{"timestamp": 10}, {"timestamp": 20}]}]
    assert [row["is_aed_overlapped"] for row in divider.mark_aed_overlapped_rtdata(rows, parts)] == [
        False, True, False, False]
    # 파트가 없으면 timestamp 를 읽지 않는다(원본과 동일).
    rows = [{"no_timestamp": 1}, {"timestamp": 3, "is_aed_overlapped": True}]
    assert _reference_mark_aed_overlapped_rtdata(deepcopy(rows), []) == divider.mark_aed_overlapped_rtdata(rows, [])
    assert rows == [{"no_timestamp": 1, "is_aed_overlapped": False}, {"timestamp": 3, "is_aed_overlapped": True}]
    # 퇴화 파트만 있어도 timestamp 를 읽는다(원본의 KeyError 유지).
    degenerate = [{"aed_part_data": [{"timestamp": 5}]}]
    assert _outcome(_reference_mark_aed_overlapped_rtdata, [{}], degenerate) == ("error", KeyError)
    assert _outcome(divider.mark_aed_overlapped_rtdata, [{}], degenerate) == ("error", KeyError)


def test_mark_aed_overlapped_rtdata_large_input_is_fast():
    rows = [{"timestamp": 1_000_000 + i * 50} for i in range(40_000)]
    parts = [{"aed_part_data": [{"timestamp": 1_000_000 + j * 100}, {"timestamp": 1_000_000 + j * 100 + 60}]}
             for j in range(20_000)]
    started = time.perf_counter()
    result = PartDivider().mark_aed_overlapped_rtdata(rows, parts)
    elapsed = time.perf_counter() - started
    # 짝수 i 는 파트 시작과 같아 제외(엄격), 홀수 i 는 시작+50 으로 파트 안.
    assert [row["is_aed_overlapped"] for row in result] == [bool(i % 2) for i in range(40_000)]
    assert elapsed < 10.0, elapsed

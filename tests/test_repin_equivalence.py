"""Strict AED interval boundaries and in-place marks of the restored HSTM loop."""

import random
from copy import deepcopy
from transformers.part_divider import PartDivider

CASES = 3000

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

def _outcome(function, *args):
    try:
        return "ok", function(*args)
    except Exception as exc:  # 범위 밖 인덱스 등은 예외 종류까지 같아야 한다.
        return "error", type(exc)

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
    divider = PartDivider(None)
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
    assert overlapped > CASES

def test_mark_aed_overlapped_rtdata_strict_bounds_and_no_parts():
    divider = PartDivider(None)
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

# Recorded inputs and historical goldens remain unchanged. D38-D46 and D138
# expectations are derived independently; no current output is copied into a
# golden file. Each case runs under the current rules (direct call; D138
# end-of-file breath, D139 no minimum-quantity null) and under the retained
# adapters' options (v4/pending-v3: D42 end of file, D07/D08 nulls).
import json
import os

import pytest

from main import run_calculator
from scripts.verify_reference_parity import _differences
from tests.calculation_options_support import RETAINED_OPTIONS, run_with_options
from tests.detection_oracle import run_expected

_DATASET_DIR = os.path.join(os.path.dirname(__file__), "dataset")

# (골든 이름, cpr bin, aed bin(None이면 b""), target, training_type)
_CASES = [
    ("result_1", "cpr_1.bin", "aed_1.bin", "adult", "cpr"),
    ("result_2", "cpr_2.bin", "aed_2.bin", "adult", "cpr"),
    ("result_3", "cpr_3.bin", "aed_3.bin", "adult", "cpr"),
    ("result_4", "cpr_4.bin", "aed_4.bin", "adult", "cpr"),
    ("result_5", "cpr_5.bin", "aed_5.bin", "adult", "cpr"),
    ("result_cco_1", "cco_1.bin", None, "adult", "compression_only"),
    ("result_vo_1", "vo_1.bin", None, "infant", "ventilation_only"),
    # R3-7: 성인 vent-only 골든 추가(스펙 §7.1)
    ("result_adult_vo_1", "adult_vo_1.bin", None, "adult", "ventilation_only"),
]


def _condition(target, training_type):
    return {
        "mode": "training",
        "target": target,
        "training_type": training_type,
        "guideline": "ARC2025",
        "cpr_cycle_type": "302",
        "is_2rescuers": False,
    }


def _read(path):
    with open(path, "rb") as file:
        return file.read()


def _run_case(cpr_bin, aed_bin, target, training_type, options=None):
    cpr_byte_data = _read(os.path.join(_DATASET_DIR, cpr_bin))
    aed_byte_data = _read(os.path.join(_DATASET_DIR, aed_bin)) if aed_bin else b""
    if options is None:
        result = run_calculator(cpr_byte_data, aed_byte_data, _condition(target, training_type), stage="test")
    else:
        result = run_with_options(cpr_byte_data, aed_byte_data, _condition(target, training_type), options)[0]
    # 골든은 JSON으로 저장되므로 같은 표현으로 정규화해 비교한다(응답 직렬화와 동일).
    return json.loads(json.dumps(result))


@pytest.mark.parametrize("options", [None, RETAINED_OPTIONS], ids=["current", "retained"])
@pytest.mark.parametrize("golden_name, cpr_bin, aed_bin, target, training_type", _CASES)
def test_result_golden(golden_name, cpr_bin, aed_bin, target, training_type, options):
    golden_path = os.path.join(_DATASET_DIR, f"{golden_name}.json")
    # 검토 반영 2026-09-05: P15 골든 부재는 skip 이 아니라 실패다.
    assert os.path.exists(golden_path), f"golden missing: {golden_path}"

    with open(golden_path, "rb") as file:
        historical = json.loads(file.read())

    expect, _chart = run_expected(_read(os.path.join(_DATASET_DIR, cpr_bin)),
                                 _read(os.path.join(_DATASET_DIR, aed_bin)) if aed_bin else b"",
                                 _condition(target, training_type), options=options)
    expect = json.loads(json.dumps(expect))

    result = _run_case(cpr_bin, aed_bin, target, training_type, options)

    # Match the complete JSON tree, retaining int/float/bool/null distinctions.
    differences = _differences(expect, result)
    assert not differences, (golden_name, differences[:10])
    if not _differences(historical, expect):
        assert not _differences(historical, result)


def test_result_count_values():
    # 보존 옵션(v4·pending-v3): cpr_1에서 comp_no==comp_count, vent_rate==vent_count.
    # 최소량 null 정책이 적용되는 필드도 레거시 별칭과 같은 값(null)을 유지한다.
    result = _run_case("cpr_1.bin", "aed_1.bin", "adult", "cpr", RETAINED_OPTIONS)
    total = result["cpr_score"]["total_score"]
    assert total["score_comp_no"] == total["score_comp_count"]
    assert total["score_vent_rate"] == total["score_vent_count"]
    assert total["score_comp_no"] is None and total["score_vent_rate"] is None and total["overall"] is None


def test_result_count_values_without_the_minimum_null():
    # D139: 현재 규칙에서 cpr_1(압박 48·호흡 2)은 점수가 계산된다. comp_no는 comp_count의 별칭 그대로다.
    # score_vent_rate 0은 D45로 바뀐 호흡률 측정 구간의 값이다(독립 기대값과 일치: test_result_golden).
    # 참고 구현의 기록값은 100이며, D139 이전에는 이 차이가 null에 가려져 있었다.
    result = _run_case("cpr_1.bin", "aed_1.bin", "adult", "cpr")
    total = result["cpr_score"]["total_score"]
    assert result["action_count"] == {"comp": 48, "vent": 2}
    assert total["score_comp_no"] == total["score_comp_count"] == 100
    assert (total["score_vent_vol"], total["score_vent_count"], total["score_vent_rate"]) == (100, 100, 0)
    assert total["overall"] == 87


def test_result_count_values_non_null_session():
    # 성인 최소량을 충족하면서 기존 6호흡/사이클 fixture의 비교 조건을 유지한다.
    from tests._synth import cpr_session

    result = run_calculator(
        cpr_session([(30, 6), (30, 6), (30, 6)]), b"", _condition("adult", "cpr"), stage="test"
    )
    total = result["cpr_score"]["total_score"]
    assert total["score_comp_no"] is not None
    assert total["score_comp_no"] == total["score_comp_count"]
    assert total["score_vent_rate"] is not None
    assert total["score_vent_rate"] == total["score_vent_count"]

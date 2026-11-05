import json

from main import run_calculator
from services.http.schemas import ConditionType
from tests.depth_boundary_reference import RATE_SCORES


def test_result():
    compare_cpr_score_keys = (
        "score_comp_depth",
        "score_comp_rate",
        "score_comp_no",
        "score_comp_count",
        "score_recoil",
        "score_hand_position",
        "score_vent_vol",
        "score_vent_count",
        "score_vent_speed",
        "score_ccf",
        "overall",
        "judg_result",
    )
    condition = ConditionType(
        mode="training",
        target="adult",
        training_type="cpr",
        guideline="AHA2020",
        cpr_cycle_type="302",
        is_2rescuers=False,
    )

    for i in range(1, 6):
        cpr_path = f"tests/dataset/cpr_{i}.bin"
        aedt_path = f"tests/dataset/aed_{i}.bin"
        expect_result_path = f"tests/dataset/result_{i}.json"

        with open(cpr_path, "rb") as file:
            cpr_byte_data = file.read()
        with open(aedt_path, "rb") as file:
            aedt_byte_data = file.read()
        with open(expect_result_path, "rb") as file:
            expect_result = json.loads(file.read())

        result = run_calculator(
            cpr_byte_data,
            aedt_byte_data,
            condition,
            stage="test",
        )

        for k in compare_cpr_score_keys:
            # DEPTH_V1의 정점 간격 속도: cpr_1 93→92, cpr_4 100→97, cpr_5 100→98.
            # test_compute가 독립 raw 참조·기존 배점표로 이 값을 재계산한다. 나머지는 역사 JSON 그대로다.
            expected = RATE_SCORES[f"cpr_{i}"] if k == "score_comp_rate" else expect_result["cpr_score"]["total_score"][k]
            assert result["cpr_score"]["total_score"][k] == expected

        assert result["aed_score"]["overall"] == expect_result["aed_score"]["overall"]


def test_result_count_values():
    condition = ConditionType(
        mode="training",
        target="adult",
        training_type="cpr",
        guideline="AHA2020",
        cpr_cycle_type="302",
        is_2rescuers=False,
    )

    cpr_path = f"tests/dataset/cpr_1.bin"
    aedt_path = f"tests/dataset/aed_1.bin"
    with open(cpr_path, "rb") as file:
        cpr_byte_data = file.read()
    with open(aedt_path, "rb") as file:
        aedt_byte_data = file.read()

    result = run_calculator(
        cpr_byte_data,
        aedt_byte_data,
        condition,
        stage="test",
    )

    assert result["cpr_score"]["total_score"]["score_comp_no"] == result["cpr_score"]["total_score"]["score_comp_count"]
    assert (
        result["cpr_score"]["total_score"]["score_vent_rate"] == result["cpr_score"]["total_score"]["score_vent_count"]
    )


def test_result_comp_only():
    compare_cpr_score_keys = (
        "score_comp_depth",
        "score_comp_rate",
        "score_comp_no",
        "score_comp_count",
        "score_recoil",
        "score_hand_position",
        "score_vent_vol",
        "score_vent_count",
        "score_vent_speed",
        "score_ccf",
        "overall",
        "judg_result",
    )
    condition = ConditionType(
        mode="training",
        target="adult",
        training_type="compression_only",
        guideline="ERC2020",
        cpr_cycle_type="302",
        is_2rescuers=False,
    )

    cpr_path = "tests/dataset/cco_1.bin"
    expect_result_path = "tests/dataset/result_cco_1.json"
    with open(cpr_path, "rb") as file:
        cpr_byte_data = file.read()
    with open(expect_result_path, "rb") as file:
        expect_result = json.loads(file.read())

    result = run_calculator(
        cpr_byte_data,
        b"",
        condition,
        stage="test",
    )

    for k in compare_cpr_score_keys:
        assert result["cpr_score"]["total_score"][k] == expect_result["cpr_score"]["total_score"][k]


def test_result_vent_only():
    compare_cpr_score_keys = (
        "score_comp_depth",
        "score_comp_rate",
        "score_comp_no",
        "score_comp_count",
        "score_recoil",
        "score_hand_position",
        "score_vent_vol",
        "score_vent_count",
        "score_vent_speed",
        "score_ccf",
        "overall",
        "judg_result",
    )
    condition = ConditionType(
        mode="training",
        target="infant",
        training_type="ventilation_only",
        guideline="ERC2020",
        cpr_cycle_type="302",
        is_2rescuers=False,
    )

    cpr_path = "tests/dataset/vo_1.bin"
    expect_result_path = "tests/dataset/result_vo_1.json"
    with open(cpr_path, "rb") as file:
        cpr_byte_data = file.read()
    with open(expect_result_path, "rb") as file:
        expect_result = json.loads(file.read())

    result = run_calculator(
        cpr_byte_data,
        b"",
        condition,
        stage="test",
    )

    for k in compare_cpr_score_keys:
        assert result["cpr_score"]["total_score"][k] == expect_result["cpr_score"]["total_score"][k]

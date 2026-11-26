"""HP_V2 손 위치 판정(D142~D144, 2026-11-26 사용자 결정 U1~U3).

기준은 앱 HandPositionReleaseTracker.observe·evaluatePosition이다. 표본 창 W는 기존 DEPTH_V1r3
창(ActionDataPrepare._own_hand_position)이며 창 재구성은 test_hand_position_attribution.py가 검증한다.
아래 기대값은 사용자 결정 표를 손으로 옮긴 값이며 구현에서 파생하지 않는다.
"""

import json

import pytest

from calculators import hand_position as hp
from calculators.action_evaluator import ActionEvaluator
from config.constants import ACTION_TYPE_COMP
from main import run_calculator
from services.config import Config
from tests._synth import packet


ADULT_CCO = {"mode": "training", "target": "adult", "training_type": "compression_only", "guideline": "ARC2025",
             "cpr_cycle_type": "302", "is_2rescuers": False}
INFANT_CCO = {**ADULT_CCO, "target": "infant"}
BOARDS = ("unknown", "nordic", "esp32c6")
R = list(range(10, 20))  # Record numbers of a window; the peak is Record 11 unless stated.


@pytest.mark.parametrize("value,expected", [
    (None, ("unknown", True)), ("unknown", ("unknown", True)), ("nordic", ("nordic", True)),
    ("esp32c6", ("esp32c6", True)), ("Nordic", ("unknown", False)), ("", ("unknown", False)),
    ("esp32", ("unknown", False)), (1, ("unknown", False)), (["nordic"], ("unknown", False)),
])
def test_board_normalization(value, expected):
    assert hp.normalize_board(value) == expected


@pytest.mark.parametrize("window,peak,board,infant,expected", [
    # Unknown board, adult: the first code that is not 0/1; none means center.
    ([1, 1, 1, 8], 11, "unknown", False, 8),
    ([1, 4, 8, 8], 11, "unknown", False, 4),
    ([1, 1, 2, 4], 11, "unknown", False, 2),
    ([1, 16, 1], 11, "unknown", False, 16),
    ([1, 1, 1], 11, "unknown", False, 1),
    ([], 11, "unknown", False, 1),
    # Unsupported first code (user 2026-11-26): no sample, like the app's absent marker.
    ([1, 3, 4], 11, "unknown", False, None),
    ([32, 1], 11, "unknown", False, None),
    # Raw 10 is the app's error marker ("Check position"): direction-unknown, still scored (F1).
    ([10, 1], 11, "unknown", False, 10),
    ([1, 4, 3], 11, "unknown", False, 4),
    # Infant on every board: masked with 0x0F, then the legacy rule; any wrong code is 10.
    *[(window, 11, board, True, expected) for board in BOARDS for window, expected in (
        ([16, 17, 17], 1), ([17, 18, 17], 10), ([0x44, 0x44], 10), ([16, 16], 1), ([17, 0x13], 10))],
    # nordic: 2 anywhere in W wins; otherwise only release Records (after the peak) count.
    ([2, 1, 1, 1], 11, "nordic", False, 2),
    ([4, 4, 1, 1], 11, "nordic", False, 1),          # side codes only while pressing
    ([1, 1, 8, 8], 11, "nordic", False, 8),
    ([1, 1], 11, "nordic", False, None),             # no release sample
    ([1, 1, 1, 4, 8], 11, "nordic", False, 10),      # tie 4/8 in the release
    ([1, 1, 1, 8, 1, 1, 1], 11, "nordic", False, 1),   # release [1, 8, 1, 1, 1]: 1 x 4 < 5, center
    ([3, 1, 1, 1], 11, "nordic", False, 1),          # an unsupported code while pressing is not sampled
    ([1, 1, 3, 1], 11, "nordic", False, 10),         # an unsupported release code
    # esp32c6: every W sample counts.
    ([2, 1, 1, 1], 11, "esp32c6", False, 2),
    ([4, 4, 1, 1], 11, "esp32c6", False, 4),
    ([8, 1, 1, 1], 11, "esp32c6", False, 8),         # exactly 25%: a direction
    ([8, 1, 1, 1, 1], 11, "esp32c6", False, 1),      # below 25%: center
    ([4, 8, 1, 1], 11, "esp32c6", False, 10),        # tie
    ([4, 4, 8, 1], 11, "esp32c6", False, 4),         # majority
    ([16, 16, 1], 11, "esp32c6", False, 16),
    ([16, 4, 1, 1], 11, "esp32c6", False, 10),       # tie between up and left
    ([1, 3, 1], 11, "esp32c6", False, 10),
    ([0, 0], 11, "esp32c6", False, None),            # all zero
    ([], 11, "esp32c6", False, None),
])
def test_classify_follows_the_app_rule(window, peak, board, infant, expected):
    assert hp.classify(window, R[:len(window)], peak, board, infant) == expected


# The case table shared with the app repository (2026-11-26 review F1), copied as given.
# W is the hp_window; an infant window is masked with 0x0F right before the judgement.
SHARED_CASES = [
    ("unknown", "adult", [1, 10, 1], "unknown"),
    ("unknown", "adult", [1, 3, 1], None),
    ("unknown", "adult", [1, 32, 4], None),        # the first wrong code decides
    ("unknown", "adult", [1, 4, 3], "left"),
    ("unknown", "adult", [1, 4, 2], "left"),       # the first code even before an abdomen code
    *[(board, "infant", [0x11, 0x13], "unknown") for board in BOARDS],
    *[(board, "infant", [0x10, 0x11], "center") for board in BOARDS],
    ("esp32c6", "adult", [1, 1, 1, 4], "left"),    # exactly 25%
    ("esp32c6", "adult", [1, 1, 1, 1, 4], "center"),   # 20%
    ("esp32c6", "adult", [4, 8], "unknown"),       # tie
    ("esp32c6", "adult", [1, 3], "unknown"),       # unsupported code
    ("esp32c6", "adult", [4, 4, 2], "down"),
]


@pytest.mark.parametrize("board,target,window,expected", SHARED_CASES)
def test_shared_app_case_table(board, target, window, expected):
    code = hp.classify(window, R[:len(window)], 11, board, target == "infant")
    assert hp.VALUES.get(code) == expected
    score = _score(window, R[:len(window)], 11, board=board, target=target)
    assert score.get("hand_position", {}).get("value") == expected


def test_shared_app_cases_for_nordic_release_samples():
    # No release sample: every Record is at or before the peak Record.
    assert hp.classify([1, 1, 4], [10, 11, 12], 12, "nordic", False) is None
    # Pressing [4, 4] (Records 10-11, peak 11), release [1, 1] (Records 12-13): only the release counts.
    assert hp.classify([4, 4, 1, 1], [10, 11, 12, 13], 11, "nordic", False) == 1
    assert _score([4, 4, 1, 1], [10, 11, 12, 13], 11, board="nordic")["hand_position"]["value"] == "center"


def test_raw_ten_on_an_unknown_board_is_scored_unknown_and_not_logged(capsys):
    assert _score([1, 10, 1], R[:3], 11)["hand_position"] == {"grade": 20, "criterion": "side", "value": "unknown"}
    result, values = _run([(1, 10, 1, 1)] * 60, "unknown")
    assert set(values) == {"unknown"}
    assert result["cpr_score"]["total_score"]["score_hand_position"] == 20
    assert result["metrics"]["HandPosition"]["n_IncorrectLR"] == 60
    assert "hand_position_code_unsupported" not in capsys.readouterr().out


def test_nordic_reads_release_records_by_number_not_position():
    # The same codes judged with the peak at the last sample have no release sample.
    assert hp.classify([1, 1, 8, 8], [10, 11, 12, 13], 11, "nordic", False) == 8
    assert hp.classify([1, 1, 8, 8], [10, 11, 12, 13], 13, "nordic", False) is None
    assert hp.classify([8, 1, 1, 1], [10, 11, 12, 13], 9, "nordic", False) == 8   # 25% of the release


def test_unsupported_legacy_flag_matches_the_none_result():
    for window in ([1, 3, 4], [32], [1, 32, 4]):
        assert hp.is_unsupported_legacy(window, "unknown", False) is True
        assert hp.classify(window, R[:len(window)], 11, "unknown", False) is None
    for window, board, infant in (([1, 4, 3], "unknown", False), ([1, 3], "unknown", True),
                                  ([1, 3], "nordic", False), ([1, 1], "unknown", False),
                                  ([1, 10, 1], "unknown", False)):
        assert hp.is_unsupported_legacy(window, board, infant) is False


def _score(window, records, peak, *, board="unknown", target="adult"):
    config = Config({**ADULT_CCO, "target": target})
    config.hand_position_board = board
    action = {"action_type": ACTION_TYPE_COMP, "compression_depth": [100], "depth_raw": 100, "recoil_raw": 0,
              "peak_record": peak, "peak_offset": 0, "prev_peak_sample": None,
              "hand_position": window, "hand_position_records": records}
    return ActionEvaluator(config)._evaluate_comp(action, config.border, "RP").score


@pytest.mark.parametrize("window,board,target,expected", [
    ([1, 1], "unknown", "adult", {"grade": 100, "criterion": "good", "value": "center"}),
    ([1, 4], "unknown", "adult", {"grade": 20, "criterion": "side", "value": "left"}),
    ([1, 8], "unknown", "adult", {"grade": 20, "criterion": "side", "value": "right"}),
    ([1, 16], "unknown", "adult", {"grade": 20, "criterion": "side", "value": "up"}),
    ([4, 8, 1, 1], "esp32c6", "adult", {"grade": 20, "criterion": "side", "value": "unknown"}),
    ([1, 2], "unknown", "adult", {"grade": 0, "criterion": "down", "value": "down"}),
    # Behaviour change: an infant abdomen code is now direction-unknown (20), not down (0).
    ([17, 18], "unknown", "infant", {"grade": 20, "criterion": "side", "value": "unknown"}),
])
def test_evaluator_scores_each_value_with_the_existing_border_rows(window, board, target, expected):
    score = _score(window, R[:len(window)], 11, board=board, target=target)
    assert list(score) == ["comp_depth", "comp_rate", "recoil", "hand_position"]
    assert score["hand_position"] == expected


def test_evaluator_omits_hand_position_without_a_sample():
    assert list(_score([1, 1], [10, 11], 11, board="nordic")) == ["comp_depth", "comp_rate", "recoil"]
    assert "hand_position" not in _score([1, 3], [10, 11], 11)


def test_config_without_a_board_is_unknown():
    config = Config(ADULT_CCO)
    action = {"action_type": ACTION_TYPE_COMP, "compression_depth": [100], "depth_raw": 100, "recoil_raw": 0,
              "peak_record": 11, "peak_offset": 0, "prev_peak_sample": None, "hand_position": [4, 4, 1, 1]}
    assert ActionEvaluator(config)._evaluate_comp(action, config.border, "RP").score["hand_position"]["value"] == "left"


# --- whole calculator --------------------------------------------------------------------------
# One compression per 10 Records: depth 0,60,100,60,20,0.. raw. Its window is codes c1,c2 (Records
# 1-2, peak at 2) plus the release c3,c4 (Records 3-4) of the next buffer.
_DEPTHS = (0, 60, 100, 60, 20, 0, 0, 0, 0, 0)
PRESS_SIDE, RELEASE_SIDE, CENTER, NO_RELEASE, DOWN = (4, 4, 1, 1), (1, 1, 8, 8), (1, 1, 1, 1), (1, 1, 0, 0), (2, 1, 1, 1)


def _session(units):
    packets, timestamp = [packet(timestamp=0, hand=0)], 50
    for codes in units:
        for depth, code in zip(_DEPTHS, (0, *codes, 0, 0, 0, 0, 0)):
            packets.append(packet(timestamp=timestamp, depth=(depth,) * 10, hand=code))
            timestamp += 50
    return b"".join(packets)


def _run(units, board, condition=ADULT_CCO):
    result = run_calculator(_session(units), b"", dict(condition), [], stage="test", hand_position_board=board)
    actions = [action for part in result["cpr_score"]["part_scores"] for action in part["action_with_score_list"]]
    return result, [action.get("hand_position", {}).get("value") for action in actions]


@pytest.mark.parametrize("units,expected", [
    (PRESS_SIDE, {"unknown": ("left", 20), "nordic": ("center", 100), "esp32c6": ("left", 20)}),
    (RELEASE_SIDE, {"unknown": ("right", 20), "nordic": ("right", 20), "esp32c6": ("right", 20)}),
    (DOWN, {"unknown": ("down", 0), "nordic": ("down", 0), "esp32c6": ("down", 0)}),
    (NO_RELEASE, {"unknown": ("center", 100), "nordic": (None, 100), "esp32c6": ("center", 100)}),
])
def test_boards_judge_the_same_recording_by_their_own_rule(units, expected):
    for board, (value, cycle_score) in expected.items():
        result, values = _run([units] * 60, board)
        assert result["action_count"]["comp"] == 60 and set(values) == {value}
        assert result["cpr_score"]["total_score"]["score_hand_position"] == cycle_score


def test_cycle_average_and_metrics_skip_compressions_without_a_sample():
    units = [RELEASE_SIDE] * 20 + [CENTER] * 20 + [NO_RELEASE] * 20
    result, values = _run(units, "nordic")
    assert values.count(None) == 20
    # (20 x 20 + 20 x 100) / 40, not / 60.
    assert result["cpr_score"]["total_score"]["score_hand_position"] == 60
    metric = result["metrics"]["HandPosition"]
    assert (metric["n_IncorrectLR"], metric["n_Good"], metric["n_IncorrectStomach"], metric["%_Good"]) == (20, 20, 0, 50)
    result, _ = _run(units, "unknown")
    assert result["cpr_score"]["total_score"]["score_hand_position"] == 73
    assert result["metrics"]["HandPosition"]["n_Good"] == 40


def test_a_cycle_whose_compressions_all_lack_a_sample_keeps_100():
    result, values = _run([NO_RELEASE] * 60, "nordic")
    assert set(values) == {None}
    assert result["cpr_score"]["total_score"]["score_hand_position"] == 100
    metric = result["metrics"]["HandPosition"]
    assert (metric["n_IncorrectLR"], metric["n_Good"], metric["n_IncorrectStomach"]) == (0, 0, 0)


def test_infant_abdomen_and_side_codes_count_as_incorrect_lr():
    result, values = _run([(17, 17, 18, 17)] * 60, "nordic", INFANT_CCO)
    assert set(values) == {"unknown"}
    assert result["cpr_score"]["total_score"]["score_hand_position"] == 20
    assert result["metrics"]["HandPosition"]["n_IncorrectLR"] == 60
    assert result["metrics"]["HandPosition"]["n_IncorrectStomach"] == 0


def test_unsupported_first_code_is_not_scored_and_logged_once(capsys):
    result, values = _run([(1, 3, 1, 1)] * 30 + [CENTER] * 30, "unknown")
    assert values.count(None) == 30 and values.count("center") == 30
    assert result["cpr_score"]["total_score"]["score_hand_position"] == 100
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    logged = [line for line in lines if line["message"] == "hand_position_code_unsupported"]
    assert logged == [{"level": "info", "message": "hand_position_code_unsupported"}]
    _run([CENTER] * 60, "unknown")
    assert "hand_position_code_unsupported" not in capsys.readouterr().out

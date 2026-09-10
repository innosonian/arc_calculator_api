"""Approved document exception: explicit null remains null and outranks prior Pass."""

from copy import deepcopy
import json

import pytest

from services import legacy_document as document


def _result(overall=None):
    return {
        "cpr_score": {
            "total_score": {"overall": overall, "score_comp_depth": None,
                            "score_hand_position": None, "score_vent_speed": None},
            "part_scores": [{"cycle_with_score_list": [
                {"overall": None, "score_comp_depth": None, "score_vent_speed": None},
                {"overall": 0, "score_comp_depth": 0, "score_vent_speed": 0},
                {"overall": 90, "score_comp_depth": 90},
            ]}],
        },
        "metrics": {"AvgCompressionDepth": 52.5, "AvgVentilationVolume": 500,
                    "VentilationSpeed": None},
        "action_count": {"comp": 89, "vent": 5},
        "training_stats": {"cycle_count": 3, "elapsed_seconds": 60},
    }


@pytest.mark.parametrize("mode", ["training", "assessment"])
@pytest.mark.parametrize("usage", ["Assessment", "CPR Training"])
@pytest.mark.parametrize("passing_score", [None, "0", "80", "inf"])
def test_null_total_clears_prior_pass_and_never_issues_certification(mode, usage, passing_score, monkeypatch):
    monkeypatch.setenv("HSTM_V2_WITHHOLD_SCORE", "1")
    original = {
        "ResultSummary": {"JudgResult": "Pass", "hStreamResult": "Pass",
                          "hStreamScore": 100, "hStreamReason": "Well Done! You passed!"},
        "Custom": {"CertificateAdult": True, "PassThreshold": 0},
        "Open_Skill": {"Passing_Score": passing_score},
        "Usage": {"Type": usage, "validNum": 123},
        "Certification": {"Target": "adult"},
        "ResultByCriteria": {"VentilationSpeed": {"%_Good": 100}},
    }
    snapshot = deepcopy(original)
    result = document._apply_calculated_fields(
        original, _result(), {"mode": mode, "target": "adult", "guideline": "ARC2025", "training_type": "cpr"},
    )
    assert result["ResultSummary"]["JudgResult"] == "Fail"
    assert result["ResultSummary"]["hStreamResult"] == "Fail"
    assert result["ResultSummary"]["hStreamScore"] is None
    assert "hStreamReason" not in result["ResultSummary"]
    assert result["Certification"] == {"Target": "N/A"}
    assert result["ResultSummary"]["Handposition"] is None
    assert result["ResultSummary"]["CompressionDepth"] == 52.5
    assert result["ResultByCycle"]["ScoreByCycle"] == {"ByCycle": [None, 0, 90], "Overall": None}
    assert result["ResultByCycle"]["CompressionDepth"] == {"ByCycle": [None, 0, 90], "Overall": None}
    assert result["ResultByCycle"]["VentilationSpeed"] == {"ByCycle": [None, 0, 0], "Overall": None}
    assert result["ResultByCriteria"]["VentilationSpeed"] is None
    assert json.loads(json.dumps(result)) == result
    assert original == snapshot


def test_null_total_outranks_summary_pass_in_direct_certification_call():
    assert document._build_certification(
        _result(), {"target": "adult"}, {"CertificateAdult": True}, {"Passing_Score": "0"},
        {"JudgResult": "Pass", "hStreamResult": "Pass"},
    ) == {"Target": "N/A"}


def test_partial_null_keeps_remaining_score_and_reference_pass_rules():
    result = _result(90)
    result["action_count"]["vent"] = 6
    built = document._apply_calculated_fields({
        "Custom": {"CertificateAdult": True}, "Open_Skill": {"Passing_Score": 80},
        "Usage": {"Type": "CPR Training", "validNum": 123},
    }, result, {"target": "adult", "training_type": "cpr", "guideline": "ARC2025"})
    assert built["ResultSummary"]["hStreamScore"] == 90
    assert built["ResultSummary"]["hStreamResult"] == "Pass"
    assert built["Certification"] == {"Target": "adult"}
    assert built["ResultByCycle"]["CompressionDepth"]["Overall"] is None


def test_zero_score_keeps_reference_threshold_behavior():
    built = document._apply_calculated_fields({
        "Custom": {"CertificateAdult": True, "PassThreshold": 0},
        "Usage": {"Type": "Assessment", "validNum": 123},
    }, _result(0), {"target": "adult", "training_type": "cpr", "guideline": "ARC2025"})
    assert built["ResultSummary"]["hStreamScore"] == 0
    assert built["ResultSummary"]["JudgResult"] == "Pass"
    assert built["Certification"] == {"Target": "adult"}


def test_missing_values_keep_reference_defaults_while_explicit_null_survives():
    missing = document._build_result_by_cycle({}, {"cpr_score": {"total_score": {}}})
    assert missing["CompressionDepth"] == {"ByCycle": [0], "Overall": 0}
    assert missing["VentilationSpeed"] == {}
    criteria = document._build_result_by_criteria(
        {"Recoil": {"%_Good": 100}, "ScoreOfCCF": 100},
        {"metrics": {"Recoil": None, "ScoreOfCCF": None}},
    )
    assert criteria["Recoil"] is None
    assert criteria["ScoreOfCCF"] is None


def test_explicit_null_measured_values_are_not_converted_into_measurements():
    result = _result(90)
    result["metrics"] = {"AvgVentilationVolume": None, "AvgVentilationSpeed": None,
                         "AvgCompressionDepth": None, "CCF": {"%_CCF": None}}
    summary = document._build_result_summary({}, result, {}, None)
    for key in ("VentilationVolume", "VentilationSpd", "CompressionDepth", "%CCF", "LungGraph"):
        assert summary[key] is None
    assert summary["CompressionRate"] == 0

"""Adversarial boundaries for sharing the existing response assembly."""

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

import pytest

import lambda_handler
from services.legacy_response import (
    DocumentSelection,
    _convert_result_to_legacy,
    finalize_legacy_response,
)


CONDITION = {"target": "adult", "training_type": "cpr", "guideline": "ARC2025"}


def _core(overall=90):
    return {
        "cpr_score": {"total_score": {"overall": overall, "score_rescue_vent": None}},
        "metrics": {"VentilationSpeed": {"%_Good": 100}},
        "action_count": {"comp": 90, "vent": 6},
        "training_stats": {"cycle_count": 3, "elapsed_seconds": 90},
        "guide_prompts": ["existing coaching"],
    }


def test_converter_remains_the_same_callable_at_the_legacy_import_boundary():
    assert lambda_handler._convert_result_to_legacy is _convert_result_to_legacy


def test_finalizer_does_not_mutate_any_nested_core_result_or_coerce_types():
    core = _core()
    core["preserved_values"] = [None, False, 0, 0.0, "0", {"nested": [1]}]
    original_json = json.dumps(core)
    result = finalize_legacy_response({"condition": CONDITION}, core)

    assert json.dumps(core) == original_json
    assert result["cpr_score"]["total_score"]["score_rescue_vent"] == 0
    assert result["metrics"]["VentilationSpeed"] is None
    assert [type(value) for value in result["preserved_values"]] == [
        type(None), bool, int, float, str, dict,
    ]
    result["preserved_values"][-1]["nested"].append(2)
    assert json.dumps(core) == original_json
    assert "submit_hstm" not in result and "submit_arc" not in result
    assert "hstm_document" not in result


def test_finalizer_excludes_stale_submission_fields_from_calculated_snapshot():
    core = _core()
    core.update(submit_hstm={"ok": True}, submit_arc={"status": "succeeded", "ok": True})
    original = deepcopy(core)
    result = finalize_legacy_response({"condition": CONDITION}, core)
    assert "submit_hstm" not in result and "submit_arc" not in result
    assert core == original


def test_default_selection_uses_nested_document_and_top_level_response_context():
    nested = {
        "Custom": {"CertificateAdult": False},
        "Open_Skill": {"Passing_Score": "99"},
        "Usage": {"Email": "nested@example.invalid", "validNum": 123},
    }
    body = {
        "condition": CONDITION,
        "Custom": {"CertificateAdult": True},
        "Open_Skill": {"Passing_Score": "80"},
        "Usage": {"Email": "top@example.invalid", "validNum": 456},
        "hstm_document": nested,
    }
    result = finalize_legacy_response(body, _core())

    assert result["certification"] == {"Target": "adult"}
    assert body["hstm_document"]["Certification"] == {"Target": "N/A"}
    assert body["hstm_document"]["Usage"]["Email"] == "nested@example.invalid"
    assert body["hstm_document"] is not nested


@pytest.mark.parametrize("nested", [None, {}, [], False, 0, ""])
def test_default_falsy_nested_document_keeps_the_legacy_top_level_fallback(nested):
    body = {
        "condition": CONDITION,
        "Usage": {"Email": "top@example.invalid", "validNum": 123},
        "hstm_document": nested,
    }
    finalize_legacy_response(body, _core())
    assert body["hstm_document"]["Usage"]["Email"] == "top@example.invalid"


def test_default_without_recognized_sections_assigns_no_document():
    body = {"condition": CONDITION, "unrecognized": {"x": 1}}
    finalize_legacy_response(body, _core())
    assert "hstm_document" in body
    assert body["hstm_document"] is None


@pytest.mark.parametrize("source", ["nested", "top_level"])
def test_explicit_empty_projection_preserves_the_already_selected_document(source):
    body = {
        "condition": CONDITION,
        "Usage": {"Email": "must-not-fallback@example.invalid", "validNum": 123},
        "hstm_document": {"Usage": {"Email": "must-not-reselect@example.invalid", "validNum": 456}},
    }
    selection = DocumentSelection(source, {})
    finalize_legacy_response(body, _core(), document_selection=selection)

    assert "ResultSummary" in body["hstm_document"]
    assert "Usage" not in body["hstm_document"]
    assert selection.document == {}


@pytest.mark.parametrize("source", ["nested", "top_level"])
def test_explicit_document_context_is_independent_of_the_stored_manifest(source):
    selected = {
        "Custom": {"CertificateAdult": False},
        "Usage": {"Email": "selected@example.invalid", "validNum": 123},
        "Institution": {"existing_nested_value": [1]},
    }
    before = deepcopy(selected)
    body = {"condition": CONDITION, "Custom": {"CertificateAdult": True}}
    result = finalize_legacy_response(body, _core(), document_selection=DocumentSelection(source, selected))

    assert result["certification"] == {"Target": "adult"}
    assert body["hstm_document"]["Certification"] == {"Target": "N/A"}
    assert selected == before
    body["hstm_document"]["Institution"]["existing_nested_value"].append(2)
    assert selected == before


def test_explicit_absent_document_does_not_reselect_top_level_or_nested_input():
    body = {
        "condition": CONDITION,
        "Custom": {"CertificateAdult": True},
        "hstm_document": {"ResultSummary": {}},
    }
    result = finalize_legacy_response(body, _core(), document_selection=DocumentSelection("none"))
    assert body["hstm_document"] is None
    assert result["certification"] == {"Target": "adult"}


def test_explicit_dict_convertible_document_keeps_legacy_conversion_semantics():
    selected = [["ResultSummary", {}], ["Usage", {"validNum": 123}]]
    body = {"condition": CONDITION}
    finalize_legacy_response(body, _core(), document_selection=DocumentSelection("nested", selected))
    assert body["hstm_document"]["Usage"]["validNum"] == 123
    assert selected == [["ResultSummary", {}], ["Usage", {"validNum": 123}]]


def test_null_total_retains_existing_fail_precedence_and_does_not_escape_to_response():
    body = {
        "condition": CONDITION,
        "Custom": {"CertificateAdult": True},
        "Open_Skill": {"Passing_Score": "0"},
        "hstm_document": {
            "ResultSummary": {"JudgResult": "Pass", "hStreamResult": "Pass", "hStreamReason": "old"},
            "Usage": {"validNum": 123},
        },
    }
    result = finalize_legacy_response(body, _core(None))
    summary = body["hstm_document"]["ResultSummary"]
    assert summary["JudgResult"] == summary["hStreamResult"] == "Fail"
    assert summary["hStreamScore"] is None
    assert "hStreamReason" not in summary
    assert result["cpr_score"]["total_score"]["overall"] is None
    assert result["certification"] == {"Target": "N/A"}
    assert "hstm_document" not in result and "ResultSummary" not in result


@pytest.mark.parametrize("source, document", [
    ("unsupported", {}), ("none", {}), ("nested", None), ("top_level", None),
])
def test_ambiguous_document_selection_is_rejected(source, document):
    with pytest.raises(ValueError):
        DocumentSelection(source, document)


def test_common_module_import_does_not_import_handler_calculator_or_network_clients():
    # A new process catches import cycles that an already-populated sys.modules
    # in this pytest process could hide. No external clients are constructed.
    code = """
import builtins
original_import = builtins.__import__
forbidden = {'lambda_handler', 'main', 'boto3', 'sentry_sdk'}
def guarded_import(name, *args, **kwargs):
    if name.split('.')[0] in forbidden:
        raise AssertionError('Forbidden common module import: ' + name)
    return original_import(name, *args, **kwargs)
builtins.__import__ = guarded_import
from services.legacy_response import finalize_legacy_response
assert callable(finalize_legacy_response)
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr

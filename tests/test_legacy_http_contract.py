"""Real binary -> HTTP -> compatible document, with submission disabled."""

import base64
from copy import deepcopy
import json

import pytest

import lambda_handler
import services.legacy_response as legacy_response
from tests._synth import comp_session, condition_json, cpr_session, multipart_event
from tests.test_reference_http_contract import _form_event


def _event(parts, wire):
    if wire == "multipart":
        return multipart_event(parts)
    fields = dict(parts)
    fields["cpr_b64_data"] = base64.urlsafe_b64encode(fields.pop("rawHexBPfile")).decode()
    return _form_event(fields)


@pytest.mark.parametrize("wire", ["multipart", "form"])
@pytest.mark.parametrize("document_source", ["hstm_document", "hstm_document_b64", "top_level"])
def test_real_null_result_replaces_stale_pass_in_generated_document(wire, document_source, monkeypatch, capsys):
    recorded = []
    for name in ("_build_hstm_document", "_apply_calculated_fields"):
        original = getattr(legacy_response, name)

        def capture(*args, _original=original, **kwargs):
            result = _original(*args, **kwargs)
            recorded.append(deepcopy(result))
            return result

        monkeypatch.setattr(legacy_response, name, capture)
    monkeypatch.setenv("HSTM_SUBMIT_LAMBDA_NAME", "must-never-run")
    monkeypatch.setenv("ARC_SUBMIT_LAMBDA_NAME", "must-never-run")
    document = {
        "Custom": {"CertificateAdult": True, "PassThreshold": 0},
        "Usage": {"Type": "CPR Training", "Email": "synthetic-private@example.invalid", "validNum": 123},
        "Open_Skill": {"Passing_Score": "0"},
        "ResultSummary": {"JudgResult": "Pass", "hStreamResult": "Pass", "hStreamReason": "Old success"},
        "ResultByCriteria": {"VentilationSpeed": {"%_Good": 100}},
    }
    parts = {
        "rawHexBPfile": cpr_session([(10, 1)] * 3),
        "condition": condition_json(guideline="ARC2025"),
        "Custom": json.dumps(document["Custom"]),
        "hstm_access_token": "synthetic-private-token",
    }
    if document_source == "top_level":
        parts.update({key: json.dumps(value) for key, value in document.items()})
    else:
        encoded = json.dumps(document)
        if document_source.endswith("_b64"):
            encoded = base64.urlsafe_b64encode(encoded.encode()).decode()
        parts[document_source] = encoded
    response = lambda_handler._run_trusted_calculation(_event(parts, wire), None)
    assert response["statusCode"] == 200, response
    body = json.loads(response["body"])
    assert body["cpr_score"]["total_score"]["overall"] is None
    assert body["certification"] == {"Target": "N/A"}
    assert "submit_hstm" not in body
    assert body["submit_arc"] == {"status": "disabled", "ok": False, "error": "arc_contract_pending"}
    assert len(recorded) == 1
    generated = recorded[0]
    assert generated["ResultSummary"]["JudgResult"] == "Fail"
    assert generated["ResultSummary"]["hStreamResult"] == "Fail"
    assert generated["ResultSummary"]["hStreamScore"] is None
    assert "hStreamReason" not in generated["ResultSummary"]
    assert generated["ResultByCycle"]["CompressionDepth"]["Overall"] is None
    assert all(x is None for x in generated["ResultByCycle"]["CompressionDepth"]["ByCycle"])
    assert generated["ResultByCriteria"]["VentilationSpeed"] is None
    assert generated["Certification"] == {"Target": "N/A"}
    assert "ResultSummary" not in body and "hstm_document" not in body
    exposed = response["body"] + capsys.readouterr().out
    assert "synthetic-private@example.invalid" not in exposed
    assert "synthetic-private-token" not in exposed


@pytest.mark.parametrize("wire", ["multipart", "form"])
@pytest.mark.parametrize("extra", [
    {"hstm_document": '"synthetic-secret-invalid-document"'},
    {"hstm_document": '{"Open_Skill": [1]}'},
    {"hstm_document": '{"Usage": "synthetic-secret-invalid-section"}'},
    {"ResultSummary": "[1]"},
    {"ResultByCycle": "[1]"},
    {"ResultByCriteria": "[1]"},
    {"Dummy": "5"},
    {"Open_Skill": "true"},
])
def test_invalid_consumed_document_is_sanitized_before_calculation(wire, extra, monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail("Invalid document reached calculation/storage")
    monkeypatch.setattr(lambda_handler, "run_calculator", forbidden)
    parts = {"rawHexBPfile": comp_session(60),
             "condition": condition_json(training_type="compression_only"), **extra}
    response = lambda_handler._run_trusted_calculation(_event(parts, wire), None)
    assert response["statusCode"] == 400
    assert json.loads(response["body"]) == {"type": "client_error", "message": "Invalid request data."}
    assert "synthetic-secret" not in response["body"] + capsys.readouterr().out


@pytest.mark.parametrize("document_value", [
    '{"ResultSummary": [["Extra", 1]], "Usage": {"validNum": 123}}',
    '[["ResultSummary", {}], ["Usage", {"validNum": 123}]]',
    '{"ResultSummary": 0, "Dummy": false, "Usage": []}',
])
def test_reference_dict_convertible_sections_and_falsy_values_are_accepted(document_value):
    response = lambda_handler._run_trusted_calculation(multipart_event({
        "rawHexBPfile": comp_session(60),
        "condition": condition_json(training_type="compression_only"),
        "hstm_document": document_value,
    }), None)
    assert response["statusCode"] == 200, response


def test_unused_top_level_usage_is_not_rejected_when_document_takes_precedence():
    response = lambda_handler._run_trusted_calculation(multipart_event({
        "rawHexBPfile": comp_session(60),
        "condition": condition_json(training_type="compression_only"),
        "Usage": '"ignored-by-calculator-coaching"',
        "hstm_document": '{"Usage": {"Type": "CPR Training", "validNum": 123}}',
    }), None)
    assert response["statusCode"] == 200, response


@pytest.mark.parametrize("learner_id_key", ["HstreamId", "hstreamId"])
def test_reference_usage_fallback_is_skipped_when_document_already_has_learner_id(learner_id_key):
    response = lambda_handler._run_trusted_calculation(multipart_event({
        "rawHexBPfile": comp_session(60),
        "condition": condition_json(training_type="compression_only"),
        "hstm_document": json.dumps({"Usage": "legacy", "ResultSummary": {learner_id_key: "h-1"}}),
    }), None)
    assert response["statusCode"] == 200, response


@pytest.mark.parametrize("condition", ["[1]", '"not-an-object"', "true"])
def test_invalid_form_condition_uses_the_existing_sanitized_error_boundary(condition):
    response = lambda_handler._run_trusted_calculation(_form_event({
        "cpr_b64_data": base64.urlsafe_b64encode(comp_session(60)).decode(),
        "condition": condition,
    }), None)
    assert response["statusCode"] == 400
    assert json.loads(response["body"]) == {"type": "client_error", "message": "Invalid request data."}

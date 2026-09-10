"""Normal wire compatibility and the explicit external-submission boundary."""

import base64
import json
from urllib.parse import quote, urlencode

import pytest

import lambda_handler
from tests._synth import comp_session, condition_json, multipart_event


def _form_event(fields):
    # The reference decodes URL quoting twice: unquote, then parse_qs.
    encoded = quote(urlencode(fields), safe="")
    return {
        "httpMethod": "POST",
        "path": "/cpr-analysis",
        "headers": {"Content-Type": "application/x-www-form-urlencoded"},
        "isBase64Encoded": True,
        "body": base64.urlsafe_b64encode(encoded.encode()).decode(),
    }


def _response(event):
    response = lambda_handler._run_trusted_calculation(event, None)
    assert response["statusCode"] == 200, response
    return json.loads(response["body"])


def _assert_same_types_and_values(left, right):
    assert type(left) is type(right)
    if isinstance(left, dict):
        assert left.keys() == right.keys()
        for key in left:
            _assert_same_types_and_values(left[key], right[key])
    elif isinstance(left, list):
        assert len(left) == len(right)
        for lvalue, rvalue in zip(left, right):
            _assert_same_types_and_values(lvalue, rvalue)
    else:
        assert left == right


@pytest.mark.parametrize("guideline", ["AHA2020", "ARC2020", "ARC2025", "ERC2020", "STD2015"])
@pytest.mark.parametrize("content_type", ["application/x-www-form-urlencoded", "application/json", None])
def test_multipart_and_legacy_form_produce_identical_typed_results(guideline, content_type):
    raw = comp_session(60)
    condition = condition_json(training_type="compression_only", guideline=guideline)
    custom = json.dumps({"CertificateAdult": True})
    multipart = _response(multipart_event({
        "rawHexBPfile": raw,
        "condition": condition,
        "Custom": custom,
    }))
    event = _form_event({
        "cpr_b64_data": base64.urlsafe_b64encode(raw).decode(),
        "condition": condition,
        "Custom": custom,
    })
    event["headers"] = {"Content-Type": content_type} if content_type else {}
    form = _response(event)
    _assert_same_types_and_values(multipart, form)
    assert multipart["certification"] == {"Target": "adult"}
    assert type(multipart["cpr_score"]["total_score"]["score_rescue_vent"]) is int
    assert multipart["cpr_score"]["total_score"]["score_rescue_vent"] == 0
    assert "submit_hstm" not in multipart
    assert multipart["submit_arc"] == {"status": "disabled", "ok": False, "error": "arc_contract_pending"}


def test_submission_credentials_are_accepted_without_invocation_or_logging(monkeypatch, capsys):
    # The autouse AWS guard fails even when production code catches Exception.
    monkeypatch.setenv("ARC_SUBMIT_LAMBDA_NAME", "must-not-invoke-arc")
    monkeypatch.setenv("HSTM_SUBMIT_LAMBDA_NAME", "must-not-invoke-hstm")
    response = _response(multipart_event({
        "rawHexBPfile": comp_session(60),
        "condition": condition_json(training_type="compression_only"),
        "hstm_access_token": "synthetic-access-token-do-not-log",
        "hstm_refresh_token": "synthetic-refresh-token-do-not-log",
        "hstm_client_secret": "synthetic-client-secret-do-not-log",
        "hstm_access_token_url": "https://example.invalid/token",
        "hstm_send_result_url": "https://example.invalid/result",
    }))
    visible = capsys.readouterr().out + json.dumps(response)
    assert "submit_hstm" not in response
    assert response["submit_arc"] == {"status": "disabled", "ok": False, "error": "arc_contract_pending"}
    for value in (
        "synthetic-access-token-do-not-log",
        "synthetic-refresh-token-do-not-log",
        "synthetic-client-secret-do-not-log",
        "https://example.invalid/token",
        "https://example.invalid/result",
    ):
        assert value not in visible

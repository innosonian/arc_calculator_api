"""Exercise the real diagnostic boundaries, without external I/O."""

from copy import deepcopy
import json

import pytest

import lambda_handler
import main
from data_handlers import chart_data
from services import calculate_cpr
from services.http.schemas import DEFAULT_CONDITION


MARKER = "PRIVATE_REQUEST_TOKEN_AND_PERSONAL_DATA"


@pytest.mark.parametrize(
    ("error_class", "status", "message"),
    [(ValueError, 400, "Invalid request data."),
     (RuntimeError, 500, "Internal server error")],
)
def test_handler_redacts_request_and_pipeline_failure_without_changing_error_body(
    monkeypatch, capsys, error_class, status, message,
):
    body = {
        "cpr_b64_data": b"measured bytes", "aed_b64_data": b"",
        "condition": {**DEFAULT_CONDITION, "extra": {"client_secret": MARKER}},
        "vp_event_list": [], "Organization": {"org_name": MARKER},
        "Usage": {"Email": MARKER},
    }
    monkeypatch.setattr(lambda_handler, "parse_body", lambda _: deepcopy(body))

    def fail(*args, **kwargs):
        raise error_class(MARKER)

    monkeypatch.setattr(lambda_handler, "run_calculator", fail)
    response = lambda_handler._run_trusted_calculation({
        "body": MARKER, "path": "/" + MARKER, "httpMethod": "POST",
        "headers": {
            "Authorization": "Bearer " + MARKER, "User-Agent": MARKER,
            "X-Forwarded-For": MARKER,
            "Content-Type": "application/x-www-form-urlencoded; secret=" + MARKER,
        },
    }, None)
    output = capsys.readouterr().out
    assert MARKER not in output
    assert response["statusCode"] == status
    assert json.loads(response["body"])["message"] == message
    records = [json.loads(line) for line in output.splitlines()]
    assert any(record["message"] == "request_failed" for record in records)
    assert any(record["message"] == "parse_complete" for record in records)


def test_storage_failure_logs_keep_existing_best_effort_behaviour(monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise RuntimeError(MARKER)

    monkeypatch.setattr(main, "upload_raw_input", fail)
    assert main._save_raw_input(b"raw", b"", {}, [], {}, "stem", "org", "test") is None
    monkeypatch.setattr(chart_data, "make_chart_data", lambda *args: {})
    monkeypatch.setattr(chart_data, "upload_json_file", fail)
    response = {"untouched": 10.5}
    assert chart_data.add_chart_data(
        response,
        {"whole_cpr_action_list": [], "prepared_aed_data": []},
        {"part_with_scores": []},
    ) == {"untouched": 10.5, "chart_dataset_url": None}
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert MARKER not in json.dumps(records)
    assert {record["message"] for record in records} == {
        "raw_input_save_failed", "chart_upload_failed",
    }


def test_calculation_layer_rethrows_same_exception_and_redacts_diagnostics(monkeypatch, capsys):
    failure = RuntimeError(MARKER)

    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(calculate_cpr, "calculate_cpr", fail)
    with pytest.raises(RuntimeError) as raised:
        calculate_cpr.make_calculate_result(None, {}, "test")
    assert raised.value is failure
    output = capsys.readouterr().out
    assert MARKER not in output
    assert any(json.loads(line)["message"] == "calc_failed" for line in output.splitlines())

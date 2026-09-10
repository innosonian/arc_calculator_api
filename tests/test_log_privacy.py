"""Privacy regressions through real SDK hooks and existing log call sites.

The transport retains envelopes in memory. No Sentry or AWS request is made.
"""

import json
import socket

import pytest
import sentry_sdk
from sentry_sdk import Client, Scope
from sentry_sdk.profiler import Profile
from sentry_sdk.transport import Transport

import lambda_handler
import main
import data_handlers.chart_data as chart_data
import services.calculate_cpr as calculate_cpr_service
from services.observability import sanitize_log_record, sentry_privacy_options
from tests._synth import comp_session, condition_json, multipart_event


_MARKER = "private-marker-arc-user-token-should-never-appear"
_REQUEST_ID = "cad783e5-0313-44a7-9693-97d929d99ad9"


class MemoryTransport(Transport):
    def __init__(self):
        super().__init__()
        self.envelopes = []

    def capture_envelope(self, envelope):
        self.envelopes.append(envelope)


@pytest.fixture
def sdk_client(monkeypatch):
    network_calls = []

    def reject_network(*args, **kwargs):
        network_calls.append(True)
        raise AssertionError("The privacy tests cannot use the network")

    monkeypatch.setattr(socket.socket, "connect", reject_network)
    monkeypatch.setattr(socket, "create_connection", reject_network)
    transport = MemoryTransport()
    client = Client(
        dsn="https://synthetic@example.invalid/1",
        transport=transport,
        default_integrations=False,
        auto_enabling_integrations=False,
        traces_sample_rate=1.0,
        **sentry_privacy_options(),
    )
    try:
        with sentry_sdk.new_scope() as active_scope:
            active_scope.set_client(client)
            yield client, transport
    finally:
        client.close(timeout=0)
        assert not network_calls


def test_log_allowlist_keeps_counts_and_removes_request_and_nested_data():
    fields = {
        "request_id": _REQUEST_ID,
        "path": "/cpr-analysis",
        "http_method": "POST",
        "content_type": f"multipart/form-data; boundary={_MARKER}",
        "content_length": "3200",
        "condition_target": "infant",
        "condition_guideline": "ARC2025",
        "condition": {"client_secret": _MARKER},
        "condition_mode": {"password": _MARKER},
        "vp_event_count": None,
        "cpr_bytes": 3200,
        "source_ip": _MARKER,
        "user_agent": _MARKER,
        "Authorization": _MARKER,
        "body": _MARKER,
        "profile": {"Usage": {"Email": _MARKER}},
        "unknown": _MARKER,
    }
    result = sanitize_log_record("info", "parse_complete", fields)
    assert _MARKER not in json.dumps(result)
    assert result["request_id"] == _REQUEST_ID
    assert result["content_type"] == "multipart/form-data"
    assert result["content_length"] == "3200"
    assert result["cpr_bytes"] == 3200
    assert result["vp_event_count"] is None
    assert result["condition_mode"] is None
    assert "condition" not in result
    assert "source_ip" not in result
    assert fields["condition"]["client_secret"] == _MARKER


def test_free_text_and_numeric_lookalikes_are_not_diagnostic_identifiers():
    result = sanitize_log_record(_MARKER, _MARKER, {
        "request_id": _MARKER,
        "error_type": _MARKER,
        "error_message": _MARKER,
        "stacktrace": [_MARKER],
        "cpr_bytes": True,
        "elapsed_ms": {"token": _MARKER},
        "content_length": _MARKER,
        "condition_is_2rescuers": {"token": _MARKER},
    })
    assert _MARKER not in json.dumps(result)
    assert result["message"] == "diagnostic_event"
    assert result["error_type"] == "Exception"
    assert result["stacktrace"]
    assert result["cpr_bytes"] is None
    assert result["elapsed_ms"] is None


def test_exception_log_preserves_code_location_without_source_or_locals():
    secret_local = _MARKER
    try:
        raise ValueError(secret_local)
    except ValueError as error:
        result = sanitize_log_record("error", "request_failed", {"exception": error})
    assert result["error_type"] == "ValueError"
    assert any("tests/test_log_privacy.py:" in frame for frame in result["stacktrace"])
    assert _MARKER not in json.dumps(result)
    assert "/Users/" not in json.dumps(result)


def test_real_sdk_error_hook_removes_scope_frames_breadcrumbs_and_attachments(sdk_client, monkeypatch):
    client, transport = sdk_client
    assert sentry_sdk.VERSION == "2.22.0"
    assert client.options["auto_session_tracking"] is False
    breadcrumb_calls = []
    original_hook = client.options["before_breadcrumb"]

    def observed_breadcrumb(crumb, hint):
        breadcrumb_calls.append(True)
        return original_hook(crumb, hint)

    monkeypatch.setitem(client.options, "before_breadcrumb", observed_breadcrumb)
    scope = Scope(client=client)
    scope.set_user({"email": _MARKER, "ip_address": _MARKER})
    scope.set_context("sensitive_context", {"token": _MARKER})
    scope.set_extra("password", _MARKER)
    scope.set_tag("account", _MARKER)
    scope.add_attachment(bytes=_MARKER.encode(), filename="private.txt")
    scope.add_breadcrumb({"message": _MARKER, "data": {"url": _MARKER}})
    assert breadcrumb_calls == [True]

    def add_sensitive_event_data(event, hint):
        event["request"] = {"headers": {"Authorization": _MARKER}, "data": _MARKER}
        event["breadcrumbs"] = {"values": [{"message": _MARKER}]}
        event["threads"] = {"values": [{"stacktrace": {"frames": [{"vars": {"secret": _MARKER}}]}}]}
        event["message"] = _MARKER
        event["server_name"] = _MARKER
        event["contexts"] = {"trace": {"dynamic_sampling_context": {"user_id": _MARKER}}}
        return event

    scope.add_event_processor(add_sensitive_event_data)
    try:
        raise ValueError(_MARKER)
    except ValueError as error:
        event_id = scope.capture_exception(error)
    assert event_id
    assert len(transport.envelopes) == 1
    envelope = transport.envelopes[0]
    assert [item.headers["type"] for item in envelope.items] == ["event"]
    visible = envelope.serialize().decode()
    assert _MARKER not in visible
    event = envelope.items[0].payload.json
    assert event["exception"]["values"][0]["type"] == "ValueError"
    assert "request" not in event
    assert "breadcrumbs" not in event
    assert "contexts" not in event
    assert "vars" not in visible


def test_real_sdk_transaction_hook_drops_separately_attached_profile(sdk_client, monkeypatch):
    client, transport = sdk_client
    hook_calls = []
    original_hook = client.options["before_send_transaction"]

    def observed_hook(event, hint):
        hook_calls.append(True)
        return original_hook(event, hint)

    monkeypatch.setitem(client.options, "before_send_transaction", observed_hook)
    scope = Scope(client=client)
    scope.add_attachment(bytes=_MARKER.encode(), filename="private.txt", add_to_transactions=True)
    profile = Profile(sampled=True, start_ns=1)
    profile.frames.append({"filename": _MARKER})
    serialized_profiles = []

    def forbidden_profile(*args, **kwargs):
        serialized_profiles.append(True)
        return {"private": _MARKER}

    monkeypatch.setattr(Profile, "to_json", forbidden_profile)
    assert scope.capture_event({
        "type": "transaction",
        "transaction": _MARKER,
        "start_timestamp": 1.0,
        "timestamp": 2.0,
        "profile": profile,
        "spans": [{"description": _MARKER}],
    }) is None
    assert hook_calls == [True]
    assert not transport.envelopes
    assert not serialized_profiles


def test_handler_error_logs_cannot_bypass_sanitizer(monkeypatch, capsys):
    def fail_calculation(*args, **kwargs):
        raise RuntimeError(_MARKER)

    monkeypatch.setattr(lambda_handler, "run_calculator", fail_calculation)
    condition = json.loads(condition_json(training_type="compression_only"))
    condition["debug"] = {"client_secret": _MARKER}
    event = multipart_event({"rawHexBPfile": comp_session(60), "condition": json.dumps(condition)})
    event["headers"]["User-Agent"] = _MARKER
    event["headers"]["X-Forwarded-For"] = _MARKER
    response = lambda_handler._run_trusted_calculation(event, None)
    assert response["statusCode"] == 500
    assert json.loads(response["body"]) == {"type": "server_error", "message": "Internal server error"}
    visible = capsys.readouterr().out
    assert "request_failed" in visible
    assert _MARKER not in visible


def test_raw_input_and_chart_errors_remain_nonfatal_without_logging_exception_text(monkeypatch, capsys):
    def fail_upload(*args, **kwargs):
        raise RuntimeError(_MARKER)

    monkeypatch.setattr(main, "upload_raw_input", fail_upload)
    main._save_raw_input(b"a", b"", {}, [], {}, "stem", "org", "prod")
    monkeypatch.setattr(chart_data, "make_chart_data", lambda *args: {})
    monkeypatch.setattr(chart_data, "upload_json_file", fail_upload)
    result = chart_data.add_chart_data(
        {}, {"whole_cpr_action_list": [], "prepared_aed_data": []},
        {"part_with_scores": []}, stage="prod",
    )
    calculate_cpr_service._log("error", "calc_failed", error_message=_MARKER, stacktrace=[_MARKER])
    assert result["chart_dataset_url"] is None
    visible = capsys.readouterr().out
    assert "raw_input_save_failed" in visible
    assert "chart_upload_failed" in visible
    assert "calc_failed" in visible
    assert _MARKER not in visible

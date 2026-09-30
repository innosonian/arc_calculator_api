"""Q7=A: /api/v2 rejections and failures are recorded like the removed /mock/v1 routes were.

The app response (status, headers, body, X-Request-Id source) is unchanged.
Records carry the X-Request-Id value as http_request_id; request_id keeps the
runtime (Lambda/local adapter) meaning. Recorder failures never change a reply.
"""

from datetime import datetime, timezone
import json
from types import SimpleNamespace
import uuid

import pytest

from mock_journey.aws_logs import InvocationBuffer, InvocationLogs
from mock_journey.aws_settings import AwsSettings
from mock_journey.course_errors import CourseError
from mock_journey.course_http import CourseHttp
from mock_journey.course_settings import fixture_course_settings
from mock_journey.course_wiring import COURSE_MODE, _login_hook
from mock_journey.errors import JourneyError
from mock_journey.handler import handle
from mock_journey.log_storage import DynamoLogStore, OperationalLogError, _parse
from mock_journey.models import AuthContext
from mock_journey.service import JourneyService
from services.operational_logs import (
    AsyncLogRecorder, bound_identifiers, log_context, record_event, validate_record, write_diagnostic,
)
from tests.aws_runtime_support import configuration, context as aws_context
from tests.course_hooks_support import attempt_record, calculation_record, hooks_with
from tests.vcc_support import event


MARKER = "PRIVATE-V2-LOG-MARKER"
TOKEN = "fixture-v2-token-" + MARKER
HTTP_ID = "71000000-0000-4000-8000-000000000001"
RUNTIME_ID = "72000000-0000-4000-8000-000000000001"
SESSION_ID = "73000000-0000-4000-8000-000000000001"
ATTEMPT_ID = "74000000-0000-4000-8000-000000000001"
CLOCK = lambda: 1_800_000_000
AUTH = AuthContext(SESSION_ID, "dummy-tester", 1, 1_800_086_400)
STAMP = "2026-09-28T00:00:00.000000Z"


class ListRecorder:
    """Stores only what the real envelope validator accepts."""

    def __init__(self):
        self.rows = []

    def record(self, category, name, fields):
        raw = validate_record({"schema": 1, "log_id": str(uuid.uuid4()), "occurred_at": STAMP,
                               "role": "api", "category": category, "event": name, "fields": fields})
        self.rows.append(json.loads(raw))
        return True


class BrokenRecorder:
    def __init__(self):
        self.calls = 0

    def record(self, *args):
        self.calls += 1
        raise RuntimeError(MARKER)


def _raise(error):
    raise error


def course_http(**hooks):
    service = SimpleNamespace(refresh_for_session=lambda auth: _raise(CourseError("CONTRACT_PENDING")))
    defaults = {
        "authenticate": lambda token, **k: AUTH if token == TOKEN else _raise(CourseError("SESSION_REQUIRED")),
        "load_attempt": lambda auth, ident: _raise(CourseError("NOT_FOUND")),
        "login": lambda login_id, password: _raise(JourneyError("LOGIN_FAILED")),
    }
    defaults.update(hooks)
    return CourseHttp(service, fixture_course_settings(), clock=CLOCK, uuid_factory=lambda: HTTP_ID,
                      hooks=hooks_with(**defaults))


def application(http, operations):
    return SimpleNamespace(course_mode=COURSE_MODE, course_http=http, operations=operations)


def send(request, http, operations):
    return handle(request, SimpleNamespace(aws_request_id=RUNTIME_ID), application(http, operations))


def attempt_path():
    return f"/api/v2/attempts/{ATTEMPT_ID}/"


def fields(row):
    return row["fields"]


def test_course_error_is_request_rejected_with_http_request_id_and_unchanged_reply():
    recorder = ListRecorder()
    request = event("GET", attempt_path(), token=TOKEN)
    response = send(request, course_http(), recorder)
    assert response == send(request, course_http(), None)
    assert response["statusCode"] == 404
    assert response["headers"] == {"Content-Type": "application/json", "Cache-Control": "no-store",
                                   "X-Request-Id": HTTP_ID}
    assert json.loads(response["body"])["error"]["code"] == "NOT_FOUND"
    assert HTTP_ID not in response["body"] and RUNTIME_ID not in response["body"]
    assert [(row["category"], row["event"]) for row in recorder.rows] == [("operation", "request_rejected")]
    assert fields(recorder.rows[0]) == {"request_id": RUNTIME_ID, "http_request_id": HTTP_ID,
                                        "error_code": "NOT_FOUND", "http_status": 404}
    assert fields(recorder.rows[0])["http_request_id"] == response["headers"]["X-Request-Id"]


@pytest.mark.parametrize("request_event,code,status", [
    (event("PUT", "/api/v2/session/", token=TOKEN, body={}), "METHOD_NOT_ALLOWED", 405),
    (event("GET", "/api/v2/unknown/", token=TOKEN), "NOT_FOUND", 404),
    (event("GET", attempt_path()), "SESSION_REQUIRED", 401),
    (event("GET", "/api/v2/courses/progress/", token=TOKEN, query={"pageSize": "x"}), "INVALID_REQUEST", 400),
])
def test_course_only_and_reused_public_codes_are_recorded(request_event, code, status):
    recorder = ListRecorder()
    response = send(request_event, course_http(), recorder)
    assert response["statusCode"] == status
    assert json.loads(response["body"])["error"]["code"] == code
    assert [row["event"] for row in recorder.rows] == ["request_rejected"]
    assert fields(recorder.rows[0])["error_code"] == code
    assert fields(recorder.rows[0])["http_status"] == status
    assert fields(recorder.rows[0])["http_request_id"] == response["headers"]["X-Request-Id"]
    assert TOKEN not in json.dumps(recorder.rows)


def test_journey_error_from_reused_hook_is_recorded_with_its_public_code():
    recorder = ListRecorder()
    http = course_http(authenticate=lambda token, **k: _raise(JourneyError("SESSION_EXPIRED")))
    response = send(event("GET", attempt_path(), token=TOKEN), http, recorder)
    assert response["statusCode"] == 401
    assert fields(recorder.rows[0]) == {"request_id": RUNTIME_ID, "http_request_id": HTTP_ID,
                                        "error_code": "SESSION_EXPIRED", "http_status": 401}


@pytest.mark.parametrize("body,code,status", [
    ({"loginId": "test@test.com", "password": MARKER}, "LOGIN_FAILED", 401),
    ({"loginId": "other@example.test", "password": MARKER}, "CONTRACT_PENDING", 503),
    ({"loginId": "test@test.com"}, "INVALID_REQUEST", 400),
])
def test_login_route_failures_are_login_failed(body, code, status):
    recorder = ListRecorder()
    request = event("POST", "/api/v2/sessions/", body=body)
    response = send(request, course_http(), recorder)
    assert response == send(request, course_http(), None)
    assert response["statusCode"] == status
    assert [row["event"] for row in recorder.rows] == ["login_failed"]
    assert fields(recorder.rows[0]) == {"request_id": RUNTIME_ID, "http_request_id": HTTP_ID,
                                        "error_code": code, "http_status": status}
    assert MARKER not in json.dumps(recorder.rows)


def test_other_method_on_login_path_is_request_rejected():
    recorder = ListRecorder()
    response = send(event("GET", "/api/v2/sessions/"), course_http(), recorder)
    assert response["statusCode"] == 405
    assert [row["event"] for row in recorder.rows] == ["request_rejected"]


def test_unexpected_exception_records_sanitized_request_failed_diagnostic():
    recorder = ListRecorder()
    def broken(auth, ident):
        raise RuntimeError(MARKER + TOKEN)
    request = event("GET", attempt_path(), token=TOKEN)
    response = send(request, course_http(load_attempt=broken), recorder)
    assert response == send(request, course_http(load_attempt=broken), None)
    assert response["statusCode"] == 503
    assert json.loads(response["body"])["error"]["code"] == "TEMPORARILY_UNAVAILABLE"
    assert [(row["category"], row["event"]) for row in recorder.rows] == [
        ("operation", "request_rejected"), ("diagnostic", "request_failed"),
    ]
    assert fields(recorder.rows[0]) == {"request_id": RUNTIME_ID, "http_request_id": HTTP_ID,
                                        "error_code": "TEMPORARILY_UNAVAILABLE", "http_status": 503}
    diagnostic = fields(recorder.rows[1])
    assert diagnostic["level"] == "error" and diagnostic["message"] == "request_failed"
    assert diagnostic["error_type"] == "RuntimeError"
    assert diagnostic["error_message"] == "Exception details redacted."
    assert diagnostic["request_id"] == RUNTIME_ID
    assert diagnostic["http_request_id"] == response["headers"]["X-Request-Id"]
    assert any(frame.startswith("tests/test_v2_observability.py:") for frame in diagnostic["stacktrace"])
    raw = json.dumps(recorder.rows)
    assert MARKER not in raw and TOKEN not in raw


def test_reused_journey_login_hook_events_carry_http_request_id():
    recorder = ListRecorder()
    token = f"s1.{SESSION_ID}.{'a' * 43}"
    auth = SimpleNamespace(
        login=lambda login_id, password: ({"session_id": SESSION_ID, "principal": "dummy-tester",
                                           "expires_at": 1_800_086_400}, token),
        authenticate=lambda value, **k: AUTH,
    )
    journey = JourneyService(None, auth, SimpleNamespace())
    request = event("POST", "/api/v2/sessions/", body={"loginId": "test@test.com", "password": "2222"})
    response = send(request, course_http(login=_login_hook(journey)), recorder)
    assert response["statusCode"] == 201
    assert response == send(request, course_http(login=_login_hook(journey)), None)
    assert [row["event"] for row in recorder.rows] == ["login_succeeded"]
    assert fields(recorder.rows[0]) == {"request_id": RUNTIME_ID, "http_request_id": HTTP_ID,
                                        "session_id": SESSION_ID}
    assert token not in json.dumps(recorder.rows)


def test_hook_events_and_diagnostics_inside_a_success_share_the_header_id():
    recorder = ListRecorder()
    def measurement(auth, ident, request_event):
        record_event("calculation_accepted", attempt_id=ident, job_id=HTTP_ID.replace("71", "75", 1))
        write_diagnostic("info", "parse_complete", {"cpr_bytes": 12, "body": MARKER})
        return calculation_record(ident, "queued")
    http = course_http(measurement_submit=measurement)
    response = send(event("POST", f"/api/v2/attempts/{ATTEMPT_ID}/calculation/", token=TOKEN,
                          body="binary", content_type="multipart/form-data; boundary=x"), http, recorder)
    assert response["statusCode"] == 202
    assert [row["event"] for row in recorder.rows] == ["calculation_accepted", "parse_complete"]
    assert all(fields(row)["http_request_id"] == response["headers"]["X-Request-Id"] for row in recorder.rows)
    assert all(fields(row)["request_id"] == RUNTIME_ID for row in recorder.rows)
    assert MARKER not in json.dumps(recorder.rows)


@pytest.mark.parametrize("scenario", ["rejected", "login", "unexpected", "success"])
def test_recorder_failure_never_changes_the_reply(scenario, capsys):
    def broken(auth, ident):
        raise RuntimeError(MARKER)
    loaded = attempt_record(ATTEMPT_ID, "created", "2026-09-18T00:00:00Z",
                            {"mode": "training", "target": "adult", "training_type": "compression_only",
                             "guideline": "ARC2025", "cpr_cycle_type": "302", "is_2rescuers": False},
                            legacy=True)
    requests = {
        "rejected": (event("GET", attempt_path(), token=TOKEN), {}),
        "login": (event("POST", "/api/v2/sessions/", body={"loginId": "test@test.com", "password": "x"}), {}),
        "unexpected": (event("GET", attempt_path(), token=TOKEN), {"load_attempt": broken}),
        "success": (event("GET", attempt_path(), token=TOKEN), {"load_attempt": lambda auth, ident: loaded}),
    }
    request, hooks = requests[scenario]
    recorder = BrokenRecorder()
    failing = send(request, course_http(**hooks), recorder)
    assert failing == send(request, course_http(**hooks), None)
    assert failing["headers"]["X-Request-Id"] == HTTP_ID
    assert (failing["statusCode"] == 200) is (scenario == "success")
    assert (recorder.calls > 0) is (scenario != "success")
    assert MARKER not in capsys.readouterr().err


def test_binding_is_limited_to_one_dispatch_and_handler_records_carry_no_header_id():
    recorder = ListRecorder()
    with log_context(recorder, request_id=RUNTIME_ID):
        course_http().dispatch(event("GET", attempt_path(), token=TOKEN))
        record_event("logout_succeeded")
    assert [row["event"] for row in recorder.rows] == ["request_rejected", "logout_succeeded"]
    assert fields(recorder.rows[1]) == {"request_id": RUNTIME_ID}
    # The handler's own boot-failure envelope (no course_http) has no X-Request-Id.
    handler_rows = ListRecorder()
    service = SimpleNamespace(operations=handler_rows, course_mode=COURSE_MODE, course_http=None)
    response = handle(event("GET", "/api/v2/session/", token=TOKEN), SimpleNamespace(aws_request_id=RUNTIME_ID),
                      service)
    assert response["statusCode"] == 503 and "X-Request-Id" not in response["headers"]
    assert [row["event"] for row in handler_rows.rows] == ["request_rejected"]
    assert fields(handler_rows.rows[0]) == {"request_id": RUNTIME_ID, "error_code": "TEMPORARILY_UNAVAILABLE",
                                            "http_status": 503}


def _record(category, name, extra):
    base = {"request_id": RUNTIME_ID}
    if category == "diagnostic":
        base = {"level": "error", "message": name, "request_id": RUNTIME_ID}
    return {"schema": 1, "log_id": str(uuid.uuid4()), "occurred_at": STAMP, "role": "api",
            "category": category, "event": name, "fields": {**base, **extra}}


@pytest.mark.parametrize("category,name", [("operation", "request_rejected"), ("diagnostic", "request_failed")])
def test_envelope_accepts_uuid_http_request_id_and_rejects_other_forms(category, name):
    raw = validate_record(_record(category, name, {"http_request_id": HTTP_ID}))
    assert _parse(raw)["fields"]["http_request_id"] == HTTP_ID
    for bad in ("local", HTTP_ID[:-1] + "A", MARKER, "", 7, None, HTTP_ID + "0"):
        with pytest.raises(ValueError):
            validate_record(_record(category, name, {"http_request_id": bad}))
        # _parse surfaces the validator's ValueError; DynamoLogStore wraps both.
        with pytest.raises((ValueError, OperationalLogError)):
            _parse(json.dumps(_record(category, name, {"http_request_id": bad}), sort_keys=True,
                              separators=(",", ":")).encode())


def test_context_binding_drops_malformed_http_request_id():
    recorder = ListRecorder()
    with log_context(recorder, request_id=RUNTIME_ID):
        with bound_identifiers(http_request_id=MARKER):
            record_event("login_succeeded")
    assert fields(recorder.rows[0]) == {"request_id": RUNTIME_ID}


def test_dynamo_log_store_and_async_recorder_keep_http_request_id():
    stored = []
    class Client:
        def put_item(self, **kwargs):
            stored.append(kwargs["Item"]["record"]["S"])
        def close(self):
            pass
    store = DynamoLogStore(Client(), "ops-table", "test")
    log = AsyncLogRecorder(lambda: store, role="api",
                           clock=lambda: datetime(2026, 9, 28, tzinfo=timezone.utc))
    try:
        with log_context(log, request_id=RUNTIME_ID):
            send(event("GET", attempt_path(), token=TOKEN), course_http(), log)
        assert log.close(timeout=2)
    finally:
        log.close()
    assert len(stored) == 1
    assert json.loads(stored[0])["fields"]["http_request_id"] == HTTP_ID


def test_aws_invocation_buffer_accepts_http_request_id_from_course_dispatch():
    records = []
    settings = AwsSettings.parse(json.dumps(configuration()), "api").logs
    log = InvocationLogs(lambda: SimpleNamespace(write=lambda raw: records.append(json.loads(raw)),
                                                 close=lambda: None), role="api", settings=settings)
    ctx = aws_context()
    with log.invocation(ctx) as buffer:
        response = handle(event("GET", attempt_path(), token=TOKEN), ctx, application(course_http(), log))
    assert type(buffer) is InvocationBuffer and buffer.status()["stored"] == 1
    assert records[0]["fields"] == {"request_id": ctx.aws_request_id, "http_request_id": HTTP_ID,
                                    "error_code": "NOT_FOUND", "http_status": 404}
    assert records[0]["fields"]["http_request_id"] == response["headers"]["X-Request-Id"]

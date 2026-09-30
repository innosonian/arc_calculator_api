"""Characterization of the /api/v2 error envelope and list paging the baseline does not reach.

tests/fixtures/v2_baseline/flow_responses.json fixes success replies and only the
403/404/409 error envelopes; the 400/405/413/422/503 envelopes, 401, the
``next``/``previous`` links of a second page and the 405 route answer are
written here by hand as literals of the current code (2026-09-30, C-02).
They are expectations of the current wire, not of any approved change; the
JSON body text is compared as a whole so key order and null spelling are
pinned too. Nothing is captured from the code under test.
"""

import base64
import json
from types import SimpleNamespace

import pytest

from mock_journey.course_errors import CourseError
from mock_journey.course_http import CourseHttp
from mock_journey.course_settings import fixture_course_settings
from mock_journey.models import AuthContext
from tests.course_hooks_support import hooks_with, session_record
from tests.vcc_api_support import World, event as world_event


HTTP_ID = "71000000-0000-4000-8000-000000000001"
SESSION_ID = "73000000-0000-4000-8000-000000000001"
ATTEMPT_ID = "74000000-0000-4000-8000-000000000001"
TOKEN = "envelope-token"
AUTH = AuthContext(SESSION_ID, "dummy-tester", 1, 1_800_086_400)
TIMESTAMP = "2027-01-15T08:00:00Z"  # clock 1_800_000_000
HEADERS = {"Content-Type": "application/json", "Cache-Control": "no-store", "X-Request-Id": HTTP_ID}


def _raise(error):
    raise error


def http():
    service = SimpleNamespace(refresh_for_session=lambda auth: _raise(RuntimeError("refresh")))
    hooks = hooks_with(
        authenticate=lambda token, **k: AUTH if token == TOKEN else _raise(CourseError("SESSION_REQUIRED")),
        login=lambda login_id, password: session_record(SESSION_ID, "2027-01-16T08:00:00Z", auth=AUTH,
                                                        access_token=TOKEN, user_name="Test User"),
        measurement_submit=lambda auth, ident, request: _raise(CourseError("MEASUREMENT_INPUT_INVALID")),
        load_attempt=lambda auth, ident: _raise(RuntimeError("storage")),
    )
    return CourseHttp(service, fixture_course_settings(), clock=lambda: 1_800_000_000,
                      uuid_factory=lambda: HTTP_ID, hooks=hooks)


def request(method, path, *, body=None, token=None, base64_body=None):
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    value = {"httpMethod": method, "path": path, "headers": headers, "body": body}
    if base64_body is not None:
        value["body"] = base64.b64encode(base64_body).decode("ascii")
        value["isBase64Encoded"] = True
    return value


def envelope(code, message):
    return ('{"success": false, "error": {"code": "' + code + '", "message": "' + message
            + '", "details": null}, "timestamp": "' + TIMESTAMP + '"}')


CASES = [
    ("400 bad json login body", request("POST", "/api/v2/sessions/", body="{not json"),
     400, envelope("INVALID_REQUEST", "Invalid request.")),
    ("401 no bearer", request("GET", "/api/v2/session/"),
     401, envelope("SESSION_REQUIRED", "A valid session is required.")),
    ("405 PUT on session", request("PUT", "/api/v2/session/", body="{}", token=TOKEN),
     405, envelope("METHOD_NOT_ALLOWED", "Method not allowed.")),
    ("413 oversized base64 login", request("POST", "/api/v2/sessions/", base64_body=b" " * 16385),
     413, envelope("PAYLOAD_TOO_LARGE", "The request exceeds the verified payload limit.")),
    ("422 measurement input", request("POST", f"/api/v2/attempts/{ATTEMPT_ID}/calculation/", body="x", token=TOKEN),
     422, envelope("MEASUREMENT_INPUT_INVALID", "The measurement input is invalid.")),
    ("503 non-dummy login", request("POST", "/api/v2/sessions/",
                                    body=json.dumps({"loginId": "other@test.com", "password": "2222"})),
     503, envelope("CONTRACT_PENDING", "The integration contract is not available.")),
    ("503 unexpected hook error", request("GET", f"/api/v2/attempts/{ATTEMPT_ID}/", token=TOKEN),
     503, envelope("TEMPORARILY_UNAVAILABLE", "The service is temporarily unavailable.")),
    ("503 refresh failure after login", request("POST", "/api/v2/sessions/",
                                                body=json.dumps({"loginId": "test@test.com", "password": "2222"})),
     503, envelope("TEMPORARILY_UNAVAILABLE", "The service is temporarily unavailable.")),
]


@pytest.mark.parametrize("name,value,status,body", CASES, ids=[case[0] for case in CASES])
def test_error_envelope_text_status_and_headers(name, value, status, body):
    response = http().dispatch(value)
    assert response["statusCode"] == status
    assert response["headers"] == HEADERS
    assert response["body"] == body
    assert list(response) == ["statusCode", "headers", "body"]


def test_envelope_literal_is_order_sensitive():
    # Negative control: the same fields in another order are a different body.
    body = envelope("INVALID_REQUEST", "Invalid request.")
    parsed = json.loads(body)
    assert list(parsed) == ["success", "error", "timestamp"]
    assert list(parsed["error"]) == ["code", "message", "details"]
    reordered = json.dumps({"timestamp": parsed["timestamp"], "error": parsed["error"], "success": False})
    assert json.loads(reordered) == parsed and reordered != body


def test_405_lists_no_allow_header_and_404_is_not_used_for_a_known_path():
    response = http().dispatch(request("PUT", "/api/v2/session/", body="{}", token=TOKEN))
    assert response["statusCode"] == 405
    assert "Allow" not in response["headers"]
    missing = http().dispatch(request("GET", "/api/v2/session", token=TOKEN))
    assert missing["statusCode"] == 404
    assert missing["body"] == envelope("NOT_FOUND", "Not found.")


def test_second_page_links_of_the_course_list():
    world = World()  # two enrollments of course 101 (501, 502)
    first = json.loads(world.http.dispatch(world_event(
        "GET", "/api/v2/courses/progress/", query={"page": "1", "pageSize": "1"}))["body"])
    second = json.loads(world.http.dispatch(world_event(
        "GET", "/api/v2/courses/progress/", query={"page": "2", "pageSize": "1"}))["body"])
    assert first["success"] is True and second["success"] is True
    assert list(first["data"]) == ["results", "count", "next", "previous"]
    assert (first["data"]["count"], len(first["data"]["results"])) == (2, 1)
    assert first["data"]["next"] == "/api/v2/courses/progress/?page=2&pageSize=1"
    assert first["data"]["previous"] is None
    assert (second["data"]["count"], len(second["data"]["results"])) == (2, 1)
    assert second["data"]["next"] is None
    assert second["data"]["previous"] == "/api/v2/courses/progress/?page=1&pageSize=1"
    assert [row["enrollmentId"] for row in first["data"]["results"] + second["data"]["results"]] == [501, 502]
    third = json.loads(world.http.dispatch(world_event(
        "GET", "/api/v2/courses/progress/", query={"page": "3", "pageSize": "1"}))["body"])
    assert third["data"] == {"results": [], "count": 2, "next": None,
                             "previous": "/api/v2/courses/progress/?page=2&pageSize=1"}

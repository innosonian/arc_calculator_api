"""Public course request limits, without AWS, sockets, or database access."""

import base64
import json

import pytest

from mock_journey import course_http
from mock_journey.course_contracts import PUBLIC_ID_MAX
from tests.test_vcc_api import World, decode, event


@pytest.mark.parametrize("key,path", [
    ("page", "/api/v2/courses/progress/"),
    ("pageSize", "/api/v2/courses/progress/"),
    ("enrollmentId", "/api/v2/courses/101/progress/"),
])
@pytest.mark.parametrize("value", [str(PUBLIC_ID_MAX + 1), "9" * 5000])
def test_oversized_decimal_query_is_client_error_before_numeric_conversion(key, path, value):
    world = World()
    response = world.http.dispatch(event("GET", path, query={key: value}))
    assert response["statusCode"] == 400
    assert decode(response)["error"]["code"] == "INVALID_REQUEST"
    assert world.repository.calls == []


def test_oversized_base64_login_is_rejected_before_decode(monkeypatch):
    world = World()
    limit = world.http._settings.max_control_body_bytes
    request = event("POST", "/api/v2/sessions/", auth=False,
                    raw=base64.b64encode(b" " * (limit + 3)).decode())
    request["isBase64Encoded"] = True

    def forbidden_decode(*args, **kwargs):
        pytest.fail("An oversized public login body reached the base64 decoder.")

    monkeypatch.setattr(course_http.base64, "b64decode", forbidden_decode)
    response = world.http.dispatch(request)
    assert response["statusCode"] == 413
    assert decode(response)["error"]["code"] == "PAYLOAD_TOO_LARGE"
    assert world.executed == []


@pytest.mark.parametrize("extra_bytes,expected_status", [(0, 201), (1, 413)])
def test_base64_control_limit_uses_decoded_bytes_at_padding_boundary(extra_bytes, expected_status):
    world = World()
    limit = world.http._settings.max_control_body_bytes
    body = json.dumps({"loginId": "test@test.com", "password": "2222"}).encode()
    body += b" " * (limit + extra_bytes - len(body))
    request = event("POST", "/api/v2/sessions/", auth=False,
                    raw=base64.b64encode(body).decode())
    request["isBase64Encoded"] = True
    response = world.http.dispatch(request)
    assert response["statusCode"] == expected_status
    if extra_bytes:
        assert decode(response)["error"]["code"] == "PAYLOAD_TOO_LARGE"
        assert world.executed == []


def test_control_limit_keeps_utf8_byte_semantics():
    world = World()
    limit = world.http._settings.max_control_body_bytes
    body = '{"loginId":"test@test.com","password":"' + "가" * (limit // 3) + '"}'
    assert len(body) < limit < len(body.encode("utf-8"))
    response = world.http.dispatch(event("POST", "/api/v2/sessions/", auth=False, raw=body))
    assert response["statusCode"] == 413
    assert decode(response)["error"]["code"] == "PAYLOAD_TOO_LARGE"
    assert world.executed == []

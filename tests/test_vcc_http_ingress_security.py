"""Public course request limits, without AWS, sockets, or database access."""

import base64
from dataclasses import replace
import json

import pytest

from mock_journey import course_http
from mock_journey.course_contracts import PUBLIC_ID_MAX
from tests.vcc_api_support import World, decode, event


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


# -- measurement upload: base64 checked in slices, decoded once by the parser (C-08) ----

ATTEMPT_ID = "50000000-0000-4000-8000-000000000001"  # World's course attempt


def _verdict(call, text):
    try:
        call(text)
    except (ValueError, TypeError) as error:
        return type(error).__name__
    return "ok"


BASE64_EDGE_CASES = [
    "", "A", "AA", "AAA", "AAAA", "AA==", "AAA=", "AAAA=", "AAAA==", "AAAAA", "AAAAAA", "AAAAAA=", "AAAAAAA=",
    "AAAAAAAA", "AA=A", "AA==AA==", "A===", "====", "=", "AAAA\n", " AAAA", "AAAA ", "가", "AAAAAA==",
    "AAAAAAA==", "AAAA=A", "AAAA=AAA", "AAAAAAAA=", "AAAAAAAAA", "AAAAAAAAAA==", "AAAA" * 3 + "AA=",
    "AAAA" * 3 + "A", "AAAA" * 3 + "=", "AAAA" * 4 + "==",
]


@pytest.mark.parametrize("slice_size", [4, 8, 12])
def test_sliced_base64_check_matches_the_validating_decoder(monkeypatch, slice_size):
    import random
    monkeypatch.setattr(course_http, "_BASE64_SLICE", slice_size)
    rng = random.Random(11)
    alphabet = "ABab09+/=\n -가"
    samples = list(BASE64_EDGE_CASES)
    samples += ["".join(rng.choice(alphabet) for _ in range(rng.randint(0, 30))) for _ in range(3000)]
    samples += [base64.b64encode(bytes(rng.randrange(256) for _ in range(n))).decode() for n in range(0, 40)]
    for text in samples:
        expected = _verdict(lambda value: base64.b64decode(value, validate=True), text)
        assert _verdict(course_http.check_base64, text) == expected, repr(text)
    # Negative control: a check that only looked at the last slice would accept this.
    assert _verdict(course_http.check_base64, "AAA=" + "AAAA" * 3) != "ok"


def test_measurement_upload_is_checked_without_holding_the_decoded_body(monkeypatch):
    world = World()
    world.calculations[ATTEMPT_ID]["state"] = "queued"
    received = []
    submit = world.http._hooks.measurement_submit
    world.http._hooks = replace(world.http._hooks,
                                measurement_submit=lambda auth, ident, request: received.append(request)
                                or submit(auth, ident, request))
    decoded_lengths = []
    original = base64.b64decode

    def spy(value, *args, **kwargs):
        decoded_lengths.append(len(value))
        return original(value, *args, **kwargs)

    monkeypatch.setattr(course_http.base64, "b64decode", spy)
    body = base64.b64encode(b"x" * (3 * course_http._BASE64_SLICE + 1)).decode()
    request = event("POST", f"/api/v2/attempts/{ATTEMPT_ID}/calculation/", raw=body,
                    headers={"Content-Type": "multipart/form-data"})
    request["isBase64Encoded"] = True
    response = world.http.dispatch(request)
    assert response["statusCode"] == 202
    assert world.executed == ["measure"]
    assert decoded_lengths and max(decoded_lengths) <= course_http._BASE64_SLICE
    # The hook receives the event as sent: the transport body is still base64 text.
    assert received[0]["body"] == body and received[0]["isBase64Encoded"] is True


def test_invalid_base64_measurement_is_400_before_the_hook():
    world = World()
    for bad in ("not base64!", "AAAAA", "AAA=" + "AAAA" * 20000):
        request = event("POST", f"/api/v2/attempts/{ATTEMPT_ID}/calculation/", raw=bad,
                        headers={"Content-Type": "multipart/form-data"})
        request["isBase64Encoded"] = True
        response = world.http.dispatch(request)
        assert response["statusCode"] == 400, bad[:20]
        assert decode(response)["error"]["code"] == "INVALID_REQUEST"
    assert world.executed == []

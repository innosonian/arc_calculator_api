"""Non-socket counterexamples for explicit /api/v2 measurement wire transport.

The parent integration suite owns real HTTP/DynamoDB execution. Here Waitress
parsers and the WSGI adapter are driven directly; the real v2 router runs with
scripted hooks. No listener, AWS client or worker is started.
"""

import base64
import io
import json
from types import SimpleNamespace
from urllib.parse import quote, urlencode

import pytest

import local_server.http as transport
from local_server_tests.course_stub import ATTEMPT, StubCourseService, calculation_path
from mock_journey.legacy_bridge import parse_measurement
from tests._synth import comp_session, condition_json, multipart_event


PATH = calculation_path(ATTEMPT)
TOKEN = "synthetic-session"
WIRE_LIMIT = 128 * 1024  # Test-only limits; no runtime policy.


def application(*, limit=WIRE_LIMIT):
    service = StubCourseService(payload_limit=4 * ((WIRE_LIMIT + 2) // 3), token=TOKEN)
    app = transport.make_application(service, lambda: True, "127.0.0.1", 8000,
                                     ("127.0.0.1",), calculation_body_limit=limit)
    return app, service


def request(app, path=PATH, *, body=b"{}", content_type="application/json", **changes):
    environ = {
        "REMOTE_ADDR": "127.0.0.1", "HTTP_HOST": "127.0.0.1:8000",
        "REQUEST_URI": path, "PATH_INFO": path, "QUERY_STRING": "",
        "REQUEST_METHOD": "POST", "CONTENT_LENGTH": str(len(body)),
        "wsgi.input": io.BytesIO(body), "HTTP_AUTHORIZATION": "Bearer " + TOKEN,
    }
    if content_type is not None:
        environ["CONTENT_TYPE"] = content_type
    environ.update(changes)
    response = {}

    def start(status, headers):
        response.update(status=int(status.split()[0]), headers=dict(headers))

    response["body"] = b"".join(app(environ, start))
    return response


def test_raw_multipart_encoded_once_and_existing_parser_restores_exact_binary():
    # Parser transport only: repeated fixture bytes are not a scoring scenario.
    raw = comp_session(60) * 5
    source = multipart_event({"rawHexBPfile": raw,
                              "condition": condition_json(training_type="compression_only", guideline="ARC2025")})
    wire = base64.b64decode(source["body"])
    assert len(wire) > transport.BODY_LIMIT
    app, service = application()
    response = request(app, body=wire, content_type=source["headers"]["Content-Type"])
    assert response["status"] == 202
    assert json.loads(response["body"])["data"]["calculationStatus"] == "pending"
    event = service.dispatched[0]
    assert event["isBase64Encoded"] is True
    assert base64.b64decode(event["body"]) == wire
    decoded = parse_measurement(event)
    assert decoded["cpr_b64_data"] == raw
    assert decoded["condition"]["guideline"] == "ARC2025"
    assert event["headers"] == {"Host": "127.0.0.1:8000", "Content-Type": source["headers"]["Content-Type"],
                                "Authorization": "Bearer " + TOKEN}
    assert service.calls[-1] == ("submit", ATTEMPT)


@pytest.mark.parametrize("content_type", [None, "application/json", "application/x-www-form-urlencoded"])
def test_encoded_form_remains_exact_text_without_double_encoding(content_type):
    raw = comp_session(2)
    encoded = quote(urlencode({
        "cpr_b64_data": base64.urlsafe_b64encode(raw).decode(),
        "condition": condition_json(training_type="compression_only", guideline="ARC2025"),
        "Custom": json.dumps({"PassThreshold": 80.0, "CertificateAdult": False}),
    }), safe="")
    wire = base64.urlsafe_b64encode(encoded.encode())
    app, service = application()
    assert request(app, body=wire, content_type=content_type)["status"] == 202
    event = service.dispatched[0]
    assert event["isBase64Encoded"] is False
    assert event["body"] == wire.decode("ascii")
    decoded = parse_measurement(event)
    assert decoded["cpr_b64_data"] == raw
    assert type(decoded["Custom"]["PassThreshold"]) is float
    assert decoded["Custom"]["CertificateAdult"] is False


def test_exact_wire_limit_and_one_byte_more():
    app, service = application()
    assert request(app, body=b"A" * WIRE_LIMIT)["status"] == 202
    assert request(app, body=b"A" * (WIRE_LIMIT + 1))["status"] == 413
    assert len(service.dispatched) == 1


@pytest.mark.parametrize("path", ["/api/v2/sessions/", "/api/v2/attempts/", "/unrecognized",
                                  PATH.replace(ATTEMPT, ATTEMPT.upper()), PATH.rstrip("/"),
                                  PATH + "extra/", f"/api/v2/attempts/{ATTEMPT}/"])
def test_large_calculation_limit_does_not_enlarge_control_or_unknown_routes(path):
    app, service = application()
    response = request(app, path, body=b"A" * (transport.BODY_LIMIT + 1))
    assert response["status"] == 413
    assert not service.dispatched


def test_large_calculation_limit_applies_only_to_post():
    app, service = application()
    response = request(app, body=b"A" * (transport.BODY_LIMIT + 1), REQUEST_METHOD="PUT")
    assert response["status"] == 413
    assert not service.dispatched


@pytest.mark.parametrize("content_type", [None, "multipart/form-data; boundary=demo", "text/plain"])
def test_default_transport_does_not_enable_calculation_or_loosen_json_control(content_type):
    app, service = application(limit=None)
    assert request(app, body=b"A", content_type=content_type)["status"] == 400
    assert request(app, body=b"A" * (transport.BODY_LIMIT + 1))["status"] == 413
    assert not service.dispatched


@pytest.mark.parametrize("changes,status", [
    ({"REMOTE_ADDR": "192.168.1.224"}, 404),
    ({"HTTP_HOST": "evil.invalid:8000"}, 400),
    ({"HTTP_ORIGIN": "null"}, 400),
    ({"HTTP_SEC_FETCH_SITE": "cross-site"}, 400),
    ({"REQUEST_URI": PATH.replace("calculation", "calcul%61tion")}, 400),
    ({"QUERY_STRING": "secret-marker=%31", "REQUEST_URI": PATH + "?secret-marker=%31"}, 400),
    ({"HTTP_TRANSFER_ENCODING": ""}, 400),
    ({"HTTP_CONTENT_ENCODING": "gzip"}, 400),
    ({"HTTP_AUTHORIZATION": "Bearer first, Bearer second"}, 401),
    ({"CONTENT_LENGTH": "7", "wsgi.input": io.BytesIO(b"short")}, 400),
])
def test_calculation_retains_origin_host_framing_and_header_guards(changes, status):
    app, service = application()
    response = request(app, **changes)
    assert response["status"] == status
    assert not service.dispatched
    assert b"secret-marker" not in response["body"]


def test_unexpected_query_on_upload_is_rejected_by_the_v2_allowlist_without_submitting():
    app, service = application()
    response = request(app, QUERY_STRING="secret-marker=1", REQUEST_URI=PATH + "?secret-marker=1")
    assert response["status"] == 400
    assert json.loads(response["body"])["error"]["code"] == "INVALID_REQUEST"
    assert b"secret-marker" not in response["body"]
    assert ("submit", ATTEMPT) not in service.calls


@pytest.mark.parametrize("limit", [True, False, 0, -1, 1.0, "131072"])
def test_untyped_or_nonpositive_wire_limit_is_rejected(limit):
    with pytest.raises(ValueError):
        application(limit=limit)


def test_encoded_event_cap_must_cover_multipart_wire_cap():
    for service in (StubCourseService(), StubCourseService(payload_limit=WIRE_LIMIT),
                    StubCourseService(payload_limit=4 * ((WIRE_LIMIT + 2) // 3) - 1)):
        with pytest.raises(ValueError, match="encoded wire-body"):
            transport.make_application(service, lambda: True, "127.0.0.1", 8000,
                                       ("127.0.0.1",), calculation_body_limit=WIRE_LIMIT)


def test_status_does_not_claim_unsupervised_worker_is_available():
    app, _ = application()
    response = request(app, "/", body=b"", REQUEST_METHOD="GET")
    status = json.loads(response["body"])
    assert list(status) == ["service", "mode", "calculator_available", "login_path", "programs_path",
                            "calculation_transport_configured"]
    assert status["mode"] == "course_v2"
    assert status["calculation_transport_configured"] is True
    assert status["calculator_available"] is False


def parser_factory(monkeypatch, app):
    from waitress.adjustments import Adjustments
    import waitress.server

    options = {}

    def without_socket(application, **kwargs):
        options.update(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr(waitress.server, "create_server", without_socket)
    server = transport.create_server(app, "127.0.0.1", 8000)
    return server.channel_class.parser_class, Adjustments(**options)


@pytest.mark.parametrize("path,limit", [(PATH, WIRE_LIMIT), (PATH + "?x=1", WIRE_LIMIT),
                                        ("/api/v2/sessions/", transport.BODY_LIMIT),
                                        (PATH.rstrip("/"), transport.BODY_LIMIT),
                                        ("/unrecognized", transport.BODY_LIMIT)])
def test_waitress_rejects_route_overflow_before_buffering_body(monkeypatch, path, limit):
    app, _ = application()
    parser_type, shared = parser_factory(monkeypatch, app)
    parser = parser_type(shared)
    header = (f"POST {path} HTTP/1.1\r\nHost: 127.0.0.1:8000\r\n"
              f"Content-Length: {limit + 1}\r\n\r\n").encode()
    assert parser.received(header) == len(header)
    assert parser.completed and parser.error.code == 413
    assert parser.body_bytes_received == 0
    assert len(parser.body_rcv.getbuf()) == 0
    assert shared.max_request_body_size == WIRE_LIMIT + 1
    assert parser.adj is not shared
    parser.close()


def test_waitress_per_request_cap_does_not_mutate_next_control_parser(monkeypatch):
    app, _ = application()
    parser_type, shared = parser_factory(monkeypatch, app)
    for path, size, rejected in ((PATH, WIRE_LIMIT, False),
                                 ("/api/v2/sessions/", transport.BODY_LIMIT + 1, True)):
        parser = parser_type(shared)
        header = (f"POST {path} HTTP/1.1\r\nHost: 127.0.0.1:8000\r\n"
                  f"Content-Length: {size}\r\n\r\n").encode()
        parser.received(header)
        assert bool(parser.error) is rejected
        if not rejected:
            assert parser.received(b"A" * size) == size
            assert parser.completed and parser.error is None
        assert parser.connection_close is True
        parser.close()


@pytest.mark.parametrize("field", [b"Transfer-Encoding: chunked", b"Transfer-Encoding:",
                                   b"Content-Length: " + b"9" * 5000,
                                   b"Content-Length: 1\r\nContent-Length: 1"])
def test_waitress_calculation_parser_sanitizes_bad_framing_without_body(monkeypatch, field):
    app, _ = application()
    parser_type, shared = parser_factory(monkeypatch, app)
    parser = parser_type(shared)
    parser.received(f"POST {PATH} HTTP/1.1\r\nHost: 127.0.0.1:8000\r\n".encode() + field + b"\r\n\r\n")
    assert parser.completed and parser.error.code == 400
    assert parser.error.body == "Invalid request headers."
    assert parser.body_bytes_received == 0
    parser.close()

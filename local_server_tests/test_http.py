"""Real socket tests of the local /api/v2 HTTP boundary, not database semantics."""

import contextlib
import http.client
import io
import json
import socket
import threading
from types import SimpleNamespace

import pytest

from local_server.http import BODY_LIMIT, create_server, make_application
from local_server_tests.course_stub import LOGIN, SESSION_ID, TOKEN, StubCourseService, calculation_path
from tests.loopback_port_support import unused_loopback_port as unused_port

# Real loopback sockets: opted out of the directory network guard (conftest.py).
pytestmark = pytest.mark.loopback


LOGIN_PATH = b"/api/v2/sessions/"


@contextlib.contextmanager
def running_gateway(*, application_host="127.0.0.1", clients=("127.0.0.1",)):
    port = unused_port()
    service = StubCourseService()
    ready = [True]
    app = make_application(service, lambda: ready[0], application_host, port, clients)
    server = create_server(app, "127.0.0.1", port)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(port=port, service=service, ready=ready, server=server)
    finally:
        from waitress import wasyncore
        # Close descriptors on their event-loop thread, avoiding select() on a
        # descriptor concurrently closed by the test runner.
        server.trigger.pull_trigger(lambda: wasyncore.close_all(map=server._map))
        thread.join(timeout=3)
        server.task_dispatcher.shutdown(timeout=2)
        assert not thread.is_alive()


@pytest.fixture
def serving():
    with running_gateway() as running:
        yield running


def call(running, path="/", method="GET", body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", running.port, timeout=3)
    conn.request(method, path, body=body, headers=headers or {})
    response = conn.getresponse()
    status, fields, data = response.status, dict(response.getheaders()), response.read()
    conn.close()
    return status, fields, data


def raw(running, request, *, source=None):
    with socket.socket() as sock:
        sock.settimeout(3)
        if source:
            sock.bind((source, 0))
        sock.connect(("127.0.0.1", running.port))
        sock.sendall(request)
        result = b""
        while True:
            data = sock.recv(64 * 1024)
            if not data:
                return result
            result += data


def framed(running, extra=b"", *, path=LOGIN_PATH, body=b"{}", method=b"POST", version=b"1.1"):
    return (method + b" " + path + b" HTTP/" + version + b"\r\n"
            + f"Host: 127.0.0.1:{running.port}\r\n".encode()
            + b"Content-Type: application/json\r\n" + extra + b"\r\n" + body)


def test_status_health_and_handler_types(serving):
    status, headers, body = call(serving)
    assert status == 200
    # Exact keys and order without calculation transport or supervised worker.
    assert list(json.loads(body)) == ["service", "mode", "calculator_available", "login_path", "programs_path"]
    assert json.loads(body) == {"service": "arc-local-api", "mode": "course_v2", "calculator_available": False,
                                "login_path": "/api/v2/sessions/", "programs_path": "/api/v2/courses/progress/"}
    assert headers["Cache-Control"] == "no-store"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Connection"] == "close"
    assert "Server" not in headers
    status, _, body = call(serving, "/api/v2/sessions/", "POST", json.dumps(LOGIN), {"Content-Type": "application/json"})
    assert status == 201
    value = json.loads(body)
    assert value["success"] is True
    assert value["data"]["accessToken"] == TOKEN and value["data"]["sessionId"] == SESSION_ID
    assert serving.service.calls == [("login", LOGIN)]
    serving.ready[0] = False
    status, _, body = call(serving, "/healthz")
    assert status == 503
    assert json.loads(body)["error"]["code"] == "TEMPORARILY_UNAVAILABLE"


@pytest.mark.parametrize("te", [b"", b",", b", ,", b"chunked", b"gzip", b"chunked, chunked"])
@pytest.mark.parametrize("cl", [b"", b"Content-Length: 2\r\n"])
@pytest.mark.parametrize("protocol", [b"1.0", b"1.1"])
def test_any_transfer_encoding_is_rejected_before_handler(serving, te, cl, protocol):
    response = raw(serving, framed(serving, b"Transfer-Encoding: " + te + b"\r\n" + cl, version=protocol))
    assert b" 400 " in response.split(b"\r\n", 1)[0]
    assert serving.service.dispatched == []


@pytest.mark.parametrize("headers", [
    b"Content-Length: 2\r\nContent-Length: 2\r\n",
    b"Content-Length: 2\r\nContent-Length: 99\r\n",
    b"Content-Length: +2\r\n", b"Content-Length: -2\r\n",
    b"Content-Length: 2.0\r\n",
])
def test_bad_length_never_reaches_handler(serving, headers):
    response = raw(serving, framed(serving, headers))
    assert b" 400 " in response.split(b"\r\n", 1)[0]
    assert serving.service.dispatched == []


@pytest.mark.parametrize("extra", [
    b"Authorization: Bearer first\r\nAuthorization: Bearer second\r\n",
    b"Content-Type: application/json\r\n",
    b"Host: attacker.example\r\n",
])
def test_duplicate_security_headers_are_not_collapsed_to_one_value(serving, extra):
    response = raw(serving, framed(serving, extra + b"Content-Length: 2\r\n"))
    assert any(code in response.split(b"\r\n", 1)[0] for code in (b" 400 ", b" 401 "))
    assert serving.service.dispatched == []


@pytest.mark.parametrize("path", [
    b"//api/v2/sessions/", b"/api//v2/sessions/", b"/api%2fv2/sessions/",
    b"/api/v2/sessions/?", b"/api/v2/sessions/#fragment", b"http://127.0.0.1/api/v2/sessions/",
    b"/api\\v2/sessions/", b"/api/v2/%73essions/",
    b"/api/v2/courses/progress/?page=%31", b"/api/v2/courses/progress/?page=1&page=1",
    b"/api/v2/courses/progress/?page=1+1", b"/api/v2/courses/progress/?page",
    b"/api/v2/courses/progress/?page=1#x", b"/api/v2/courses/progress/?page=1?",
])
def test_original_uri_is_not_normalized_into_accepted_route(serving, path):
    response = raw(serving, framed(serving, b"Content-Length: 2\r\n", path=path))
    assert b" 400 " in response.split(b"\r\n", 1)[0]
    assert serving.service.dispatched == []


@pytest.mark.parametrize("path", ["/healthz?probe=1", "/?probe=1", "/unrecognized?probe=1"])
def test_query_is_admitted_only_under_api_v2(serving, path):
    status, _, body = call(serving, path)
    assert status == 400 and json.loads(body)["error"]["code"] == "INVALID_REQUEST"
    assert b"probe" not in body and serving.service.dispatched == []


def test_query_on_login_reaches_only_the_v2_allowlist_and_never_logs_in(serving):
    secret = b"PRIVATE-QUERY-MARKER"
    response = raw(serving, framed(serving, b"Content-Length: 2\r\n", path=LOGIN_PATH + b"?token=" + secret))
    assert b" 400 " in response.split(b"\r\n", 1)[0]
    assert secret not in response
    assert serving.service.calls == []
    # The accepted query is parsed once into both REST proxy representations.
    assert serving.service.dispatched[0]["queryStringParameters"] == {"token": secret.decode()}
    assert serving.service.dispatched[0]["multiValueQueryStringParameters"] == {"token": [secret.decode()]}


@pytest.mark.parametrize("headers", [
    {"Host": "attacker.example"}, {"Origin": "http://attacker.example"},
    {"Origin": ""}, {"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"},
])
def test_browser_and_host_guards(serving, headers):
    status, _, body = call(serving, headers=headers)
    assert status == 400
    assert json.loads(body)["error"]["code"] == "INVALID_REQUEST"


def test_actual_peer_is_used_not_forwarded_headers(serving):
    # A forged disallowed address cannot replace an allowed actual peer.
    status, _, _ = call(serving, headers={"X-Forwarded-For": "203.0.113.55"})
    assert status == 200
    # Keep the socket physically on loopback while configuring a LAN admission
    # policy. This tests an actual denied TCP peer without an OS-specific alias.
    with running_gateway(application_host="192.168.50.10", clients=("192.168.50.20",)) as private:
        status, _, _ = call(private, "/api/v2/sessions/", "POST", json.dumps(LOGIN), headers={
            "Host": f"192.168.50.10:{private.port}", "Content-Type": "application/json",
            "X-Forwarded-For": "192.168.50.20",
            "Forwarded": 'for=192.168.50.20;host="192.168.50.10"',
        })
        assert status == 404
        assert private.service.dispatched == [] and private.service.calls == []


@pytest.mark.parametrize("content_type", ["text/plain", "application/x-www-form-urlencoded",
                                          "application/json; charset=latin1", "application/json, application/json"])
def test_only_control_json_is_accepted(serving, content_type):
    status, _, _ = call(serving, "/api/v2/sessions/", "POST", "{}", {"Content-Type": content_type})
    assert status == 400
    assert serving.service.dispatched == []


def test_invalid_utf8_get_body_and_large_body(serving):
    assert call(serving, "/api/v2/sessions/", "POST", b"\xff", {"Content-Type": "application/json"})[0] == 400
    assert call(serving, "/", "GET", b"{}")[0] == 400
    assert call(serving, "/api/v2/session/", "DELETE", b"{}", {"Authorization": "Bearer " + TOKEN})[0] == 400
    assert call(serving, "/api/v2/sessions/", "POST", b"x" * (BODY_LIMIT + 1), {"Content-Type": "application/json"})[0] == 413
    assert serving.service.dispatched == []


def test_malformed_secret_header_is_not_reflected_or_logged(serving, capsys):
    secret = b"UNIQUE-SECRET-DO-NOT-LOG"
    response = raw(serving, framed(serving, b"Bad: " + secret + b"\nmalformed\r\nContent-Length: 2\r\n"))
    assert b" 400 " in response.split(b"\r\n", 1)[0]
    assert secret not in response
    captured = capsys.readouterr()
    assert secret.decode() not in captured.out + captured.err
    assert serving.service.dispatched == []


def test_pipelined_second_request_is_never_dispatched(serving):
    request = framed(serving, b"Content-Length: 2\r\n")
    response = raw(serving, request + request)
    assert response.count(b"HTTP/1.1") == 1
    assert len(serving.service.dispatched) == 1


@pytest.mark.parametrize("method", ["PATCH", "HEAD", "OPTIONS", "TRACE"])
def test_only_v2_methods_reach_the_router(serving, method):
    status, _, _ = call(serving, "/api/v2/session/", method, headers={"Authorization": "Bearer " + TOKEN})
    assert status in (400, 404)
    assert serving.service.dispatched == []


def test_forwarded_event_carries_only_allowlisted_headers(serving):
    status, _, body = call(serving, "/api/v2/session/", headers={
        "Authorization": "Bearer " + TOKEN, "X-Attempt-ID": "a5917021-0db0-45ed-9c8c-97d31e43a652",
        "X-Forwarded-For": "203.0.113.55", "Cookie": "PRIVATE-COOKIE",
    })
    assert status == 200 and json.loads(body)["data"]["sessionId"] == SESSION_ID
    event = serving.service.dispatched[0]
    assert event["headers"] == {"Host": f"127.0.0.1:{serving.port}", "Authorization": "Bearer " + TOKEN}
    assert event["multiValueHeaders"] == {name: [value] for name, value in event["headers"].items()}
    assert (event["httpMethod"], event["path"], event["isBase64Encoded"]) == ("GET", "/api/v2/session/", False)


def test_measurement_route_without_calculation_transport_stays_disabled(serving):
    headers = {"Authorization": "Bearer " + TOKEN, "Content-Type": "multipart/form-data; boundary=demo"}
    # Without an explicit wire limit the upload route keeps the control body
    # limit and JSON-only rule: no measurement reaches the application.
    status, _, _ = call(serving, calculation_path(), "POST", b"A" * (BODY_LIMIT + 1), headers)
    assert status == 413
    status, _, body = call(serving, calculation_path(), "POST", b"--demo--\r\n", headers)
    assert status == 400 and json.loads(body)["error"]["code"] == "INVALID_REQUEST"
    assert serving.service.dispatched == [] and serving.service.calls == []


@pytest.mark.parametrize("host", ["0.0.0.0", "8.8.8.8", "localhost", "::1", "127.0.0.2", "192.168.001.1"])
def test_factory_refuses_wildcard_nonliteral_and_nonlocal_addresses(host):
    with pytest.raises(ValueError):
        create_server(lambda *_: [], host, 8000)


@pytest.mark.parametrize("service", [
    SimpleNamespace(), SimpleNamespace(course_mode="course_v2"),
    SimpleNamespace(course_mode="course_v2", course_http=None),
    SimpleNamespace(course_mode="journey", course_http=StubCourseService()),
])
def test_factory_serves_only_the_assembled_course_application(service):
    with pytest.raises(ValueError, match="/api/v2"):
        make_application(service, lambda: True, "127.0.0.1", 8000, ["127.0.0.1"])


def test_no_global_waitress_parser_change(serving):
    from waitress.channel import HTTPChannel
    from waitress.parser import HTTPRequestParser
    assert HTTPChannel.parser_class is HTTPRequestParser
    assert serving.server.channel_class.parser_class is not HTTPRequestParser


def test_wsgi_response_exception_is_generic_and_health_is_fail_closed():
    app = make_application(StubCourseService(), lambda: (_ for _ in ()).throw(RuntimeError("private-secret")),
                           "127.0.0.1", 8000, ["127.0.0.1"])
    environ = {"REMOTE_ADDR": "127.0.0.1", "HTTP_HOST": "127.0.0.1:8000", "REQUEST_URI": "/healthz", "PATH_INFO": "/healthz", "REQUEST_METHOD": "GET", "wsgi.input": io.BytesIO()}
    statuses = []
    result = b"".join(app(environ, lambda status, headers: statuses.append(status)))
    assert statuses == ["503 Service Unavailable"]
    assert b"private-secret" not in result

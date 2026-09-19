"""Loopback HTTP framing for course_v2: old routes 404, PUT/query allowlist, alias gone."""

import contextlib
import http.client
import json
import socket
import threading
from types import SimpleNamespace

import pytest

from local_server.http import BODY_LIMIT, create_server, make_application
from mock_journey.course_errors import CourseError
from mock_journey.course_http import CourseHttp
from mock_journey.course_settings import fixture_course_settings
from mock_journey.course_wiring import COURSE_MODE
from tests.vcc_support import OLD_ROUTES


CLOCK = lambda: 1_800_000_000


class CourseV2Service:
    course_mode = COURSE_MODE

    def __init__(self):
        self.operations = None
        self.calculation = SimpleNamespace(payload_limit=4 * ((BODY_LIMIT + 2) // 3))
        settings = fixture_course_settings()
        self.course_http = CourseHttp(
            SimpleNamespace(), settings, clock=CLOCK,
            uuid_factory=lambda: "61000000-0000-4000-8000-000000000001",
            authenticate=lambda *a, **k: (_ for _ in ()).throw(CourseError("SESSION_REQUIRED")),
            login=lambda *a, **k: (_ for _ in ()).throw(CourseError("LOGIN_FAILED")),
        )


def unused_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextlib.contextmanager
def serving():
    port = unused_port()
    service = CourseV2Service()
    app = make_application(service, lambda: True, "127.0.0.1", port, ("127.0.0.1",))
    server = create_server(app, "127.0.0.1", port)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(port=port, service=service, server=server)
    finally:
        from waitress import wasyncore
        server.trigger.pull_trigger(lambda: wasyncore.close_all(map=server._map))
        thread.join(timeout=3)
        server.task_dispatcher.shutdown(timeout=2)
        assert not thread.is_alive()


def call(running, path, method="GET", body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", running.port, timeout=3)
    conn.request(method, path, body=body, headers=headers or {})
    response = conn.getresponse()
    status, data = response.status, response.read()
    conn.close()
    return status, data


@pytest.fixture
def running():
    with serving() as value:
        yield value


@pytest.mark.parametrize("method,path", OLD_ROUTES)
def test_course_v2_legacy_paths_are_404(running, method, path):
    headers = {"Content-Type": "application/json"} if method in ("POST", "PUT") else {}
    body = b"{}" if method in ("POST", "PUT") else None
    status, data = call(running, path, method=method, body=body, headers=headers)
    assert status == 404
    payload = json.loads(data)
    assert payload["success"] is False
    assert payload["error"]["code"] == "NOT_FOUND"


def test_course_v2_allows_put_and_list_query(running):
    status, data = call(
        running, "/api/v2/courses/progress/?page=1&pageSize=10", method="GET",
        headers={"Authorization": "Bearer not-a-token"},
    )
    assert status == 401
    payload = json.loads(data)
    assert payload["success"] is False
    assert payload["error"]["code"] == "SESSION_REQUIRED"
    status, data = call(
        running, "/api/v2/courses/101/progress/", method="PUT",
        body=b'{"enrollmentId":501,"courseItemLinkId":1001,"startId":"40000000-0000-4000-8000-000000000001","reportId":"20000000-0000-4000-8000-000000000001","contentVersion":"v1","event":{"type":"video_segments","intervalsMs":[[0,1]]}}',
        headers={"Content-Type": "application/json", "Authorization": "Bearer not-a-token"},
    )
    assert status == 401


def test_course_v2_does_not_redirect_missing_slash(running):
    status, data = call(running, "/api/v2/session")
    assert status == 404
    assert b"Location" not in data


def test_course_v2_rejects_unknown_query(running):
    status, data = call(
        running, "/api/v2/courses/progress/?secret=1", method="GET",
        headers={"Authorization": "Bearer not-a-token"},
    )
    assert status == 400
    assert b"secret" not in data


def test_health_and_slash_remain(running):
    status, data = call(running, "/healthz")
    assert status == 200
    body = json.loads(data)
    assert body["mode"] == "course_v2"
    assert body["login_path"] == "/api/v2/sessions/"

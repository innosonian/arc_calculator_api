"""Independent L2 chart security over real TCP and the private product store.

This explicitly assembled transport fixture is not the default CLI, a worker,
or a DynamoDB logout test. The chart signing, files, parser and socket are real.
"""

import contextlib
import http.client
import json
import socket
import threading
import time
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from local_server.database import prepare_material
from local_server.cli import installation_lock
from local_server.http import BODY_LIMIT, HEADER_LIMIT, create_server, make_application
from mock_journey import typed
from mock_journey.errors import JourneyError
from util.uploader import build_key_stem, date_prefix


ARTIFACT_LIMIT = 256 * 1024
RESPONSE_LIMIT = ARTIFACT_LIMIT + HEADER_LIMIT
DIRECTORY = "calculator_result/interpreted_rtdata/arc"
STAGE = "local-chart-test"
SESSION = "http-chart-test-session"
SECRET = "CHART-PRIVATE-MARKER-DO-NOT-LOG"


class _ControlService:
    def __init__(self):
        self.revoked = False
        self.auth_calls, self.logout_calls = [], []
        self.snapshot = b'{"integer":80,"float":80.0,"nullable":null}'
        self.auth = SimpleNamespace(authenticate=self.authenticate)
        self.state = SimpleNamespace(logout=self.logout)
        self.calculation = SimpleNamespace(payload_limit=100_000, result=self.result)

    def authenticate(self, token, **kwargs):
        self.auth_calls.append(token)
        if token != SESSION or self.revoked:
            raise JourneyError("SESSION_REQUIRED")
        return "test-authenticated"

    def logout(self, auth):
        self.logout_calls.append(auth)
        self.revoked = True

    def result(self, auth, ident):
        return 200, self.snapshot

    def require_calculation(self):
        return self.calculation


def _port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextlib.contextmanager
def gateway(tmp_path, **options):
    with installation_lock(tmp_path / "installation") as data_dir:
        yield from _configured_gateway(data_dir, **options)


def _configured_gateway(data_dir, *, body=None,
                        application_host="127.0.0.1", clients=("127.0.0.1",)):
    from local_server.object_storage import prepare_object_material, LocalObjectClient
    from local_server.charts import LocalChartService

    if body is None:
        body = typed.json_bytes({"values": [1, 1.0, None], "label": "real-local-chart"})
    port = _port()
    material = prepare_material(data_dir)
    object_material = prepare_object_material(material)
    client = LocalObjectClient(object_material, bucket="local-chart-test", directory=DIRECTORY,
                               stage=STAGE, artifact_limit=ARTIFACT_LIMIT, quota_bytes=4 * 1024 * 1024)
    now = [int(time.time())]
    stem = build_key_stem()
    key = f"{DIRECTORY}/{STAGE}/_no_org/{date_prefix(stem)}/{stem}.json"
    client.put_object(Bucket="local-chart-test", Key=key, Body=body, Metadata={})
    charts = LocalChartService(client, base_url=f"http://{application_host}:{port}", clock=lambda: now[0])
    url = charts.create_signed_url(key, expires_in=300)
    service = _ControlService()
    app = make_application(service, lambda: True, application_host, port, clients,
                           chart_service=charts, response_body_limit=RESPONSE_LIMIT)
    server = create_server(app, "127.0.0.1", port)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(port=port, charts=charts, client=client, material=object_material, now=now,
                              key=key, url=url, path=urlsplit(url).path, body=body, app=app,
                              service=service, server=server)
    finally:
        from waitress import wasyncore
        server.trigger.pull_trigger(lambda: wasyncore.close_all(map=server._map))
        thread.join(timeout=3)
        server.task_dispatcher.shutdown(timeout=2)
        client.close()
        assert not thread.is_alive()


@pytest.fixture
def serving(tmp_path):
    with gateway(tmp_path) as value:
        yield value


def request(world, *, path=None, method="GET", body=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", world.port, timeout=3)
    try:
        connection.request(method, world.path if path is None else path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, {key.lower(): value for key, value in response.getheaders()}, response.read()
    finally:
        connection.close()


def raw(world, *, path=None, method="GET", extra=b"", body=b""):
    target = world.path if path is None else path
    wire = (f"{method} {target} HTTP/1.1\r\nHost: 127.0.0.1:{world.port}\r\n".encode()
            + extra + b"\r\n" + body)
    with socket.socket() as sock:
        sock.settimeout(3)
        sock.connect(("127.0.0.1", world.port))
        sock.sendall(wire)
        chunks = []
        while True:
            data = sock.recv(64 * 1024)
            if not data:
                return b"".join(chunks)
            chunks.append(data)


def assert_safe_error(response, *, statuses=(400, 404, 503)):
    status, headers, body = response
    assert status in statuses
    assert headers["cache-control"] == "no-store"
    assert headers["x-content-type-options"] == "nosniff"
    assert SECRET.encode() not in body
    assert DIRECTORY.encode() not in body
    assert b"Traceback" not in body


def test_real_chart_bytes_download_without_session_and_keep_secure_headers(serving):
    status, headers, body = request(serving)
    assert status == 200 and body == serving.body
    assert headers["content-type"] == "application/json"
    assert headers["cache-control"] == "no-store" and headers["x-content-type-options"] == "nosniff"
    assert headers["content-length"] == str(len(body)) and headers["connection"] == "close"
    assert not {"server", "location", "content-disposition", "set-cookie", "accept-ranges"}.intersection(headers)
    assert serving.service.auth_calls == []
    assert serving.key not in serving.url and DIRECTORY not in serving.url
    assert urlsplit(serving.url).query == ""


def test_expiry_at_exact_300_seconds_and_logout_does_not_revoke_chart_capability(serving):
    issued = serving.now[0]
    assert request(serving, path="/mock/v1/session", method="DELETE",
                   headers={"Authorization": "Bearer " + SESSION})[0] == 204
    assert serving.service.logout_calls == ["test-authenticated"]
    serving.now[0] = issued + 299
    assert request(serving)[0] == 200
    serving.now[0] = issued + 300
    assert_safe_error(request(serving), statuses=(404,))
    assert len(serving.service.auth_calls) == 1


@pytest.mark.parametrize("headers", [
    {"Host": "attacker.invalid"}, {"Origin": "http://attacker.invalid"}, {"Origin": ""},
    {"Origin": "null"}, {"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"},
    {"Content-Encoding": "gzip"}, {"Content-Encoding": ""},
    {"Range": "bytes=0-1"}, {"Range": ""}, {"Range": "bytes=0-1,3-4"},
])
def test_common_http_and_no_partial_download_guards_run_before_chart_read(serving, headers, monkeypatch):
    calls = []
    monkeypatch.setattr(serving.charts, "read_path", lambda path: calls.append(path) or serving.body)
    assert_safe_error(request(serving, headers=headers), statuses=(404,) if "Range" in headers else (400,))
    assert calls == []


@pytest.mark.parametrize("mutation", [
    lambda p: p + "?", lambda p: p + "?token=" + SECRET, lambda p: p + "#fragment",
    lambda p: p.replace("/local/", "//local/"), lambda p: p.replace("charts/", "charts//"),
    lambda p: p.replace("/local/", "/%6cocal/"), lambda p: p.replace("/local/", "/local\\"),
    lambda p: "http://127.0.0.1" + p,
])
def test_raw_uri_cannot_normalize_to_a_valid_chart(serving, mutation, monkeypatch):
    calls = []
    monkeypatch.setattr(serving.charts, "read_path", lambda path: calls.append(path) or serving.body)
    response = raw(serving, path=mutation(serving.path))
    assert b" 400 " in response.split(b"\r\n", 1)[0]
    assert SECRET.encode() not in response and serving.body not in response
    assert calls == []


@pytest.mark.parametrize("method", ["POST", "DELETE", "HEAD", "OPTIONS", "PUT", "PATCH"])
def test_chart_capability_only_authorizes_get(serving, method, monkeypatch):
    calls = []
    monkeypatch.setattr(serving.charts, "read_path", lambda path: calls.append(path) or serving.body)
    status, _, body = request(serving, method=method, headers={"Content-Type": "application/json"})
    assert status in (400, 404) and serving.body not in body
    assert calls == []


@pytest.mark.parametrize("extra", [
    b"Transfer-Encoding: chunked\r\n", b"Transfer-Encoding:\r\n",
    b"Content-Length: 0\r\nContent-Length: 0\r\n",
    b"Content-Length: 0\r\nContent-Length: 1\r\n",
    b"Content-Length: +0\r\n", b"Content-Length: 0.0\r\n",
    b"Host: attacker.invalid\r\n", b"Range: bytes=0-1\r\nRange: bytes=2-3\r\n",
])
def test_ambiguous_framing_and_headers_cannot_reach_chart(serving, extra, monkeypatch):
    calls = []
    monkeypatch.setattr(serving.charts, "read_path", lambda path: calls.append(path) or serving.body)
    response = raw(serving, extra=extra)
    assert (b" 404 " if extra.startswith(b"Range:") else b" 400 ") in response.split(b"\r\n", 1)[0]
    assert serving.body not in response and calls == []


def test_get_body_is_rejected_before_reader(serving, monkeypatch):
    calls = []
    monkeypatch.setattr(serving.charts, "read_path", lambda path: calls.append(path) or serving.body)
    assert_safe_error(request(serving, body=b"{}"), statuses=(400,))
    assert calls == []


@pytest.mark.parametrize("suffix", ["/extra", "/../secret.bin", ".json", "=" + SECRET])
def test_extra_path_or_noncanonical_token_never_serves_a_file(serving, suffix, capsys):
    assert_safe_error(request(serving, path=serving.path + suffix), statuses=(404,))
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err and serving.path not in captured.out + captured.err


def test_changed_capability_is_not_accepted_and_not_echoed_or_logged(serving, capsys):
    mutated = serving.path[:-1] + ("A" if serving.path[-1] != "A" else "B")
    assert_safe_error(request(serving, path=mutated), statuses=(404,))
    captured = capsys.readouterr()
    assert serving.path not in captured.out + captured.err and mutated not in captured.out + captured.err


def test_rebuilt_chart_service_reads_persisted_key_and_preserves_remaining_expiry(serving, monkeypatch):
    from local_server.object_storage import LocalObjectClient
    from local_server.charts import LocalChartService
    client = LocalObjectClient(serving.material, bucket="local-chart-test", directory=DIRECTORY,
                               stage=STAGE, artifact_limit=ARTIFACT_LIMIT, quota_bytes=4 * 1024 * 1024)
    recreated = LocalChartService(client, base_url=f"http://127.0.0.1:{serving.port}", clock=lambda: serving.now[0])
    monkeypatch.setattr(serving.charts, "read_path", recreated.read_path)
    try:
        serving.now[0] += 299
        assert request(serving)[2] == serving.body
        serving.now[0] += 1
        assert_safe_error(request(serving), statuses=(404,))
    finally:
        client.close()


def test_other_installation_cannot_read_original_capability(serving, tmp_path, monkeypatch):
    from local_server.object_storage import prepare_object_material, LocalObjectClient
    from local_server.charts import LocalChartService
    with installation_lock(tmp_path / "different-installation") as data_dir:
        material = prepare_object_material(prepare_material(data_dir))
        client = LocalObjectClient(material, bucket="local-chart-test", directory=DIRECTORY,
                                   stage=STAGE, artifact_limit=ARTIFACT_LIMIT, quota_bytes=4 * 1024 * 1024)
        try:
            client.put_object(Bucket="local-chart-test", Key=serving.key, Body=serving.body, Metadata={})
            different = LocalChartService(client, base_url=f"http://127.0.0.1:{serving.port}", clock=lambda: serving.now[0])
            monkeypatch.setattr(serving.charts, "read_path", different.read_path)
            assert_safe_error(request(serving), statuses=(404,))
        finally:
            client.close()


@pytest.mark.parametrize("suffix", [".bin", ".aed.bin", ".meta.json", ".request.json"])
def test_raw_and_diagnostic_objects_cannot_be_signed_or_downloaded(serving, suffix):
    key = serving.key[:-5] + suffix
    serving.client.put_object(Bucket="local-chart-test", Key=key, Body=SECRET.encode(), Metadata={})
    with pytest.raises((JourneyError, ValueError)):
        serving.charts.create_signed_url(key, expires_in=300)
    assert_safe_error(request(serving, path="/local/v1/charts/" + key), statuses=(404,))


def test_chart_token_grants_no_session_or_program_access(serving):
    token = serving.path.rsplit("/", 1)[-1]
    for path in ("/mock/v1/session", "/mock/v1/programs"):
        assert request(serving, path=path, headers={"Authorization": "Bearer " + token})[0] == 401
    assert request(serving)[0] == 200


def test_forwarded_headers_do_not_replace_actual_denied_tcp_peer(tmp_path):
    with gateway(tmp_path, application_host="192.168.50.10", clients=("192.168.50.20",)) as world:
        assert_safe_error(request(world, headers={"Host": f"192.168.50.10:{world.port}",
                                                  "X-Forwarded-For": "192.168.50.20",
                                                  "Forwarded": 'for=192.168.50.20'}), statuses=(404,))
        assert world.service.auth_calls == []


def test_reader_exception_is_a_safe_service_error_not_a_chart_success(serving, monkeypatch, capsys):
    def broken(path):
        raise RuntimeError(SECRET + path)
    monkeypatch.setattr(serving.charts, "read_path", broken)
    assert_safe_error(request(serving), statuses=(503,))
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err and serving.path not in captured.out + captured.err


def test_valid_token_with_unavailable_object_reader_has_the_same_public_404(serving):
    serving.client.close()
    assert_safe_error(request(serving), statuses=(404,))


def test_maximum_chart_and_large_calculation_output_never_spill_to_general_temp(tmp_path, monkeypatch):
    from waitress.buffers import TempfileBasedBuffer
    spill = []
    def forbidden(*args, **kwargs):
        spill.append(True)
        raise AssertionError("HTTP output attempted to spill to a general temporary file.")
    monkeypatch.setattr(TempfileBasedBuffer, "newfile", forbidden)
    prefix, suffix = b'{"values": "', b'"}'
    body = prefix + b"x" * (ARTIFACT_LIMIT - len(prefix) - len(suffix)) + suffix
    with gateway(tmp_path, body=body) as world:
        assert world.server.adj.outbuf_overflow > RESPONSE_LIMIT + HEADER_LIMIT
        assert request(world)[2] == body
        world.service.snapshot = b'{"values":"' + b"y" * (200 * 1024) + b'"}'
        status, _, response = request(world, path="/mock/v1/attempts/00000000-0000-0000-0000-000000000001/calculation",
                                      headers={"Authorization": "Bearer " + SESSION})
        assert status == 200 and len(response) > 64 * 1024
        assert json.loads(response)["submit_arc"]["status"] == "disabled"
        assert spill == []
        # This is a response cap, not a relaxation of the control input limit.
        assert request(world, path="/mock/v1/sessions", method="POST", body=b"x" * (BODY_LIMIT + 1),
                       headers={"Content-Type": "application/json"})[0] == 413


def test_oversized_composed_calculation_response_is_rejected_before_output(serving):
    serving.service.snapshot = b'{"private":"' + SECRET.encode() + b"x" * RESPONSE_LIMIT + b'"}'
    assert_safe_error(request(serving, path="/mock/v1/attempts/00000000-0000-0000-0000-000000000001/calculation",
                              headers={"Authorization": "Bearer " + SESSION}), statuses=(503,))


@pytest.mark.parametrize("limit", [None, True, 0, ARTIFACT_LIMIT, ARTIFACT_LIMIT + HEADER_LIMIT - 1])
def test_chart_configuration_requires_explicit_output_budget(serving, limit):
    with pytest.raises(ValueError):
        make_application(serving.service, lambda: True, "127.0.0.1", serving.port, ["127.0.0.1"],
                         chart_service=serving.charts, response_body_limit=limit)

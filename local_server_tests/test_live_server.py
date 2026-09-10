"""Acceptance through the real CLI, HTTP parser, and persistent DynamoDB Local.

This file deliberately lives outside tests/: the unit suite's AWS monkeypatch
must not be the reason the executable is safe. No application/test store is
injected. Every request in these tests crosses a real loopback TCP connection.
The prerequisites must be installed beforehand; tests never download software.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
import http.client
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]
PYTHON = Path(os.environ.get("ARC_LOCAL_TEST_PYTHON", ROOT / "var/local-python/bin/python"))
DYNAMODB_HOME = Path(os.environ.get("ARC_LOCAL_TEST_DYNAMODB_HOME", ROOT / "var/dynamodb-local-3.3.1"))
ATTEMPT_ID = "b1234567-1234-4234-9234-123456789abc"
MARKER = "PRIVATE-LOCAL-ACCEPTANCE-MARKER"


def require(condition, explanation):
    """Fail with a fixed explanation, never pytest's credential-rich operands."""
    if not condition:
        raise AssertionError(explanation)


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@dataclass
class Reply:
    status: int
    headers: dict
    body: bytes = field(repr=False)

    def json(self):
        try:
            return json.loads(self.body)
        except (ValueError, UnicodeError):
            raise AssertionError("Expected a JSON response; content suppressed.") from None


class LiveServer:
    def __init__(self, work, *, control_only=True):
        self.work = work.resolve()
        self.work.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.work.chmod(0o700)
        self.data = self.work / "state"
        self.port = free_port()
        self.db_port = free_port()
        while self.db_port == self.port:
            self.db_port = free_port()
        self.process = None
        self.logs = []
        self._log = None
        self.secrets = []
        self.control_only = control_only

    def command(self):
        return [str(PYTHON), str(ROOT / "scripts/serve_local.py"),
                "--host", "127.0.0.1", "--port", str(self.port),
                "--db-port", str(self.db_port), "--data-dir", str(self.data),
                "--dynamodb-home", str(DYNAMODB_HOME)] + (["--control-only"] if self.control_only else [])

    def launch(self):
        require(self.process is None, "Test attempted to launch twice.")
        log_path = self.work / f"run-{len(self.logs)}.log"
        self.logs.append(log_path)
        self._log = log_path.open("wb")
        log_path.chmod(0o600)
        # Pass inherited configuration intentionally: isolation belongs to the
        # actual CLI. No genuine credential values are printed or inspected.
        environment = dict(os.environ)
        environment["PYTHONUNBUFFERED"] = "1"
        self.process = subprocess.Popen(self.command(), cwd=ROOT, env=environment,
                                        stdout=self._log, stderr=subprocess.STDOUT,
                                        start_new_session=True)

    def start(self):
        self.launch()
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise AssertionError("Real local CLI exited before becoming ready; inspect private test log.")
            try:
                reply = self.request("GET", "/healthz")
                if reply.status == 200:
                    return
            except (OSError, http.client.HTTPException):
                pass
            time.sleep(0.1)
        raise AssertionError("Real HTTP/DB readiness did not succeed within the test startup budget.")

    def stop(self):
        process, self.process = self.process, None
        if process is not None:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
                try:
                    process.wait(timeout=12)
                except subprocess.TimeoutExpired:
                    # This group was created by this test; never target any
                    # pre-existing process or discover a PID from a port.
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=5)
        if self._log is not None:
            self._log.close()
            self._log = None

    def request(self, method, path, body=None, token=None, headers=None):
        request_headers = {} if headers is None else dict(headers)
        if body is not None and type(body) is not bytes:
            body = json.dumps(body, allow_nan=False).encode()
            request_headers.setdefault("Content-Type", "application/json")
        if token:
            request_headers["Authorization"] = "Bearer " + token
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=4)
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            return Reply(response.status, dict(response.getheaders()), response.read(64 * 1024))
        finally:
            connection.close()

    def raw(self, content):
        with socket.create_connection(("127.0.0.1", self.port), timeout=4) as sock:
            sock.settimeout(4)
            sock.sendall(content)
            response = http.client.HTTPResponse(sock)
            response.begin()
            return Reply(response.status, dict(response.getheaders()), response.read(64 * 1024))

    def login(self):
        reply = self.request("POST", "/mock/v1/sessions", {
            "login_id": "test@test.com", "password": "2222",
        })
        require(reply.status == 201, "Exact dummy credentials must create a session.")
        result = reply.json()
        self.secrets.append(result["session_token"])
        return result

    def assert_private_logs(self):
        if self._log is not None:
            self._log.flush()
        for path in self.logs:
            content = path.read_bytes()
            require(MARKER.encode() not in content, "Request marker leaked into CLI log.")
            for token in self.secrets:
                require(token.encode() not in content, "Session token leaked into CLI log.")


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    require(PYTHON.is_file(), "Install local Python dependencies before running live acceptance.")
    require((DYNAMODB_HOME / "DynamoDBLocal.jar").is_file(), "Install DynamoDB Local before acceptance.")
    server = LiveServer(tmp_path_factory.mktemp("arc-real-http"))
    try:
        server.start()
        yield server
        server.assert_private_logs()
    finally:
        server.stop()


def error(reply, status, code):
    require(reply.status == status, f"Expected HTTP {status}.")
    value = reply.json()
    require(value.get("error", {}).get("code") == code, f"Expected fixed error {code}.")
    require(type(value["error"].get("request_id")) is str, "Error must carry a request ID.")
    require(MARKER.encode() not in reply.body, "Request data reflected in error response.")


def test_health_and_root_are_control_only_without_user_state(live):
    for path in ("/", "/healthz"):
        reply = live.request("GET", path)
        require(reply.status == 200, "Local status endpoint must be reachable.")
        require(b"control_only" in reply.body, "Status must disclose the supported scope.")
        require(b"session_token" not in reply.body and b"progress_epoch" not in reply.body,
                "Public status endpoint must not expose user state.")


@pytest.mark.parametrize("body,status,code", [
    ({"login_id": "test@test.com", "password": 2222}, 400, "INVALID_REQUEST"),
    ({"login_id": "test@test.com", "password": "wrong"}, 401, "LOGIN_FAILED"),
    ({"login_id": "TEST@test.com", "password": "2222"}, 401, "LOGIN_FAILED"),
    ({"login_id": "test@test.com", "password": "2222", "extra": MARKER}, 400, "INVALID_REQUEST"),
    (b'{"login_id":"test@test.com","password":"2222","password":"2222"}', 400, "INVALID_REQUEST"),
    (b'{"login_id":"test@test.com","password":NaN}', 400, "INVALID_REQUEST"),
    (b'\xff', 400, "INVALID_REQUEST"),
])
def test_real_login_rejects_wrong_types_and_ambiguous_json(live, body, status, code):
    error(live.request("POST", "/mock/v1/sessions", body, headers={"Content-Type": "application/json"}),
          status, code)


def test_real_login_contract_and_exact_catalog_types(live):
    before = datetime.now(timezone.utc)
    first, second = live.login(), live.login()
    require(set(first) == {"environment", "session_id", "session_token", "expires_in", "expires_at", "mock_user"},
            "Login contract changed.")
    require(first["environment"] == "mock" and type(first["expires_in"]) is int
            and first["expires_in"] == 86400, "Session lifetime/type changed.")
    require(first["session_token"] != second["session_token"], "Two sessions shared a bearer token.")
    require(first["session_id"] != second["session_id"], "Two sessions shared a session ID.")
    require(first["mock_user"] == second["mock_user"], "Dummy sessions must share one principal.")
    expiry = datetime.fromisoformat(first["expires_at"].replace("Z", "+00:00"))
    require(86395 <= (expiry - before).total_seconds() <= 86405, "Real session expiry differs from 24 hours.")
    programs = live.request("GET", "/mock/v1/programs", token=first["session_token"])
    require(programs.status == 200, "Authenticated catalog failed.")
    require(programs.headers.get("Cache-Control") == "no-store", "Control response became cacheable.")
    value = programs.json()
    require(value["guideline"] == "ARC2025" and value["guideline_basis"] == "ARC2020"
            and value["profile_name"] == "tester", "Approved guideline/profile changed.")
    require(type(value["progress_version"]) is int, "Progress revision is not an integer.")
    expected = {"mock-cpr": 3, "mock-compression-only": 60, "mock-ventilation-only": 8,
                "mock-two-rescuer-cpr": 8, "mock-two-rescuer-aed": 10}
    require({program["id"] for program in value["programs"]} == set(expected), "Catalog inventory changed.")
    for program in value["programs"]:
        require(program["is_mock"] is True, "Program must identify mock data.")
        require(program["supported_targets"] == ["adult", "child", "infant"], "Target inventory changed.")
        require(type(program["goal"]["required"]) is int
                and program["goal"]["required"] == expected[program["id"]], "Program goal/type changed.")
        require(set(program["progress_by_target"]) == {"adult", "child", "infant"}, "Missing progress slots.")
        require(all(type(count) is int and count == 0 for count in program["active_attempts_by_target"].values()),
                "Control-only server unexpectedly created an active attempt.")
    other = live.request("GET", "/mock/v1/programs", token=second["session_token"]).json()
    require(value == other, "Concurrent sessions do not observe the same progress.")


def test_concurrent_logins_share_epoch_but_not_tokens(live):
    with ThreadPoolExecutor(max_workers=4) as pool:
        sessions = list(pool.map(lambda _: live.login(), range(8)))
    require(len({session["session_token"] for session in sessions}) == 8, "Concurrent session token collision.")
    epochs = {live.request("GET", "/mock/v1/programs", token=session["session_token"]).json()["progress_epoch"]
              for session in sessions}
    require(len(epochs) == 1, "Concurrent login reset shared progress.")


def test_logout_resets_once_preserves_other_session_and_survives_process_restart(live):
    first, second = live.login(), live.login()
    token_a, token_b = first["session_token"], second["session_token"]
    before = live.request("GET", "/mock/v1/programs", token=token_b).json()
    reply = live.request("DELETE", "/mock/v1/session", token=token_a)
    require(reply.status == 204 and reply.body == b"", "Logout must return an empty 204.")
    after = live.request("GET", "/mock/v1/programs", token=token_b).json()
    require(after["progress_epoch"] != before["progress_epoch"], "Logout did not reset shared epoch.")
    require(after["progress_version"] == before["progress_version"] + 1, "Logout revision changed incorrectly.")
    error(live.request("GET", "/mock/v1/session", token=token_a), 403, "SESSION_REVOKED")
    require(live.request("DELETE", "/mock/v1/session", token=token_a).status == 204, "Logout replay failed.")
    require(live.request("GET", "/mock/v1/programs", token=token_b).json() == after,
            "Logout replay reset progress again.")
    live.stop()
    live.start()
    require(live.request("GET", "/mock/v1/session", token=token_b).status == 200,
            "Restart lost the still-active session.")
    error(live.request("GET", "/mock/v1/session", token=token_a), 403, "SESSION_REVOKED")
    require(live.request("GET", "/mock/v1/programs", token=token_b).json() == after,
            "Real DB restart lost shared epoch/revision.")


def test_unconfigured_training_and_calculation_do_not_create_progress(live):
    token = live.login()["session_token"]
    before = live.request("GET", "/mock/v1/programs", token=token).json()
    error(live.request("POST", "/mock/v1/attempts", {
        "client_request_id": "live-unverified-create", "catalog_version": "mock-catalog-v1",
        "program_id": "mock-cpr", "target": "infant",
    }, token), 503, "CALCULATOR_CONTRACT_MISMATCH")
    for method, suffix in (("POST", "calculation"), ("GET", "calculation"), ("GET", "chart-link")):
        error(live.request(method, f"/mock/v1/attempts/{ATTEMPT_ID}/{suffix}",
                           {} if method == "POST" else None, token), 503, "CALCULATOR_CONTRACT_MISMATCH")
    require(live.request("GET", "/mock/v1/programs", token=token).json() == before,
            "An unavailable calculator mutated progress.")


def test_protected_request_authentication_precedes_bad_json(live):
    error(live.request("POST", "/mock/v1/attempts", b"{not-json", headers={"Content-Type": "application/json"}),
          401, "SESSION_REQUIRED")


@pytest.mark.parametrize("target", ["/mock/v1/%73essions", "/mock//v1/sessions", "/mock/v1/sessions?token=x",
                                     "/mock/v1/sessions#fragment", "http://localhost/mock/v1/sessions"])
def test_raw_paths_cannot_normalize_into_login(live, target):
    body = b'{"login_id":"test@test.com","password":"2222"}'
    raw = (f"POST {target} HTTP/1.1\r\nHost: 127.0.0.1:{live.port}\r\n"
           f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode() + body
    reply = live.raw(raw)
    require(400 <= reply.status < 500, "A noncanonical path reached the login handler.")
    require(b"session_token" not in reply.body, "Noncanonical path issued a session.")


@pytest.mark.parametrize("version", ["HTTP/1.0", "HTTP/1.1"])
@pytest.mark.parametrize("value", ["", " ,", " chunked", " identity"])
@pytest.mark.parametrize("with_length", [False, True])
def test_every_transfer_encoding_presence_is_rejected_before_login(live, version, value, with_length):
    extra = "Content-Length: 0\r\n" if with_length else ""
    raw = (f"POST /mock/v1/sessions {version}\r\nHost: 127.0.0.1:{live.port}\r\n"
           f"Content-Type: application/json\r\nTransfer-Encoding:{value}\r\n"
           f"{extra}Connection: close\r\n\r\n0\r\n\r\n").encode()
    reply = live.raw(raw)
    require(reply.status == 400, "Transfer-Encoding presence bypassed the transport rejection.")
    require(b"session_token" not in reply.body, "Unsupported transfer framing issued a session.")


@pytest.mark.parametrize("extra", [
    "Authorization: Bearer PRIVATE-LOCAL-ACCEPTANCE-MARKER\r\nauthorization: Bearer other\r\n",
    "Content-Length: 0\r\nContent-Length: 1\r\n",
    "Content-Length: 0\r\nContent-Length: 0\r\n",
    "Host: private-invalid.example\r\n",
])
def test_duplicate_security_headers_fail_without_echo(live, extra):
    raw = (f"GET /mock/v1/session HTTP/1.1\r\nHost: 127.0.0.1:{live.port}\r\n"
           f"{extra}Connection: close\r\n\r\n").encode()
    reply = live.raw(raw)
    require(400 <= reply.status < 500, "Ambiguous security header was accepted.")
    require(MARKER.encode() not in reply.body, "Parser error reflected secret header data.")
    require(live.request("GET", "/healthz").status == 200, "Rejected request damaged server health.")


@pytest.mark.parametrize("headers", [
    {"Host": "attacker.example"}, {"Origin": "https://attacker.example"},
    {"Origin": "null"}, {"Sec-Fetch-Site": "cross-site"},
])
def test_host_and_browser_origin_protection(live, headers):
    reply = live.request("POST", "/mock/v1/sessions", {
        "login_id": "test@test.com", "password": "2222",
    }, headers=headers)
    require(reply.status == 400, "Untrusted browser/Host request passed the local boundary.")
    require(b"session_token" not in reply.body, "Rejected browser request issued a session.")


def test_control_body_limit_is_enforced_and_server_recovers(live):
    reply = live.request("POST", "/mock/v1/sessions", b"x" * (16 * 1024 + 1),
                         headers={"Content-Type": "application/json"})
    require(reply.status == 413, "Oversized local control body was not rejected.")
    require(live.request("GET", "/healthz").status == 200, "Oversized request damaged server health.")


def test_request_headers_and_responses_are_not_logged(live):
    live.request("GET", "/mock/v1/session", token=MARKER)
    live.request("POST", "/mock/v1/sessions", {"login_id": MARKER, "password": MARKER})
    live.assert_private_logs()


def test_actual_database_failure_is_503_and_restart_recovers(live):
    token = live.login()["session_token"]
    before = live.request("GET", "/mock/v1/programs", token=token).json()
    # Inspect only children of this test-owned CLI; never discover/kill an
    # arbitrary process by port, name, or a broad process listing.
    listed = subprocess.run(["pgrep", "-P", str(live.process.pid)],
                            capture_output=True, text=True, timeout=3, check=False)
    identifiers = listed.stdout.split()
    require(listed.returncode == 0 and len(identifiers) == 1 and identifiers[0].isdigit(),
            "Expected exactly one owned database child.")
    child_id = int(identifiers[0])
    identity = subprocess.run(["ps", "-p", str(child_id), "-o", "args="],
                              capture_output=True, text=True, timeout=3, check=False)
    require(identity.returncode == 0 and "ArcLocalDynamo" in identity.stdout
            and str(live.data / "dynamodb") in identity.stdout,
            "Refusing to stop a child without the exact test database identity.")
    try:
        os.kill(child_id, signal.SIGTERM)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", live.db_port), timeout=0.1):
                    pass
            except OSError:
                break
            time.sleep(0.1)
        else:
            raise AssertionError("Owned test database did not stop.")
        error(live.request("GET", "/healthz"), 503, "TEMPORARILY_UNAVAILABLE")
        error(live.request("GET", "/mock/v1/programs", token=token), 503, "TEMPORARILY_UNAVAILABLE")
        error(live.request("POST", "/mock/v1/sessions", {
            "login_id": "test@test.com", "password": "2222",
        }), 503, "TEMPORARILY_UNAVAILABLE")
    finally:
        live.stop()
        live.start()
    require(live.request("GET", "/mock/v1/programs", token=token).json() == before,
            "Database restart after failure reset persisted progress.")


def test_lost_installation_key_refuses_restart_without_replacement(tmp_path):
    server = LiveServer(tmp_path / "lost-key")
    try:
        server.start()
        server.login()
        server.stop()
        key = server.data / "installation.json"
        require(key.is_file(), "Startup did not persist its installation key.")
        backup = server.data / "test-owned-installation-backup.json"
        key.rename(backup)
        server.launch()
        try:
            result = server.process.wait(timeout=12)
        except subprocess.TimeoutExpired:
            raise AssertionError("Startup with a missing key did not fail promptly.") from None
        require(result != 0, "Existing database was accepted without its installation key.")
        require(not key.exists(), "Missing key was silently regenerated.")
        require((server.data / "dynamodb").is_dir(), "Key loss deleted the existing database.")
        server.assert_private_logs()
    finally:
        server.stop()


def test_occupied_api_port_is_not_reused_or_terminated(tmp_path):
    server = LiveServer(tmp_path / "occupied-port")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as existing:
        existing.bind(("127.0.0.1", server.port))
        existing.listen(1)
        try:
            server.launch()
            require(server.process.wait(timeout=12) != 0, "CLI accepted an already occupied API port.")
            with socket.create_connection(("127.0.0.1", server.port), timeout=1):
                connection, _ = existing.accept()
                connection.close()
            require(not server.data.exists(), "Port refusal created a new local installation.")
        finally:
            server.stop()

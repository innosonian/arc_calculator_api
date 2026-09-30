"""Acceptance through the real CLI, HTTP parser, owned worker and persistent DynamoDB Local.

This file deliberately lives outside tests/: the unit suite's AWS monkeypatch
must not be the reason the executable is safe. No application/test store is
injected. Every request in these tests crosses a real loopback TCP connection
to the default local server, which serves only /api/v2 with the 15 temporary
Dummy Dev courses. The prerequisites must be installed beforehand; tests never
download software.

The harness here (LiveServer and helpers) is shared by the other live files.
It imports only the standard library and pytest so that the validation runner
can load this file on its own (tests/test_local_validation_runner.py).
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
import http.client
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import time
import uuid

import pytest


# Real loopback sockets: opted out of the directory network guard (conftest.py).
pytestmark = pytest.mark.loopback

ROOT = Path(__file__).resolve().parents[1]
PYTHON = Path(os.environ.get("ARC_LOCAL_TEST_PYTHON", ROOT / "var/local-python/bin/python"))
DYNAMODB_HOME = Path(os.environ.get("ARC_LOCAL_TEST_DYNAMODB_HOME", ROOT / "var/dynamodb-local-3.3.1"))
ATTEMPT_ID = "b1234567-1234-4234-9234-123456789abc"
MARKER = "PRIVATE-LOCAL-ACCEPTANCE-MARKER"
DUMMY_LOGIN = {"loginId": "test@test.com", "password": "2222"}
COURSES_PATH = "/api/v2/courses/progress/?page=1&pageSize=100"
MULTIPART_BOUNDARY = "arc-live-acceptance-boundary"
# Independent expectations for the Dummy Dev catalog (mock_journey/dev_course.py
# numbering): program order, display name and approved goal.
PROGRAMS = (
    ("mock-cpr", "CPR Training", "cycles", 3),
    ("mock-compression-only", "Chest Compression Only", "compressions", 60),
    ("mock-ventilation-only", "Ventilation Only", "ventilations", 8),
    ("mock-two-rescuer-cpr", "2-Rescuer CPR", "cycles", 8),
    ("mock-two-rescuer-aed", "2-Rescuer CPR with AED-T", "cycles", 10),
)
TARGETS = ("adult", "child", "infant")
HEALTH_KEYS = ["service", "mode", "calculator_available", "login_path", "programs_path",
               "calculation_transport_configured", "program_target_combinations", "completion_policy",
               "operational_logs"]
# Time budgets of the live harness (one place; adjust after runner measurements).
# STARTUP_SECONDS: CLI launch (javac + DB child + worker) until /healthz is 200.
# RESULT_SECONDS: the owned worker's automatic calculation of one upload.
# SHUTDOWN_SECONDS: an interrupted CLI's clean exit, or a refused launch's exit.
STARTUP_SECONDS = 60
RESULT_SECONDS = 30
SHUTDOWN_SECONDS = 12


def require(condition, explanation):
    """Fail with a fixed explanation, never pytest's credential-rich operands."""
    if not condition:
        raise AssertionError(explanation)


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@dataclass(frozen=True)
class DummyCourse:
    program: str
    name: str
    kind: str
    required: int
    target: str
    number: int

    @property
    def course_id(self):
        return 910000 + self.number

    @property
    def enrollment_id(self):
        return 920000 + self.number

    @property
    def progress_id(self):
        return 930000 + self.number

    @property
    def practice_link_id(self):
        return 940000 + self.number * 10 + 1

    @property
    def final_link_id(self):
        return 940000 + self.number * 10 + 2

    @property
    def title(self):
        return f"[Dummy Dev] {self.name} ({self.target})"


def dummy_courses():
    return [DummyCourse(program, name, kind, required, target, index * len(TARGETS) + position + 1)
            for index, (program, name, kind, required) in enumerate(PROGRAMS)
            for position, target in enumerate(TARGETS)]


def dummy_course(program="mock-compression-only", target="adult"):
    matches = [course for course in dummy_courses() if (course.program, course.target) == (program, target)]
    require(len(matches) == 1, "Unknown Dummy Dev course requested by the test.")
    return matches[0]


def multipart(parts):
    """multipart/form-data body with the measurement part names the parser reads."""
    body = b"".join(
        f'--{MULTIPART_BOUNDARY}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        + (value.encode() if type(value) is str else value) + b"\r\n"
        for name, value in parts.items()
    ) + f"--{MULTIPART_BOUNDARY}--\r\n".encode()
    return body, "multipart/form-data; boundary=" + MULTIPART_BOUNDARY


_ENVELOPE_TIMESTAMP = re.compile(rb', "timestamp": "\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z"\}\Z')


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

    def data(self, status=200):
        require(self.status == status, f"Expected HTTP {status}.")
        value = self.json()
        require(value.get("success") is True and "data" in value, "Expected a v2 success envelope.")
        return value["data"]

    def stable_body(self, status=200):
        """The exact response bytes minus the envelope's per-response timestamp.

        A v2 envelope ends with its response time. Everything before it (the
        data bytes, their key order and number formatting, and the message)
        must stay byte-identical across a replay, a restart or a retry.
        """
        self.data(status)
        match = _ENVELOPE_TIMESTAMP.search(self.body)
        require(match is not None, "Expected the v2 envelope to end with its response timestamp.")
        return self.body[:match.start()]


class LiveServer:
    """The real default CLI (scripts/serve_local.py) in a test-owned data directory."""

    def __init__(self, work, *, extra_args=()):
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
        self.extra_args = list(extra_args)

    def command(self):
        return [str(PYTHON), str(ROOT / "scripts/serve_local.py"),
                "--host", "127.0.0.1", "--port", str(self.port),
                "--db-port", str(self.db_port), "--data-dir", str(self.data),
                "--dynamodb-home", str(DYNAMODB_HOME)] + self.extra_args

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
        deadline = time.monotonic() + STARTUP_SECONDS
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
        raise AssertionError("Real HTTP/DB/worker readiness did not succeed within the test startup budget.")

    def stop(self):
        process, self.process = self.process, None
        if process is not None:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
                try:
                    process.wait(timeout=SHUTDOWN_SECONDS)
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
        fields = {} if headers is None else dict(headers)
        if body is not None and type(body) is not bytes:
            body = json.dumps(body, allow_nan=False).encode()
            fields.setdefault("Content-Type", "application/json")
        if token:
            fields["Authorization"] = "Bearer " + token
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            connection.request(method, path, body=body, headers=fields)
            response = connection.getresponse()
            content = response.read(8_100_001)
            require(len(content) <= 8_100_000, "Test response exceeded its explicit bound.")
            return Reply(response.status, dict(response.getheaders()), content)
        finally:
            connection.close()

    def raw(self, content):
        with socket.create_connection(("127.0.0.1", self.port), timeout=4) as sock:
            sock.settimeout(4)
            sock.sendall(content)
            response = http.client.HTTPResponse(sock)
            response.begin()
            return Reply(response.status, dict(response.getheaders()), response.read(64 * 1024))

    # -- /api/v2 --------------------------------------------------------------
    def login(self):
        data = self.request("POST", "/api/v2/sessions/", DUMMY_LOGIN).data(201)
        self.secrets.append(data["accessToken"])
        return data

    def token(self):
        return self.login()["accessToken"]

    def refresh(self, token):
        return self.request("POST", "/api/v2/session/refresh/", {}, token).data()

    def courses(self, token):
        return self.request("GET", COURSES_PATH, token=token).data()

    def course(self, token, course):
        return self.request("GET", f"/api/v2/courses/{course.course_id}/progress/?enrollmentId={course.enrollment_id}",
                            token=token).data()

    def item(self, token, course, link_id=None):
        link_id = course.practice_link_id if link_id is None else link_id
        items = [item for item in self.course(token, course)["courseItems"] if item["courseItemLinkId"] == link_id]
        require(len(items) == 1, "Expected one course item for the requested link.")
        return items[0]

    def start_attempt_reply(self, token, course, link_id=None, *, request_id=None, definition_hash=None):
        if definition_hash is None:
            definition_hash = self.course(token, course)["definitionHash"]
        return self.request("POST", "/api/v2/attempts/", token=token, body={
            "clientRequestId": request_id or str(uuid.uuid4()), "courseId": course.course_id,
            "enrollmentId": course.enrollment_id,
            "courseItemLinkId": course.practice_link_id if link_id is None else link_id,
            "definitionHash": definition_hash,
        })

    def start_attempt(self, token, course=None, link_id=None, *, request_id=None):
        data = self.start_attempt_reply(token, course or dummy_course(), link_id, request_id=request_id).data(201)
        self.secrets.append(data["resumeCredential"])
        return data

    def attempt(self, token, attempt):
        return self.request("GET", f'/api/v2/attempts/{attempt["attemptId"]}/', token=token)

    @staticmethod
    def calculation_path(attempt):
        return f'/api/v2/attempts/{attempt["attemptId"]}/calculation/'

    def upload(self, token, attempt, *, data=None, aed=None):
        if data is None:
            data = (ROOT / "tests/dataset/cco_1.bin").read_bytes()
        parts = {"rawHexBPfile": data, "condition": json.dumps(attempt["condition"])}
        if aed is not None:
            parts["aedHexBPfile"] = aed
        body, content_type = multipart(parts)
        return self.request("POST", self.calculation_path(attempt), body=body, token=token,
                            headers={"Content-Type": content_type})

    def calculation(self, token, attempt):
        return self.request("GET", self.calculation_path(attempt), token=token)

    def result(self, token, attempt, *, seconds=RESULT_SECONDS):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            reply = self.calculation(token, attempt)
            if reply.status == 200:
                return reply
            require(reply.status == 202 and reply.json()["data"]["calculationStatus"] == "pending",
                     "The real worker returned an unexpected terminal error.")
            time.sleep(0.1)
        raise AssertionError("The default CLI did not automatically calculate within this test wait budget.")

    def complete_practice(self, token, course=None):
        """Complete a practice item in the current epoch through the real worker."""
        course = course or dummy_course()
        if self.item(token, course)["isCompleted"] is True:
            return None
        attempt = self.start_attempt(token, course)
        require(self.upload(token, attempt).status in (200, 202), "Recorded measurement must be accepted.")
        result = self.result(token, attempt).data()
        require(result["evaluation"]["program_completed"] is True, "Recorded Only measurement must complete.")
        return attempt

    def chart(self, url):
        from urllib.parse import urlsplit
        value = urlsplit(url)
        require(value.scheme == "http" and value.netloc == f"127.0.0.1:{self.port}"
                and not value.query and not value.fragment, "Chart must use this owned local listener.")
        self.secrets.append(value.path.rsplit("/", 1)[-1])
        return self.request("GET", value.path)

    # -- owned processes -------------------------------------------------------
    def owned_children(self):
        """(pid, args) of this CLI's direct children, verified by parent PID."""
        listed = subprocess.run(["pgrep", "-P", str(self.process.pid)],
                                capture_output=True, text=True, timeout=3, check=False)
        children = []
        for value in listed.stdout.split():
            require(value.isdecimal(), "Unexpected owned process inventory.")
            identity = subprocess.run(["ps", "-p", value, "-o", "ppid=,args="],
                                      capture_output=True, text=True, timeout=3, check=False)
            parts = identity.stdout.strip().split(None, 1)
            if identity.returncode == 0 and len(parts) == 2 and parts[0] == str(self.process.pid):
                children.append((int(value), parts[1]))
        return children

    def owned_database_pid(self):
        matches = [pid for pid, args in self.owned_children()
                   if "ArcLocalDynamo" in args and str(self.data / "dynamodb") in args]
        require(len(matches) == 1, "Refusing to signal a database child without the exact test identity.")
        return matches[0]

    def owned_worker_pid(self):
        matches = [pid for pid, args in self.owned_children() if "multiprocessing.spawn" in args]
        require(len(matches) == 1, "Refusing to signal any process without proven test ownership.")
        return matches[0]

    def assert_private_logs(self):
        if self._log is not None:
            self._log.flush()
        for path in self.logs:
            content = path.read_bytes()
            require(MARKER.encode() not in content, "Request marker leaked into CLI log.")
            for token in self.secrets:
                require(token.encode() not in content, "Session token or credential leaked into CLI log.")


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
    require(value.get("success") is False or type(value["error"].get("request_id")) is str,
            "Error must be a v2 envelope or a transport error with a request ID.")
    require(MARKER.encode() not in reply.body, "Request data reflected in error response.")


def objects_fingerprint(server):
    import hashlib
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (server.data / "objects").glob("*.object")}


def test_health_and_root_describe_the_v2_course_api_without_user_state(live):
    for path in ("/", "/healthz"):
        reply = live.request("GET", path)
        require(reply.status == 200, "Local status endpoint must be reachable.")
        value = reply.json()
        require(list(value) == HEALTH_KEYS, "Status keys/order differ from the course_v2 status contract.")
        require(value["service"] == "arc-local-api" and value["mode"] == "course_v2"
                and value["calculator_available"] is True and value["calculation_transport_configured"] is True
                and value["login_path"] == "/api/v2/sessions/"
                and value["programs_path"] == "/api/v2/courses/progress/"
                and value["program_target_combinations"] == 15, "Status must disclose the supported v2 scope.")
        require(value["completion_policy"] == {"cycles": "evaluated", "compressions": "evaluated",
                                               "ventilations": "evaluated"},
                "CPR completion follows the D136 cycle rule; every goal kind is evaluated.")
        require(value["operational_logs"]["scope"] == "api_process", "Log counters are API-process only.")
        require(all(name not in reply.body for name in (b"accessToken", b"sessionId", b"epoch", b"courseId")),
                "Public status endpoint must not expose user state.")


@pytest.mark.parametrize("body,status,code", [
    ({"loginId": "test@test.com", "password": 2222}, 400, "INVALID_REQUEST"),
    ({"loginId": "test@test.com", "password": "wrong"}, 401, "LOGIN_FAILED"),
    ({"loginId": "test@test.com", "password": ""}, 400, "INVALID_REQUEST"),
    # Only the Dummy login exists locally; other IDs wait for the ARC contract.
    ({"loginId": "TEST@test.com", "password": "2222"}, 503, "CONTRACT_PENDING"),
    ({"loginId": "test@test.com", "password": "2222", "extra": MARKER}, 400, "INVALID_REQUEST"),
    ({"login_id": "test@test.com", "password": "2222"}, 400, "INVALID_REQUEST"),
    (b'{"loginId":"test@test.com","password":"2222","password":"2222"}', 400, "INVALID_REQUEST"),
    (b'{"loginId":"test@test.com","password":NaN}', 400, "INVALID_REQUEST"),
    (b'\xff', 400, "INVALID_REQUEST"),
])
def test_real_login_rejects_wrong_types_and_ambiguous_json(live, body, status, code):
    reply = live.request("POST", "/api/v2/sessions/", body, headers={"Content-Type": "application/json"})
    error(reply, status, code)
    require(b"accessToken" not in reply.body, "A rejected login issued a session.")


def test_real_login_contract_and_exact_catalog_types(live):
    before = datetime.now(timezone.utc)
    first, second = live.login(), live.login()
    require(list(first) == ["sessionId", "expiresAt", "learningAvailability", "accessToken", "tokenType", "userName"],
            "Login contract changed.")
    require(first["tokenType"] == "Bearer" and first["learningAvailability"] == {"state": "ready", "reason": None},
            "Dummy login must be ready with its temporary courses.")
    require(first["userName"] == "Test User", "The Dummy login must return the fixed display name (D129).")
    require(first["accessToken"] != second["accessToken"], "Two sessions shared a bearer token.")
    require(first["sessionId"] != second["sessionId"], "Two sessions shared a session ID.")
    expiry = datetime.fromisoformat(first["expiresAt"].replace("Z", "+00:00"))
    require(86395 <= (expiry - before).total_seconds() <= 86405, "Real session expiry differs from 24 hours.")
    session = live.request("GET", "/api/v2/session/", token=first["accessToken"]).data()
    require(session == {key: first[key] for key in ("sessionId", "expiresAt", "learningAvailability")},
            "Session view differs from the login response.")
    reply = live.request("GET", COURSES_PATH, token=first["accessToken"])
    require(reply.headers.get("Cache-Control") == "no-store", "Course response became cacheable.")
    value = reply.data()
    require(value["count"] == 15 and value["next"] is None and value["previous"] is None,
            "Dummy login must list exactly the 15 temporary courses.")
    expected = dummy_courses()
    require([row["courseId"] for row in value["results"]] == [course.course_id for course in expected],
            "Dummy Dev course IDs or order changed.")
    for row, course in zip(value["results"], expected):
        require(row["courseName"] == course.title and row["enrollmentId"] == course.enrollment_id
                and row["progressId"] == course.progress_id and row["certificationType"] is None,
                "Dummy Dev course identity changed.")
        require([(item["id"], item["itemType"], item["displayOrder"]) for item in row["summary"]]
                == [(950000 + course.number * 10 + 1, "training", 1), (950000 + course.number * 10 + 2, "assessment", 2)],
                "Each temporary course must be practice then final assessment.")
        require(type(row["learningAvailability"]) is dict, "Course availability must be an object.")
    require(live.courses(second["accessToken"]) == value, "Concurrent sessions do not observe the same courses.")
    detail = live.course(first["accessToken"], dummy_course())
    require([item["courseItemLinkId"] for item in detail["courseItems"]]
            == [dummy_course().practice_link_id, dummy_course().final_link_id], "Course item links changed.")
    require(type(detail["definitionHash"]) is str and len(detail["definitionHash"]) == 64,
            "Course detail must expose the pinned definition hash.")


def test_concurrent_logins_do_not_reset_shared_progress_or_share_tokens(live):
    course = dummy_course()
    token = live.token()
    live.complete_practice(token, course)
    with ThreadPoolExecutor(max_workers=4) as pool:
        sessions = list(pool.map(lambda _: live.login(), range(8)))
    require(len({session["accessToken"] for session in sessions}) == 8, "Concurrent session token collision.")
    views = [live.item(session["accessToken"], course) for session in sessions]
    require(all(view["isCompleted"] is True and view["isPassed"] is True for view in views),
            "Concurrent login reset the shared progress epoch.")


def test_logout_resets_once_preserves_other_session_and_survives_process_restart(live):
    course = dummy_course()
    token_a, token_b = live.token(), live.token()
    live.complete_practice(token_b, course)
    require(live.item(token_b, course)["isCompleted"] is True, "Precondition: completed practice.")
    reply = live.request("DELETE", "/api/v2/session/", token=token_a)
    require(reply.status == 204 and reply.body == b"", "Logout must return an empty 204.")
    error(live.request("GET", "/api/v2/session/", token=token_a), 403, "SESSION_REVOKED")
    # The surviving session sees the new epoch as waiting until it refreshes.
    waiting = live.request("GET", "/api/v2/session/", token=token_b).data()
    require(waiting["learningAvailability"] == {"state": "waiting", "reason": "arc_progress_unavailable"},
            "Logout did not reset the shared epoch.")
    error(live.request("GET", COURSES_PATH, token=token_b), 503, "ARC_PROGRESS_UNAVAILABLE")
    refreshed = live.refresh(token_b)
    require(refreshed["learningAvailability"] == {"state": "ready", "reason": None}, "Refresh did not recover.")
    after = live.course(token_b, course)
    require([item["isCompleted"] for item in after["courseItems"]] == [False, False],
            "The new epoch must not keep the previous completion.")
    require(live.request("DELETE", "/api/v2/session/", token=token_a).status == 204, "Logout replay failed.")
    # A second reset would put the surviving session back into waiting (503).
    require(live.request("GET", "/api/v2/session/", token=token_b).data() == refreshed,
            "Logout replay reset the shared epoch again.")
    require(live.course(token_b, course) == after, "Logout replay reset progress again.")
    live.stop()
    live.start()
    require(live.request("GET", "/api/v2/session/", token=token_b).status == 200,
            "Restart lost the still-active session.")
    error(live.request("GET", "/api/v2/session/", token=token_a), 403, "SESSION_REVOKED")
    require(live.course(token_b, course) == after, "Real DB restart lost shared epoch/progress.")


def test_unknown_attempt_routes_do_not_create_progress_or_files(live):
    token = live.token()
    before, files = live.courses(token), objects_fingerprint(live)
    unknown = {"attemptId": ATTEMPT_ID, "condition": {"mode": "training"}}
    error(live.upload(token, unknown), 404, "NOT_FOUND")
    for method, suffix, body in (("GET", "calculation/", None), ("GET", "chart-link/", None), ("GET", "", None),
                                 ("POST", "cancel/", {"reason": "user_cancelled"}),
                                 ("POST", "reauthorize/", {"resumeCredential": MARKER})):
        error(live.request(method, f"/api/v2/attempts/{ATTEMPT_ID}/{suffix}", body, token), 404, "NOT_FOUND")
    require(live.courses(token) == before and objects_fingerprint(live) == files,
            "An unknown attempt mutated progress or stored files.")


def test_protected_request_authentication_precedes_bad_json(live):
    error(live.request("POST", "/api/v2/attempts/", b"{not-json", headers={"Content-Type": "application/json"}),
          401, "SESSION_REQUIRED")


@pytest.mark.parametrize("target", ["/api/v2/%73essions/", "/api//v2/sessions/", "/api/v2/sessions/?token=x",
                                    "/api/v2/sessions/#fragment", "http://localhost/api/v2/sessions/",
                                    "/api/v2/sessions", "/API/v2/sessions/"])
def test_raw_paths_cannot_normalize_into_login(live, target):
    body = json.dumps(DUMMY_LOGIN).encode()
    raw = (f"POST {target} HTTP/1.1\r\nHost: 127.0.0.1:{live.port}\r\n"
           f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode() + body
    reply = live.raw(raw)
    require(400 <= reply.status < 500, "A noncanonical path reached the login handler.")
    require(b"accessToken" not in reply.body, "Noncanonical path issued a session.")


@pytest.mark.parametrize("version", ["HTTP/1.0", "HTTP/1.1"])
@pytest.mark.parametrize("value", ["", " ,", " chunked", " identity"])
@pytest.mark.parametrize("with_length", [False, True])
def test_every_transfer_encoding_presence_is_rejected_before_login(live, version, value, with_length):
    extra = "Content-Length: 0\r\n" if with_length else ""
    raw = (f"POST /api/v2/sessions/ {version}\r\nHost: 127.0.0.1:{live.port}\r\n"
           f"Content-Type: application/json\r\nTransfer-Encoding:{value}\r\n"
           f"{extra}Connection: close\r\n\r\n0\r\n\r\n").encode()
    reply = live.raw(raw)
    require(reply.status == 400, "Transfer-Encoding presence bypassed the transport rejection.")
    require(b"accessToken" not in reply.body, "Unsupported transfer framing issued a session.")


@pytest.mark.parametrize("extra", [
    "Authorization: Bearer PRIVATE-LOCAL-ACCEPTANCE-MARKER\r\nauthorization: Bearer other\r\n",
    "Content-Length: 0\r\nContent-Length: 1\r\n",
    "Content-Length: 0\r\nContent-Length: 0\r\n",
    "Host: private-invalid.example\r\n",
])
def test_duplicate_security_headers_fail_without_echo(live, extra):
    raw = (f"GET /api/v2/session/ HTTP/1.1\r\nHost: 127.0.0.1:{live.port}\r\n"
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
    reply = live.request("POST", "/api/v2/sessions/", DUMMY_LOGIN, headers=headers)
    require(reply.status == 400, "Untrusted browser/Host request passed the local boundary.")
    require(b"accessToken" not in reply.body, "Rejected browser request issued a session.")


def test_control_body_limit_is_enforced_and_server_recovers(live):
    reply = live.request("POST", "/api/v2/sessions/", b"x" * (16 * 1024 + 1),
                         headers={"Content-Type": "application/json"})
    require(reply.status == 413, "Oversized local control body was not rejected.")
    require(live.request("GET", "/healthz").status == 200, "Oversized request damaged server health.")


def test_request_headers_and_responses_are_not_logged(live):
    live.request("GET", "/api/v2/session/", token=MARKER)
    live.request("POST", "/api/v2/sessions/", {"loginId": MARKER, "password": MARKER})
    live.request("POST", "/api/v2/sessions/", {"loginId": "test@test.com", "password": MARKER})
    live.assert_private_logs()


def test_actual_database_failure_stops_serving_and_restart_recovers(live):
    course = dummy_course()
    token = live.token()
    live.complete_practice(token, course)
    before = live.course(token, course)
    child_id = live.owned_database_pid()
    process = live.process
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
        # A dead owned DB is never served as healthy: any answered request is
        # 503 until the supervising CLI exits with a visible failure. The CLI
        # stops within one serve poll, so this real-process check may see only
        # refused connections; the definite 503 for /healthz, the progress
        # route and login is test_runtime's dead-owned-database WSGI test.
        deadline = time.monotonic() + 30
        while process.poll() is None and time.monotonic() < deadline:
            for method, path, body, bearer in (("GET", "/healthz", None, None),
                                               ("GET", COURSES_PATH, None, token),
                                               ("POST", "/api/v2/sessions/", DUMMY_LOGIN, None)):
                try:
                    reply = live.request(method, path, body, bearer)
                except (OSError, http.client.HTTPException):
                    continue
                error(reply, 503, "TEMPORARILY_UNAVAILABLE")
            time.sleep(0.05)
        require(process.poll() is not None and process.returncode != 0,
                "A dead owned database must cause a visible CLI failure within the bound.")
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", live.port), timeout=0.5).close()
    finally:
        live.stop()
        live.start()
    require(live.course(token, course) == before, "Database restart after failure reset persisted progress.")


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
            result = server.process.wait(timeout=SHUTDOWN_SECONDS)
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
            require(server.process.wait(timeout=SHUTDOWN_SECONDS) != 0, "CLI accepted an already occupied API port.")
            with socket.create_connection(("127.0.0.1", server.port), timeout=1):
                connection, _ = existing.accept()
                connection.close()
            require(not server.data.exists(), "Port refusal created a new local installation.")
        finally:
            server.stop()


@pytest.mark.parametrize("flag", ["--control-only", "--course-v2"])
def test_removed_flags_are_unknown_arguments_before_any_installation(tmp_path, flag):
    # D123: the real executable ends with argparse's usage error (status 2)
    # and never creates a data directory, lock, key or DB for the old flags.
    server = LiveServer(tmp_path / "removed-flag", extra_args=[flag])
    try:
        server.launch()
        require(server.process.wait(timeout=SHUTDOWN_SECONDS) == 2, "A removed flag must exit with argparse status 2.")
        server.stop()
        output = server.logs[0].read_text()
        require("unrecognized arguments: " + flag in output, "The removed flag must be reported as unknown.")
        require("ARC local course API ready" not in output, "A removed flag must not start the server.")
        require(not server.data.exists(), "The refused command created a local installation.")
    finally:
        server.stop()

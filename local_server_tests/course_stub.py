"""Test-only /api/v2 service for local HTTP transport tests (not collected by pytest).

The real ``CourseHttp`` router, input validation and response envelopes run
behind ``local_server.http``; only its session/calculation hooks are scripted.
Every REST proxy event that crosses the transport is recorded in
``dispatched`` and every reached hook in ``calls``, so a test can prove that a
rejected request never reached the application. No DB, provider, storage or
calculator is involved; real persistence is covered by the live CLI tests.
"""

from copy import deepcopy
from types import SimpleNamespace
import uuid

from local_server.runtime import local_course_settings
from mock_journey.course_errors import CourseError
from mock_journey.course_http import CourseHttp
from mock_journey.course_response import utc_timestamp
from mock_journey.models import AuthContext
from tests.course_hooks_support import calculation_record, hooks_with, session_record


CLOCK_START = 1_800_000_000
SESSION_ID = "5e551000-0000-4000-8000-000000000001"
TOKEN = "socket-test-token"
ATTEMPT = "a5917021-0db0-45ed-9c8c-97d31e43a652"
LOGIN = {"loginId": "test@test.com", "password": "2222"}


class _Courses:
    """CourseService stand-in: login/refresh availability only."""

    def refresh_for_session(self, auth):
        raise CourseError("CONTRACT_PENDING")


class StubCourseService:
    course_mode = "course_v2"

    def __init__(self, *, payload_limit=None, token=TOKEN):
        self.token = token
        self.now = [CLOCK_START]
        self.calls = []
        self.dispatched = []
        self.revoked = False
        self.operations = None
        self.snapshot = {"integer": 80, "float": 80.0, "nullable": None}
        self.calculation = SimpleNamespace(payload_limit=payload_limit)
        # session_check reuses the session reader, as the refresh route did before it had its own hook.
        self._http = CourseHttp(
            _Courses(), local_course_settings(), clock=lambda: self.now[0],
            uuid_factory=lambda: str(uuid.uuid4()),
            hooks=hooks_with(authenticate=self._authenticate, login=self._login, logout=self._logout,
                             session_reader=self._session, session_check=self._session,
                             measurement_submit=self._submit, calculation_result=self._result),
        )
        self.course_http = self

    # The transport hands every event to course_http.dispatch.
    def dispatch(self, event):
        self.dispatched.append(deepcopy(event))
        return self._http.dispatch(event)

    def _auth(self):
        return AuthContext(SESSION_ID, "dummy-tester", 0, self.now[0] + 86400)

    def _authenticate(self, token, *, allow_logout_receipt=False):
        self.calls.append(("authenticate", token))
        if token != self.token:
            raise CourseError("SESSION_REQUIRED")
        if self.revoked and not allow_logout_receipt:
            raise CourseError("SESSION_REVOKED")
        return self._auth()

    def _login(self, login_id, password):
        self.calls.append(("login", {"loginId": login_id, "password": password}))
        return session_record(SESSION_ID, utc_timestamp(self.now[0] + 86400), auth=self._auth(),
                              access_token=self.token, user_name="Test User")

    def _logout(self, auth):
        self.calls.append(("logout", auth.session_id))
        self.revoked = True

    def _session(self, auth):
        self.calls.append(("session", auth.session_id))
        return session_record(SESSION_ID, utc_timestamp(self.now[0] + 86400),
                              learning_availability={"state": "ready", "reason": None})

    def _submit(self, auth, attempt_id, event):
        self.calls.append(("submit", attempt_id))
        return calculation_record(attempt_id, "queued")

    def _result(self, auth, attempt_id):
        self.calls.append(("result", attempt_id))
        return calculation_record(attempt_id, "evaluated", calculation=deepcopy(self.snapshot),
                                  evaluation={"program_completed": False}, progress_application={"applied": False},
                                  submit_arc=None)


def calculation_path(attempt_id=ATTEMPT):
    return f"/api/v2/attempts/{attempt_id}/calculation/"

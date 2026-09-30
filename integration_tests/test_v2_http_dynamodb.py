"""/api/v2 session/attempt commands through the public handler on DynamoDB Local.

Moved from integration_tests/test_mock_http_dynamodb.py (the removed v1 version):
start receipt replay after a provider outage, cross-session privacy, logout
receipt and shared epoch, and proof-based reauthorization after expiry. The
application is the real course_v2 composition (tests/journey_support.V2Journey)
over a fresh DynamoDB Local table. The removed v1 programs view
(active_attempts_by_target) has no v2 counterpart and is not carried over; the
shared epoch is read from the USER row instead.
"""

import json
import uuid

import pytest

from mock_journey.course_errors import CourseError
from tests.journey_support import V2Journey, dummy_course, dynamodb_local_store


USER = ("USER#dummy-tester", "STATE")
COURSE = dummy_course("mock-ventilation-only", "infant")


class SwitchableProvider:
    """The Dummy Dev provider until ``down`` is set; then every provider call fails."""

    def __init__(self, inner):
        self._inner = inner
        self.down = False
        self.calls_while_down = []

    def __getattr__(self, name):
        target = getattr(self._inner, name)
        if not callable(target):
            return target

        def call(*args, **kwargs):
            if self.down:
                self.calls_while_down.append(name)
                raise CourseError("ARC_PROGRESS_UNAVAILABLE")
            return target(*args, **kwargs)
        return call


@pytest.fixture
def journey(dynamodb_client):
    from mock_journey.course_settings import fixture_course_settings
    from mock_journey.dev_course import DummyDevCourseProvider
    from mock_journey.execution_definitions import execution_catalog

    provider = SwitchableProvider(DummyDevCourseProvider(settings=fixture_course_settings(),
                                                         execution=execution_catalog()))
    with dynamodb_local_store(dynamodb_client) as store:
        yield V2Journey(store, provider=provider)


def epoch_course_rows(h, epoch):
    """Every course row (HEAD, ITEM, FINAL, ...) stored under ``epoch``, in key order."""
    return [row for row in h.store.rows()
            if row["PK"].startswith("COURSE#") and row["SK"].startswith(f"EPOCH#{epoch}#")]


def status(h, method, path, token=None, body=None):
    reply = h.call(method, path, token=token, body=body)
    return reply.status, (reply.body if reply.status != 204 else None)


def test_real_http_replay_logout_receipt_shared_epoch_and_cross_session_privacy(journey):
    h = journey
    a, b = h.login(), h.login()
    definition_hash = h.course(a.token, COURSE)["definitionHash"]
    body = {"clientRequestId": str(uuid.uuid4()), "courseId": COURSE.course_id, "enrollmentId": COURSE.enrollment_id,
            "courseItemLinkId": COURSE.practice_link_id, "definitionHash": definition_hash}
    created, attempt = status(h, "POST", "/api/v2/attempts/", a.token, body)
    assert created == 201
    attempt = attempt["data"]
    path = f"/api/v2/attempts/{attempt['attemptId']}/"
    # A provider outage after commit must not make the acknowledged identity
    # unrecoverable after response loss: the replay is the stored receipt.
    h.provider.down = True
    replayed, replay = status(h, "POST", "/api/v2/attempts/", a.token, body)
    assert replayed == 200 and replay["data"] == attempt
    conflict_status, conflict = status(h, "POST", "/api/v2/attempts/", a.token,
                                       {**body, "courseItemLinkId": COURSE.final_link_id})
    assert conflict_status == 409 and conflict["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert h.provider.calls_while_down == []
    h.provider.down = False
    assert status(h, "GET", path, b.token)[0] == 404
    before = h.store.row(*USER)
    assert h.store.row(f"ATTEMPT#{attempt['attemptId']}", "META")["epoch"] == before["epoch"]
    assert status(h, "DELETE", "/api/v2/session/", a.token)[0] == 204
    after = h.store.row(*USER)
    assert after["epoch"] != before["epoch"] and after["revision"] == before["revision"] + 1
    assert status(h, "GET", "/api/v2/session/", b.token)[0] == 200  # The shared epoch moved; b stays signed in.
    rows = h.store.rows()
    assert status(h, "DELETE", "/api/v2/session/", a.token)[0] == 204  # Logout receipt, no second reset.
    assert h.store.rows() == rows
    revoked_status, revoked = status(h, "GET", "/api/v2/session/", a.token)
    assert revoked_status == 403 and revoked["error"]["code"] == "SESSION_REVOKED"
    # A valid proof rebinds the old-epoch attempt without reviving old progress. The
    # snapshot is taken before the reauthorize: neither it nor the two cancels may
    # touch the USER row or any course row of the new epoch.
    assert h.store.row(*USER) == after
    new_epoch_before = epoch_course_rows(h, after["epoch"])
    # Nothing has started or completed in the new epoch yet, so it holds no course row;
    # the old epoch holds the rows its start wrote (the rows a revival would copy).
    assert new_epoch_before == []
    assert any(row["SK"] == f"EPOCH#{before['epoch']}#HEAD" for row in epoch_course_rows(h, before["epoch"]))
    rebound_status, rebound = status(h, "POST", path + "reauthorize/", b.token,
                                     {"resumeCredential": attempt["resumeCredential"]})
    assert rebound_status == 200
    assert rebound["data"] == attempt  # Same view and the same creator-bound proof.
    stored = h.store.row(f"ATTEMPT#{attempt['attemptId']}", "META")
    assert stored["epoch"] == before["epoch"] != h.store.row(*USER)["epoch"]
    assert stored["bound_session_id"] == b.session_id
    assert h.store.row(*USER) == after
    assert epoch_course_rows(h, after["epoch"]) == new_epoch_before
    for _ in range(2):
        assert status(h, "POST", path + "cancel/", b.token, {"reason": "connection_lost"})[0] == 204
        assert h.store.row(*USER) == after
        assert epoch_course_rows(h, after["epoch"]) == new_epoch_before
    assert h.attempt(b.token, attempt["attemptId"])["state"] == "cancelled"
    # The new epoch still shows the course not started for the surviving session.
    h.refresh(b.token)
    assert [item["isCompleted"] for item in h.course(b.token, COURSE)["courseItems"]] == [False, False]


def test_expiry_requires_proof_and_keeps_attempt_types_and_identity(journey):
    h = journey
    old = h.login()
    started = h.start(old.token, COURSE, COURSE.practice_link_id)
    credential = started.pop("resumeCredential")
    path = f"/api/v2/attempts/{started['attemptId']}/"
    view = h.attempt(old.token, started["attemptId"])
    assert view == started
    epoch = h.store.row(*USER)["epoch"]
    h.advance(86400)
    expired_status, expired = status(h, "GET", path, old.token)
    assert expired_status == 401 and expired["error"]["code"] == "SESSION_EXPIRED"
    new = h.login()
    assert h.session(new.token)["sessionId"] == new.session_id
    assert h.store.row(*USER)["epoch"] == epoch  # A login does not reset progress.
    assert status(h, "GET", path, new.token)[0] == 404
    assert status(h, "POST", path + "reauthorize/", new.token, {"resumeCredential": "wrong"})[0] == 404
    assert status(h, "POST", path + "reauthorize/", new.token, {"resumeCredential": credential[:-1] + (
        "A" if credential[-1] != "A" else "B")})[0] == 404
    rebound = h.reauthorize(new.token, started["attemptId"], credential)
    assert rebound.pop("resumeCredential") == credential
    assert rebound == view
    condition = rebound["condition"]
    assert type(condition["is_2rescuers"]) is bool and type(condition["cpr_cycle_type"]) is str
    assert json.dumps(condition, sort_keys=True) == json.dumps(started["condition"], sort_keys=True)
    assert h.attempt(new.token, started["attemptId"]) == rebound

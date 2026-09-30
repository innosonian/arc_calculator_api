"""Session/attempt state races and idempotency on DynamoDB Local, through /api/v2 only.

Moved from integration_tests/test_mock_state_dynamodb.py, which drove the
state repository directly with attempts created by the removed v1 API. Here
every command is a public
/api/v2 route on the real course_v2 composition (tests/journey_support.V2Journey)
over a fresh DynamoDB Local table, so the transaction conditions that decide
each race are DynamoDB's own. Attempts come from the v2 course start route or
from the captured legacy rows (tests/legacy_rows_support.py) -- the latter keep
the USER.slots accounting (open_attempts) that only legacy attempts carry.

Races are forced, not hoped for: ``ArmedClient`` runs a hook once just before
the next TransactWriteItems of one API instance (after that command's reads),
either to commit a competing change, to meet another API instance at a
barrier, or to drop the response of a committed write. Two API instances on the
same table and clock stand for two Lambda processes.
"""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from threading import Barrier, Lock
from types import SimpleNamespace
import uuid

from botocore.exceptions import ReadTimeoutError
import pytest

from mock_journey.assembly import ExecutionCatalog
from mock_journey.catalog import PROGRAMS, TARGETS
from mock_journey.course_settings import fixture_course_settings
from mock_journey.dev_course import DummyDevCourseProvider
from mock_journey.execution_definitions import execution_catalog
from tests.journey_support import JourneyStore, V2Journey, dummy_course, dynamodb_local_store, encode_item
from tests.legacy_rows_support import legacy_resume_credential, seed_legacy_rows


PRINCIPAL = "dummy-tester"
USER = ("USER#" + PRINCIPAL, "STATE")
SESSION_SECONDS = 86400
COURSE = dummy_course("mock-compression-only", "adult")
LEGACY_SLOT = "mock-cpr:adult"  # The captured "created" legacy attempt's counted slot.


class ArmedClient:
    """Real DynamoDB Local client; an armed hook runs once before the next TransactWriteItems."""

    def __init__(self, client):
        self.client = client
        self._lock = Lock()
        self._plan = None

    def __getattr__(self, name):
        return getattr(self.client, name)

    def arm(self, *, before=None, lose_response=False):
        with self._lock:
            self._plan = (before, lose_response)

    def transact_write_items(self, **kwargs):
        with self._lock:
            plan, self._plan = self._plan, None
        before, lose_response = plan or (None, False)
        if before is not None:
            before()
        response = self.client.transact_write_items(**kwargs)
        if lose_response:
            raise ReadTimeoutError(endpoint_url="http://127.0.0.1/local-response-loss")
        return response


class Race:
    """A primary /api/v2 instance plus same-table twins sharing one clock and object store."""

    def __init__(self, store, *, legacy=False, provider=None):
        self.store = store
        self.seeded = seed_legacy_rows(store) if legacy else None
        options = {"provider": provider} if provider is not None else {}
        if legacy:
            options.update(objects=self.seeded.objects, start=self.seeded.meta["capture_clock"] + 60)
        self.options = options
        self.h = V2Journey(JourneyStore(ArmedClient(store.client), store.table), **options)

    def twin(self):
        other = V2Journey(JourneyStore(ArmedClient(self.store.client), self.store.table),
                          **{**self.options, "objects": self.h.objects})
        other.now = self.h.now  # One clock for every process.
        return other

    # -- raw table access (DynamoDB Local, no condition) ----------------------
    def rows(self):
        return self.store.rows()

    def row(self, key):
        return self.store.row(*key)

    def mutate(self, key, **values):
        """Commit a competing change the way another writer would: new values, revision + 1."""
        item = self.row(key)
        item.update(values)
        item["revision"] += 1
        self.store.client.put_item(TableName=self.store.table, Item=encode_item(item))

    def attempt(self, attempt_id):
        return self.row((f"ATTEMPT#{attempt_id}", "META"))

    def attempts(self):
        return [row for row in self.rows() if row["PK"].startswith("ATTEMPT#")]

    def receipts(self):
        return [row for row in self.rows() if row["SK"].startswith("COURSE_CREATE#")]

    def legacy(self, label):
        attempt_id = self.seeded.attempts[label]["attempt_id"]
        return attempt_id, legacy_resume_credential(self.seeded, label)


@pytest.fixture
def store(dynamodb_client):
    with dynamodb_local_store(dynamodb_client) as created:
        yield created


def parallel(*calls):
    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        futures = [pool.submit(call) for call in calls]
        return [future.result(timeout=30) for future in futures]


def meet_at(barrier):
    return lambda: barrier.wait(timeout=10)


def start_body(course, link_id, definition_hash, request_id=None):
    return {"clientRequestId": request_id or str(uuid.uuid4()), "courseId": course.course_id,
            "enrollmentId": course.enrollment_id, "courseItemLinkId": link_id, "definitionHash": definition_hash}


def start(h, token, body):
    return h.call("POST", "/api/v2/attempts/", token=token, body=body)


def code(reply):
    return reply.status, (reply.error["code"] if reply.status >= 400 else None)


def v2_attempt(race, session):
    """A created v2 training attempt of ``session``: (attempt_id, resume credential)."""
    started = race.h.start(session.token, COURSE, COURSE.practice_link_id)
    return started["attemptId"], started["resumeCredential"]


def owned_attempt(race, kind):
    """(creator session id, creator token, attempt id, resume credential) of a created v2 or legacy attempt."""
    if kind == "legacy":
        attempt_id, credential = race.legacy("created")
        return race.seeded.session_id, race.seeded.issue_session_token(), attempt_id, credential
    creator = race.h.login()
    attempt_id, credential = v2_attempt(race, creator)
    return creator.session_id, creator.token, attempt_id, credential


def course_heads(race, epoch):
    """Course-scope HEAD rows of ``epoch`` by scope key (a course may project several scopes)."""
    return {row["PK"].removeprefix("COURSE#"): row for row in race.rows()
            if row["PK"].startswith("COURSE#") and row["SK"] == f"EPOCH#{epoch}#HEAD"}


def course_items(race, epoch):
    return [row for row in race.rows() if row["PK"].startswith("COURSE#") and row["SK"].startswith(f"EPOCH#{epoch}#ITEM#")]


def creator_expiry(race, session_id):
    return race.row((f"SESSION#{session_id}", "AUTH"))["expires_at"]


# -- course start (replaces the removed v1 create races) --------------------------

def test_concurrent_starts_of_one_training_item_from_two_sessions_both_succeed(store):
    race = Race(store)
    first, second = race.h.login(), race.h.login()
    definition_hash = race.h.course(first.token, COURSE)["definitionHash"]
    epoch = race.row(USER)["epoch"]
    heads_before = course_heads(race, epoch)
    assert heads_before and course_items(race, epoch) == []
    twins, barrier = [race.twin(), race.twin()], Barrier(2)
    for twin in twins:
        twin.store.client.arm(before=meet_at(barrier))
    replies = parallel(*[
        lambda twin=twin, session=session: start(twin, session.token, start_body(
            COURSE, COURSE.practice_link_id, definition_hash))
        for twin, session in zip(twins, (first, second))
    ])
    assert [reply.status for reply in replies] == [201, 201], [reply.body for reply in replies]
    assert replies[0].data["attemptId"] != replies[1].data["attemptId"]
    stored = {row["attempt_id"]: row for row in race.attempts()}
    assert set(stored) == {reply.data["attemptId"] for reply in replies}
    assert {row["bound_session_id"] for row in stored.values()} == {first.session_id, second.session_id}
    assert len({json.dumps(row["course_binding"], sort_keys=True) for row in stored.values()}) == 1
    # No lost update (the v2 counterpart of the removed open_attempts == 2): each start moved its
    # scope's course HEAD exactly once under the revision condition, other scopes did not move,
    # and both attempts share one ITEM row.
    (scope,) = {row["course_binding"]["scope_key"] for row in stored.values()}
    heads_after = course_heads(race, epoch)
    assert set(heads_after) == set(heads_before) and scope in heads_after
    assert {key: row["revision"] - heads_before[key]["revision"] for key, row in heads_after.items()} == {
        key: (2 if key == scope else 0) for key in heads_before}
    items = course_items(race, epoch)
    assert len(items) == 1 and items[0]["completed"] is False and items[0]["scope_key"] == scope
    assert {row["course_binding"]["placement_key"] for row in stored.values()} == {items[0]["placement_key"]}


def course_counts(race, epoch):
    """The v2 activity accounting of ``epoch``: HEAD revision per scope, every ITEM and FINAL row."""
    return SimpleNamespace(
        user=race.row(USER),
        heads={key: row["revision"] for key, row in course_heads(race, epoch).items()},
        items=course_items(race, epoch),
        finals={row["PK"].removeprefix("COURSE#"): row for row in race.rows()
                if row["PK"].startswith("COURSE#") and row["SK"] == f"EPOCH#{epoch}#FINAL"},
    )


def assert_one_start_accounted(race, before, epoch, attempt_id, role):
    """The v2 counterpart of the removed ``open_attempts == 1``: one start accounted exactly once.

    Checked against mock_journey/course_state.py _commit_start: the start moves
    its scope's HEAD revision by one under the revision condition, creates the
    placement's ITEM once (revision 0, not completed) and, for a final
    assessment only, takes FINAL (phase active, this attempt, revision + 1).
    A replayed or duplicated start adds none of these again, every other scope
    stays unchanged, and the USER row (only a guard of the start) never moves.
    """
    binding = race.attempt(attempt_id)["course_binding"]
    scope, placement = binding["scope_key"], binding["placement_key"]
    after = course_counts(race, epoch)
    assert after.user == before.user
    assert set(after.heads) == set(before.heads) and scope in after.heads
    assert {key: after.heads[key] - before.heads[key] for key in after.heads} == {
        key: (1 if key == scope else 0) for key in before.heads}
    new_items = [row for row in after.items if row not in before.items]
    assert len(new_items) == 1 and len(after.items) == len(before.items) + 1
    assert (new_items[0]["scope_key"], new_items[0]["placement_key"]) == (scope, placement)
    assert new_items[0]["completed"] is False and new_items[0]["revision"] == 0
    changed_finals = {key for key in after.finals if after.finals[key] != before.finals.get(key)}
    if role == "training":
        assert changed_finals == set()
    else:
        assert changed_finals == {scope}
        final = after.finals[scope]
        assert before.finals[scope]["phase"] == "free"
        assert (final["phase"], final["active_attempt_id"]) == ("active", attempt_id)
        assert final["revision"] == before.finals[scope]["revision"] + 1


def test_same_start_request_race_persists_exactly_one_attempt(store):
    race = Race(store)
    session = race.h.login()
    definition_hash = race.h.course(session.token, COURSE)["definitionHash"]
    body = start_body(COURSE, COURSE.practice_link_id, definition_hash)
    epoch = race.row(USER)["epoch"]
    counts = course_counts(race, epoch)
    assert counts.heads and counts.items == [] and counts.finals
    twins, barrier = [race.twin(), race.twin()], Barrier(2)
    for twin in twins:
        twin.store.client.arm(before=meet_at(barrier))
    replies = parallel(*[lambda twin=twin: start(twin, session.token, body) for twin in twins])
    assert sorted(reply.status for reply in replies) == [200, 201], [reply.body for reply in replies]
    assert replies[0].data == replies[1].data
    assert [row["attempt_id"] for row in race.attempts()] == [replies[0].data["attemptId"]]
    assert len(race.receipts()) == 1
    # The losing twin replayed the receipt; it did not account a second start.
    assert_one_start_accounted(race, counts, epoch, replies[0].data["attemptId"], "training")
    accounted = race.rows()
    conflict = start(race.h, session.token, {**body, "courseItemLinkId": COURSE.final_link_id})
    assert code(conflict) == (409, "IDEMPOTENCY_CONFLICT")
    assert len(race.attempts()) == 1 and len(race.receipts()) == 1
    assert race.rows() == accounted


def test_training_completion_committed_after_start_read_is_reread_and_the_start_still_succeeds(store):
    """D130: a completion landing between the start's read and its commit is not a refusal.

    The first commit loses on the HEAD/ITEM revisions it read; the retry re-reads
    the completed rows and commits a new attempt against them without touching
    the completion.
    """
    race = Race(store)
    first, second = race.h.login(), race.h.login()
    started = race.h.start(first.token, COURSE, COURSE.practice_link_id)
    race.h.upload(first.token, started["attemptId"], started["condition"])
    body = start_body(COURSE, COURSE.practice_link_id, race.h.course(second.token, COURSE)["definitionHash"])
    twin, finished, committed = race.twin(), [], []

    def complete():
        # The Worker commits the passing result (ITEM completed, HEAD revision) after the start's reads.
        finished.append(race.h.work(started["attemptId"]))
        committed.append(race.rows())

    twin.store.client.arm(before=complete)
    reply = start(twin, second.token, body)
    assert finished == [True]
    assert code(reply) == (201, None), reply.body
    assert sorted(row["attempt_id"] for row in race.attempts()) == sorted([started["attemptId"], reply.data["attemptId"]])
    assert len([row for row in race.receipts() if row["bound_session_id"] == second.session_id]) == 1
    item = next(row for row in committed[0] if "#ITEM#" in row["SK"])
    assert item["completed"] is True
    after = next(row for row in race.rows() if "#ITEM#" in row["SK"])
    assert (after["completed"], after["passed"], after["completed_by_attempt"]) == (
        item["completed"], item["passed"], item["completed_by_attempt"])
    assert race.h.result(first.token, started["attemptId"])["evaluation"]["program_completed"] is True


@pytest.mark.parametrize("role", ["training", "final_assessment"])
def test_start_commit_response_loss_followed_by_same_request_does_not_duplicate(store, role):
    race = Race(store)
    session = race.h.login()
    link_id = COURSE.practice_link_id
    if role == "final_assessment":
        # The completed practice unlocks the final assessment (its FINAL row is the single active slot).
        practice = race.h.start(session.token, COURSE, COURSE.practice_link_id)
        race.h.upload(session.token, practice["attemptId"], practice["condition"])
        assert race.h.work(practice["attemptId"]) is True
        link_id = COURSE.final_link_id
    body = start_body(COURSE, link_id, race.h.course(session.token, COURSE)["definitionHash"])
    epoch = race.row(USER)["epoch"]
    counts = course_counts(race, epoch)
    attempts_before, receipts_before = race.attempts(), race.receipts()
    race.h.store.client.arm(lose_response=True)
    assert code(start(race.h, session.token, body)) == (503, "TEMPORARILY_UNAVAILABLE")
    committed = [row for row in race.attempts() if row not in attempts_before]
    assert len(committed) == 1  # The lost response belonged to a committed write.
    assert committed[0]["course_binding"]["start_role"] == role
    assert_one_start_accounted(race, counts, epoch, committed[0]["attempt_id"], role)
    accounted = race.rows()
    recovered = start(race.h, session.token, body)
    assert recovered.status == 200 and recovered.data["attemptId"] == committed[0]["attempt_id"]
    assert recovered.data["role"] == role
    assert len(race.receipts()) == len(receipts_before) + 1
    assert race.rows() == accounted  # The replay is the stored receipt: no second accounting.


@pytest.mark.parametrize("change,expected", [("revoke", (403, "SESSION_REVOKED")), ("expire", (401, "SESSION_EXPIRED"))])
def test_session_change_after_start_read_rejects_the_start_atomically(store, change, expected):
    race = Race(store)
    session = race.h.login()
    body = start_body(COURSE, COURSE.practice_link_id, race.h.course(session.token, COURSE)["definitionHash"])
    key = (f"SESSION#{session.session_id}", "AUTH")
    before = [row for row in race.rows() if (row["PK"], row["SK"]) != key]

    def interfere():
        if change == "revoke":
            race.mutate(key, status="revoked")
        else:
            race.mutate(key, expires_at=race.h.clock())

    race.h.store.client.arm(before=interfere)
    assert code(start(race.h, session.token, body)) == expected
    assert [row for row in race.rows() if (row["PK"], row["SK"]) != key] == before


def test_conflict_retry_checks_a_fresh_clock_instead_of_reusing_the_initial_auth_time(store):
    race = Race(store)
    session = race.h.login()
    body = start_body(COURSE, COURSE.practice_link_id, race.h.course(session.token, COURSE)["definitionHash"])
    expires_at = creator_expiry(race, session.session_id)

    def interfere():
        race.mutate(USER)  # Forces the start transaction to fail its USER condition and re-read.
        race.h.now[0] = expires_at

    race.h.store.client.arm(before=interfere)
    assert code(start(race.h, session.token, body)) == (401, "SESSION_EXPIRED")
    assert race.attempts() == [] and race.receipts() == []


# -- logout ----------------------------------------------------------------------

def assert_legacy_slots_reset_once(race, kind, before_user):
    """Legacy USER rows carry slots; the first logout resets them for the new epoch (R3)."""
    if kind == "legacy":
        assert before_user["slots"][LEGACY_SLOT]["open_attempts"] == 1
        assert race.row(USER)["slots"][LEGACY_SLOT]["open_attempts"] == 0


@pytest.mark.parametrize("kind", ["v2", "legacy"])
def test_logout_replay_does_not_reset_other_session_new_progress(store, kind):
    race = Race(store, legacy=kind == "legacy")
    old, surviving = race.h.login(), race.h.login()
    initial = race.row(USER)
    initial_epoch = initial["epoch"]
    race.h.logout(old.token)
    reset_epoch = race.row(USER)["epoch"]
    assert reset_epoch != initial_epoch
    assert_legacy_slots_reset_once(race, kind, initial)
    race.h.refresh(surviving.token)
    attempt_id, _ = v2_attempt(race, surviving)
    before = race.rows()
    race.h.logout(old.token)  # Replayed logout: a stored receipt, not a second reset.
    assert race.rows() == before
    assert race.row(USER)["epoch"] == reset_epoch == race.attempt(attempt_id)["epoch"]
    assert race.h.session(surviving.token)["sessionId"] == surviving.session_id
    assert race.h.attempt(surviving.token, attempt_id)["state"] == "created"


def test_two_different_concurrent_logouts_each_reset_once_and_keep_third_session_active(store):
    race = Race(store)
    first, second, survivor = race.h.login(), race.h.login(), race.h.login()
    initial = race.row(USER)
    twins, barrier = [race.twin(), race.twin()], Barrier(2)
    for twin in twins:
        twin.store.client.arm(before=meet_at(barrier))
    replies = parallel(*[lambda twin=twin, session=session: twin.call("DELETE", "/api/v2/session/", token=session.token)
                         for twin, session in zip(twins, (first, second))])
    assert [reply.status for reply in replies] == [204, 204]
    sessions = [race.row((f"SESSION#{session.session_id}", "AUTH")) for session in (first, second)]
    assert [row["status"] for row in sessions] == ["revoked", "revoked"]
    assert sessions[0]["logout_epoch"] != sessions[1]["logout_epoch"]
    current = race.row(USER)
    assert current["revision"] == initial["revision"] + 2
    assert current["epoch"] in {row["logout_epoch"] for row in sessions}
    assert race.h.session(survivor.token)["sessionId"] == survivor.session_id
    assert race.row((f"SESSION#{survivor.session_id}", "AUTH"))["status"] == "active"


@pytest.mark.parametrize("order", ["logout_commits_first", "start_commits_first", "barrier"])
def test_start_racing_logout_never_mixes_attempt_epoch_and_current_epoch(store, order):
    race = Race(store)
    logout_session, creator = race.h.login(), race.h.login()
    old_epoch = race.row(USER)["epoch"]
    body = start_body(COURSE, COURSE.practice_link_id, race.h.course(creator.token, COURSE)["definitionHash"])
    starter, logger = race.twin(), race.twin()
    logout_call = lambda: logger.call("DELETE", "/api/v2/session/", token=logout_session.token)
    start_call = lambda: start(starter, creator.token, body)
    if order == "logout_commits_first":
        outcome = {}
        starter.store.client.arm(before=lambda: outcome.setdefault("logout", logout_call()))
        started = start_call()
        logged_out = outcome["logout"]
    elif order == "start_commits_first":
        outcome = {}
        logger.store.client.arm(before=lambda: outcome.setdefault("start", start_call()))
        logged_out = logout_call()
        started = outcome["start"]
    else:
        barrier = Barrier(2)
        starter.store.client.arm(before=meet_at(barrier))
        logger.store.client.arm(before=meet_at(barrier))
        started, logged_out = parallel(start_call, logout_call)
    assert logged_out.status == 204
    assert race.row((f"SESSION#{logout_session.session_id}", "AUTH"))["status"] == "revoked"
    current_epoch = race.row(USER)["epoch"]
    assert current_epoch != old_epoch
    new_epoch_rows = [row for row in race.rows() if f"EPOCH#{current_epoch}#" in row["SK"]]
    assert new_epoch_rows == []  # Nothing was started in the new epoch without its own refresh.
    if started.status == 201:
        attempt = race.attempt(started.data["attemptId"])
        assert attempt["epoch"] == attempt["course_binding"]["epoch"] == old_epoch
        items = [row for row in race.rows() if row["SK"].startswith(f"EPOCH#{old_epoch}#ITEM#")]
        assert len(items) == 1 and len(race.receipts()) == 1
    else:
        # The retried start re-reads USER, sees the new epoch and has no inventory for it yet.
        assert code(started) == (503, "ARC_PROGRESS_UNAVAILABLE"), started.body
        assert race.attempts() == [] and race.receipts() == []
    if order != "barrier":
        assert started.status == (201 if order == "start_commits_first" else 503)


@pytest.mark.parametrize("kind", ["v2", "legacy"])
def test_logout_commit_response_loss_does_not_erase_progress_on_retry(store, kind):
    race = Race(store, legacy=kind == "legacy")
    logout_session, creator = race.h.login(), race.h.login()
    initial = race.row(USER)
    race.h.store.client.arm(lose_response=True)
    lost = race.h.call("DELETE", "/api/v2/session/", token=logout_session.token)
    assert code(lost) == (503, "TEMPORARILY_UNAVAILABLE")
    assert race.row((f"SESSION#{logout_session.session_id}", "AUTH"))["status"] == "revoked"
    assert race.row(USER)["epoch"] != initial["epoch"]
    assert_legacy_slots_reset_once(race, kind, initial)
    race.h.refresh(creator.token)
    attempt_id, _ = v2_attempt(race, creator)
    before_retry = deepcopy(race.rows())
    race.h.logout(logout_session.token)
    assert race.rows() == before_retry
    assert race.h.attempt(creator.token, attempt_id)["state"] == "created"


# -- attempt ownership, cancel -----------------------------------------------------

@pytest.mark.parametrize("kind", ["v2", "legacy"])
def test_bound_session_is_required_even_when_the_principal_is_shared(store, kind):
    race = Race(store, legacy=kind == "legacy")
    creator_id, creator_token, attempt_id, _ = owned_attempt(race, kind)
    other = race.h.login()
    before = race.rows()
    for method, suffix, body in (("GET", "", None), ("POST", "cancel/", {"reason": "user_cancelled"}),
                                 ("GET", "calculation/", None), ("GET", "chart-link/", None)):
        reply = race.h.call(method, f"/api/v2/attempts/{attempt_id}/{suffix}", token=other.token, body=body)
        assert code(reply) == (404, "NOT_FOUND"), (method, suffix)
    upload = race.h.upload_event(other.token, attempt_id, race.h.attempt(creator_token, attempt_id)["condition"])
    assert code(race.h.call("POST", upload["path"], event=upload)) == (404, "NOT_FOUND")
    assert race.rows() == before
    assert race.attempt(attempt_id)["bound_session_id"] == creator_id
    assert race.h.attempt(creator_token, attempt_id)["state"] == "created"
    if kind == "legacy":
        assert race.row(USER)["slots"][LEGACY_SLOT]["open_attempts"] == 1


def test_old_epoch_legacy_cancel_does_not_decrement_new_epoch_count(store):
    race = Race(store, legacy=True)
    token = race.seeded.issue_session_token()
    attempt_id, _ = race.legacy("created")
    other = race.h.login()
    race.h.logout(other.token)  # New epoch; the legacy slots are reset (R3).
    user = race.row(USER)
    assert user["epoch"] != race.attempt(attempt_id)["epoch"]
    assert user["slots"][LEGACY_SLOT]["open_attempts"] == 0
    # A counted attempt of the new epoch, as the removed v1 create wrote it.
    slots = deepcopy(user["slots"])
    slots[LEGACY_SLOT]["open_attempts"] = 1
    race.mutate(USER, slots=slots)
    before = race.row(USER)
    race.h.cancel(token, attempt_id)
    race.h.cancel(token, attempt_id)
    assert race.row(USER) == before
    assert race.attempt(attempt_id)["state"] == "cancelled"
    assert race.h.attempt(token, attempt_id)["state"] == "cancelled"


@pytest.mark.parametrize("kind", ["v2", "legacy"])
def test_current_epoch_cancel_is_idempotent_and_cannot_be_reauthorized(store, kind):
    race = Race(store, legacy=kind == "legacy")
    creator_id, token, attempt_id, credential = owned_attempt(race, kind)
    user = race.row(USER)
    race.h.cancel(token, attempt_id)
    after_first = race.rows()
    race.h.cancel(token, attempt_id)
    assert race.rows() == after_first
    if kind == "legacy":
        cancelled_user = race.row(USER)
        assert cancelled_user["slots"][LEGACY_SLOT]["open_attempts"] == 0
        assert cancelled_user["revision"] == user["revision"] + 1
    else:
        assert race.row(USER) == user  # A v2 attempt is not counted in USER.
    race.h.now[0] = creator_expiry(race, creator_id)
    new = race.h.login()
    assert code(race.h.call("POST", f"/api/v2/attempts/{attempt_id}/reauthorize/", token=new.token,
                            body={"resumeCredential": credential})) == (409, "INVALID_STATE")
    assert race.attempt(attempt_id)["bound_session_id"] == creator_id


# -- reauthorize -------------------------------------------------------------------

def reauthorize(h, token, attempt_id, credential):
    return h.call("POST", f"/api/v2/attempts/{attempt_id}/reauthorize/", token=token,
                  body={"resumeCredential": credential})


@pytest.mark.parametrize("kind", ["v2", "legacy"])
def test_new_session_revocation_during_resume_cannot_take_ownership(store, kind):
    race = Race(store, legacy=kind == "legacy")
    creator_id, _, attempt_id, credential = owned_attempt(race, kind)
    before = race.attempt(attempt_id)
    race.h.now[0] = creator_expiry(race, creator_id)
    new = race.h.login()
    race.h.store.client.arm(before=lambda: race.mutate((f"SESSION#{new.session_id}", "AUTH"), status="revoked"))
    assert code(reauthorize(race.h, new.token, attempt_id, credential)) == (403, "SESSION_REVOKED")
    assert race.attempt(attempt_id) == before


@pytest.mark.parametrize("kind", ["v2", "legacy"])
def test_active_old_session_cannot_be_taken_over_and_expired_resume_keeps_creator_and_epoch(store, kind):
    race = Race(store, legacy=kind == "legacy")
    creator_id, _, attempt_id, credential = owned_attempt(race, kind)
    another = race.h.login()
    original = race.attempt(attempt_id)
    assert code(reauthorize(race.h, another.token, attempt_id, credential)) == (409, "INVALID_STATE")
    assert race.attempt(attempt_id) == original
    race.h.now[0] = creator_expiry(race, creator_id)
    new = race.h.login()
    rebound = reauthorize(race.h, new.token, attempt_id, credential)
    assert rebound.status == 200 and rebound.data["attemptId"] == attempt_id
    # The proof is bound to the creator session, so it stays the same credential.
    assert rebound.data["resumeCredential"] == credential
    stored = race.attempt(attempt_id)
    assert stored["bound_session_id"] == new.session_id
    assert stored["revision"] == original["revision"] + 1
    assert {key: value for key, value in stored.items() if key not in ("bound_session_id", "revision")} == {
        key: value for key, value in original.items() if key not in ("bound_session_id", "revision")}
    for field in ("creator_session_id", "epoch", "definition_json", "resume_digest", "resume_nonce"):
        assert stored[field] == original[field]
    again = reauthorize(race.h, new.token, attempt_id, credential)
    assert again.status == 200 and again.data == rebound.data
    assert race.attempt(attempt_id) == stored


@pytest.mark.parametrize("kind", ["v2", "legacy"])
def test_two_new_sessions_cannot_both_take_over_the_same_expired_attempt(store, kind):
    race = Race(store, legacy=kind == "legacy")
    creator_id, _, attempt_id, credential = owned_attempt(race, kind)
    race.h.now[0] = creator_expiry(race, creator_id)
    sessions = [race.h.login(), race.h.login()]
    twins, barrier = [race.twin(), race.twin()], Barrier(2)
    for twin in twins:
        twin.store.client.arm(before=meet_at(barrier))
    replies = parallel(*[lambda twin=twin, session=session: reauthorize(twin, session.token, attempt_id, credential)
                         for twin, session in zip(twins, sessions)])
    assert sorted(code(reply) for reply in replies) == [(200, None), (409, "INVALID_STATE")], [
        reply.body for reply in replies]
    winner = sessions[[reply.status for reply in replies].index(200)]
    assert race.attempt(attempt_id)["bound_session_id"] == winner.session_id


# -- stored definition types -------------------------------------------------------

def typed_catalog():
    """The approved 15 definitions with a calculation_profile holding null/bool/int/float/str leaves."""
    base = execution_catalog()
    definitions = {}
    for program, *_ in PROGRAMS:
        for target in TARGETS:
            definition = base.get_definition(program, target)
            definition["calculation_profile"] = {"Custom": {
                "PassThreshold": 80.0, "PassThresholdChild": 80, "CertificateAdult": False,
                "CertificateChild": None, "CertificateInfant": "80",
            }}
            definitions[f"{program}:{target}"] = definition
    return ExecutionCatalog(definitions, base.schemas)


def leaf_types(value):
    if type(value) is dict:
        return {key: leaf_types(nested) for key, nested in value.items()}
    if type(value) is list:
        return [leaf_types(nested) for nested in value]
    return type(value).__name__


def test_definition_json_string_preserves_null_bool_integer_float_and_string(store):
    provider = DummyDevCourseProvider(settings=fixture_course_settings(), execution=typed_catalog())
    race = Race(store, provider=provider)
    creator = race.h.login()
    attempt_id, credential = v2_attempt(race, creator)
    stored = race.attempt(attempt_id)
    assert type(stored["definition_json"]) is str
    definition = json.loads(stored["definition_json"])
    assert leaf_types(definition["calculation_profile"]) == {"Custom": {
        "PassThreshold": "float", "PassThresholdChild": "int", "CertificateAdult": "bool",
        "CertificateChild": "NoneType", "CertificateInfant": "str",
    }}
    assert definition["calculation_profile"]["Custom"]["PassThreshold"] == 80.0
    condition = race.h.attempt(creator.token, attempt_id)["condition"]
    assert leaf_types(condition) == leaf_types(definition["condition"]) and condition == definition["condition"]
    assert type(condition["is_2rescuers"]) is bool and type(condition["cpr_cycle_type"]) is str
    race.h.now[0] = creator_expiry(race, creator.session_id)
    new = race.h.login()
    rebound = race.h.reauthorize(new.token, attempt_id, credential)
    assert rebound["condition"] == condition and leaf_types(rebound["condition"]) == leaf_types(condition)
    assert race.attempt(attempt_id)["definition_json"] == stored["definition_json"]


def test_legacy_definition_json_is_kept_byte_for_byte_through_reauthorize_and_cancel(store):
    race = Race(store, legacy=True)
    captured = {label: race.attempt(race.seeded.attempts[label]["attempt_id"])["definition_json"]
                for label in ("created", "queued", "evaluated")}
    race.h.now[0] = race.seeded.meta["session_expires_at"]
    new = race.h.login()
    for label in captured:
        attempt_id, credential = race.legacy(label)
        view = race.h.reauthorize(new.token, attempt_id, credential)
        assert view["condition"] == json.loads(captured[label])["condition"]
    race.h.cancel(new.token, race.seeded.attempts["created"]["attempt_id"])
    assert {label: race.attempt(race.seeded.attempts[label]["attempt_id"])["definition_json"]
            for label in captured} == captured

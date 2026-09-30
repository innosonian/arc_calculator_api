"""Q4 restart limit against DynamoDB Local, the real worker/runtime and the Relay due index.

An interruption is a calculation call that never finished: a process killed
mid-call (no worker handler runs) or an unexpected calculator error. Up to five
replacement calls are issued; the sixth interruption closes the job.

Course jobs use the assigned fixture learner (tests/vcc_runtime_support.py).
Legacy (no course binding) jobs are the queued job of the captured legacy row
fixture (tests/fixtures/legacy_mock_v1_rows), finished by a current Worker as
D103 keeps. No v1 API route or v1-only service method is used.
"""

import json
from types import SimpleNamespace
import uuid

import pytest

from integration_tests.worker_journey_support import (  # noqa: F401 (store fixture)
    attempt_row, calculation, seeded_legacy, store, user_row,
)
from mock_journey.course_errors import CourseError
from mock_journey.dispatch import OutboxRelay
from mock_journey.errors import JourneyError
from mock_journey.handler import handle
from mock_journey.jobs import CALCULATION_RESTART_LIMIT, RESTART_LIMIT_BASIS, DynamoJobRepository, JobLeaseLost
from mock_journey.state import DynamoStateRepository
from mock_journey.typed import json_bytes
from tests.journey_support import decode_item, encode_item
from tests.vcc_policy_support import completed_progress
from tests.vcc_runtime_support import runtime, start_attempt, submit
from tests.vcc_support import event


LEGACY_SLOT = "mock-compression-only:adult"
LEGACY_PRINCIPAL = "dummy-tester"


class ProcessKilled(BaseException):
    """Stands in for a Lambda timeout or SIGKILL: no worker handler or write runs."""


class Sender:
    def __init__(self):
        self.sent = []

    def send(self, job_id):
        self.sent.append(job_id)


def raw_row(client, table, pk, sk):
    found = client.get_item(TableName=table, Key=encode_item({"PK": pk, "SK": sk}), ConsistentRead=True).get("Item")
    return decode_item(found) if found is not None else None


def interrupt(env, monkeypatch, mode, calls, *, real_after=None):
    real = env.adapter.calculate

    def calculate(*args):
        calls.append(1)
        if real_after is not None and len(calls) > real_after:
            return real(*args)
        if mode == "killed":
            raise ProcessKilled()
        raise RuntimeError("test-only calculator interruption")
    monkeypatch.setattr(env.adapter, "calculate", calculate)


def interrupted_run(env, job_id, mode):
    if mode == "killed":
        with pytest.raises(ProcessKilled):
            env.worker.process(job_id)
    else:
        assert env.worker.process(job_id) is False
    env.now[0] += 61  # Past the 60s lease of a killed call and the 5s retry due of a deferral.


def relay(env):
    sender = Sender()
    return OutboxRelay(env.worker.jobs, sender, lease_seconds=60, retry_seconds=5, page_size=10,
                       max_pages=5, clock=env.clock), sender


def interrupt_to_limit(env, job_id, mode, calls):
    jobs = env.worker.jobs
    for number in range(CALCULATION_RESTART_LIMIT + 1):
        interrupted_run(env, job_id, mode)
        assert len(calls) == number + 1
        job = jobs.get_job(job_id)
        if number == 0:
            assert "calculation_restarts" not in job  # A first call is not a restart; absent reads 0.
        assert job.get("calculation_restarts", 0) == number
        assert job["state"] == "running" and job["call_phase"] == "started"


def assert_relay_stops_after_close(env, job_id):
    """Before closure the Relay wakes the due job; the next worker run closes it and no scan wakes it again."""
    jobs = env.worker.jobs
    wake, sender = relay(env)
    assert wake.reconcile()["job_wakes"] == 1 and job_id in sender.sent
    cutoff = env.clock()
    raw, _ = jobs.due_step("JOB", cutoff=cutoff)
    assert raw["PK"] == f"JOB#{job_id}"
    assert env.worker.process(job_id) is True
    wake, sender = relay(env)
    assert wake.reconcile() == {"outbox_wakes": 0, "job_wakes": 0, "failures": 0}
    assert sender.sent == []
    assert jobs.due_jobs(limit=50)[0] == []
    assert jobs.due_current("JOB", raw, cutoff=cutoff) is None
    assert jobs.due_step("JOB", cutoff=cutoff) == (None, None)


# ---------------------------------------------------------------------------
# Legacy jobs through the Worker runtime (seeded legacy queued job)
# ---------------------------------------------------------------------------

def legacy_env(store):
    h, seeded, token = seeded_legacy(store)
    queued = seeded.attempts["queued"]
    assert queued["row"].get("course_binding") is None and queued["row"]["state"] == "queued"
    env = SimpleNamespace(worker=h.worker, adapter=h.calculator, now=h.now, clock=h.clock, h=h, token=token)
    return env, queued["attempt_id"], queued["job_id"]


@pytest.mark.parametrize("mode", ["killed", "error"])
def test_legacy_sixth_interruption_fails_once_without_another_call(store, monkeypatch, mode):
    env, attempt_id, job_id = legacy_env(store)
    assert user_row(env.h)["slots"][LEGACY_SLOT]["open_attempts"] == 1
    calls = []
    interrupt(env, monkeypatch, mode, calls)
    interrupt_to_limit(env, job_id, mode, calls)
    assert_relay_stops_after_close(env, job_id)
    assert len(calls) == CALCULATION_RESTART_LIMIT + 1
    job = env.worker.jobs.get_job(job_id)
    assert job["state"] == "failed" and job["error_code"] == "CALCULATION_FAILED"
    assert job["failure_basis"] == RESTART_LIMIT_BASIS
    assert job["calculation_restarts"] == CALCULATION_RESTART_LIMIT
    assert job["owner"] is None and job["candidate_ref"] is None and job["final_ref"] is None
    assert "GSI1PK" not in job and "terminal_seal" not in job
    stored = attempt_row(env.h, attempt_id)
    assert stored["state"] == "failed" and stored["error_code"] == "CALCULATION_FAILED"
    assert stored["evaluation"] is None and stored["progress_application"] is None
    assert stored["active_counted"] is False and stored.get("result_ref") is None
    slot = user_row(env.h)["slots"][LEGACY_SLOT]
    assert slot["completed"] is False and slot["open_attempts"] == 0
    reply = calculation(env.h, env.token, attempt_id)
    assert reply.status == 503 and reply.error["code"] == "CALCULATION_FAILED"
    assert env.worker.process(job_id) is True  # A repeated wake is acknowledged, not recalculated.
    assert len(calls) == CALCULATION_RESTART_LIMIT + 1


def test_legacy_candidate_from_the_last_allowed_call_is_resumed_not_closed(store, monkeypatch):
    env, attempt_id, job_id = legacy_env(store)
    calls = []
    interrupt(env, monkeypatch, "killed", calls, real_after=CALCULATION_RESTART_LIMIT)
    save = env.worker.storage.save_calculation

    def saved_then_killed(*args):
        save(*args)
        raise ProcessKilled()  # Candidate file exists; its DB reference was never committed.
    monkeypatch.setattr(env.worker.storage, "save_calculation", saved_then_killed)
    interrupt_to_limit(env, job_id, "killed", calls)
    assert env.worker.jobs.get_job(job_id)["candidate_ref"] is None
    assert env.worker.process(job_id) is True
    assert len(calls) == CALCULATION_RESTART_LIMIT + 1  # Resumed with zero recalculation.
    stored = attempt_row(env.h, attempt_id)
    assert stored["state"] == "evaluated" and stored["evaluation"]["program_completed"] is True
    assert stored["progress_application"]["reason"] == "APPLIED"
    assert user_row(env.h)["slots"][LEGACY_SLOT]["completed_by_attempt"] == attempt_id
    job = env.worker.jobs.get_job(job_id)
    assert job["state"] == "done" and "failure_basis" not in job


# ---------------------------------------------------------------------------
# Course jobs (assigned fixture learner, final assessment)
# ---------------------------------------------------------------------------

def course_key(attempt, kind):
    return (f"COURSE#{attempt['course_binding']['scope_key']}", f"EPOCH#{attempt['epoch']}#{kind}")


def final_attempt(env):
    """Valid prerequisite progress isolates final recovery; the HTTP suite learns every item."""
    view = env.app.course_service.get_course(env.auth, course_id=101, enrollment_id=501)
    head = raw_row(env.client, env.table, f"COURSE#{view.scope_key}", f"EPOCH#{view.gate.epoch}#HEAD")
    progress = completed_progress(view.bundle, 1001, 1002, 1003, 1004)
    head.update(progress_json=json_bytes(progress).decode(), completed_placements=progress["completed_placements"],
                revision=head["revision"] + 1)
    env.client.put_item(TableName=env.table, Item=encode_item(head))
    return start_attempt(env, 1005)


def final_row(env, attempt):
    return raw_row(env.client, env.table, *course_key(attempt, "FINAL"))


def head_row(env, attempt):
    return raw_row(env.client, env.table, *course_key(attempt, "HEAD"))


def saved(env, attempt):
    return raw_row(env.client, env.table, f"ATTEMPT#{attempt['attempt_id']}", "META")


def course_user(env):
    return raw_row(env.client, env.table, f"USER#{env.auth.principal}", "STATE")


def course_http_result(env, attempt):
    response = handle(event("GET", f"/api/v2/attempts/{attempt['attempt_id']}/calculation/", token=env.token),
                      SimpleNamespace(aws_request_id="restart-limit"), env.app)
    return response["statusCode"], json.loads(response["body"])


@pytest.mark.parametrize("mode", ["killed", "error"])
def test_course_sixth_interruption_seals_and_releases_only_own_final(dynamodb_client, store, monkeypatch, mode):
    env = runtime(dynamodb_client, store.table)
    attempt = final_attempt(env)
    job_id = submit(env, attempt)
    head = head_row(env, attempt)
    user = course_user(env)
    calls = []
    interrupt(env, monkeypatch, mode, calls)
    interrupt_to_limit(env, job_id, mode, calls)
    # A killed call leaves the FINAL active; a deferral marks it recovery_required. Neither unlocks it.
    assert final_row(env, attempt)["phase"] == ("active" if mode == "killed" else "recovery_required")
    assert_relay_stops_after_close(env, job_id)
    assert len(calls) == CALCULATION_RESTART_LIMIT + 1
    job = env.worker.jobs.get_job(job_id)
    seal = job["terminal_seal"]
    assert job["state"] == "failed" and job["error_code"] == "CALCULATION_FAILED"
    assert seal["basis"] == job["failure_basis"] == RESTART_LIMIT_BASIS
    assert (seal["job_id"], seal["attempt_id"], seal["epoch"], seal["call_id"]) == (
        job_id, attempt["attempt_id"], attempt["epoch"], job["call_id"])
    assert "GSI1PK" not in job and job["owner"] is None and job["candidate_ref"] is None
    stored = saved(env, attempt)
    assert stored["state"] == "failed" and stored["error_code"] == "CALCULATION_FAILED"
    assert stored["evaluation"] is None and stored["progress_application"] is None
    assert stored.get("result_ref") is None and stored.get("submit_arc") is None
    final = final_row(env, attempt)
    assert final["phase"] == "free" and final["active_attempt_id"] is None
    after = head_row(env, attempt)
    assert after["revision"] > head["revision"]
    assert (after["progress_json"], after["completed_placements"]) == (
        head["progress_json"], head["completed_placements"])  # No progress, completion or score applied.
    assert course_user(env) == user  # The USER row is only a condition of the seal.
    keys = [row["PK"]["S"] for row in dynamodb_client.scan(TableName=store.table)["Items"]]
    assert not any(key.startswith("SUBMISSION#") for key in keys)
    status, body = course_http_result(env, attempt)
    assert status == 503 and body["error"]["code"] == "CALCULATION_FAILED"
    assert env.worker.process(job_id) is True  # Sealed: no further claim or calculation.
    assert len(calls) == CALCULATION_RESTART_LIMIT + 1
    assert start_attempt(env, 1005)["attempt_id"] != attempt["attempt_id"]


def test_course_candidate_from_the_last_allowed_call_is_resumed_not_sealed(dynamodb_client, store, monkeypatch):
    env = runtime(dynamodb_client, store.table)
    attempt = final_attempt(env)
    job_id = submit(env, attempt)
    calls = []
    interrupt(env, monkeypatch, "killed", calls, real_after=CALCULATION_RESTART_LIMIT)
    save = env.worker.storage.save_calculation

    def saved_then_killed(*args):
        save(*args)
        raise ProcessKilled()
    monkeypatch.setattr(env.worker.storage, "save_calculation", saved_then_killed)
    interrupt_to_limit(env, job_id, "killed", calls)
    assert env.worker.process(job_id) is True
    assert len(calls) == CALCULATION_RESTART_LIMIT + 1
    stored = saved(env, attempt)
    assert stored["state"] == "evaluated" and stored["evaluation"]["program_completed"] is True
    assert final_row(env, attempt)["phase"] == "passed"
    assert env.worker.jobs.get_job(job_id).get("terminal_seal") is None


def course_rows(env, attempt, job_id):
    return (env.worker.jobs.get_job(job_id), saved(env, attempt), final_row(env, attempt),
            head_row(env, attempt), course_user(env))


def test_course_restart_seal_requires_limit_fresh_evidence_and_is_idempotent(dynamodb_client, store, monkeypatch):
    env = runtime(dynamodb_client, store.table)
    attempt = final_attempt(env)
    job_id = submit(env, attempt)
    calls = []
    interrupt(env, monkeypatch, "killed", calls)
    jobs, recovery = env.worker.jobs, env.worker.course_recovery
    for _ in range(CALCULATION_RESTART_LIMIT):
        interrupted_run(env, job_id, "killed")
    _, claimed = jobs.claim(job_id, "below-limit", 60)
    evidence = recovery.inspect(job_id, "below-limit", claimed["fence"])
    assert evidence.action == "retry_local_call" and claimed["calculation_restarts"] == CALCULATION_RESTART_LIMIT - 1
    before = course_rows(env, attempt, job_id)
    with pytest.raises(JourneyError) as error:
        jobs.close_course_restart_limit(job_id, "below-limit", claimed["fence"], evidence)
    assert error.value.code == "INVALID_STATE"
    assert course_rows(env, attempt, job_id) == before
    env.now[0] += 61
    interrupted_run(env, job_id, "killed")
    _, claimed = jobs.claim(job_id, "closer", 60)
    fence = claimed["fence"]
    stale = recovery.inspect(job_id, "closer", fence)
    jobs.renew_lease(job_id, "closer", fence, 60)
    before = course_rows(env, attempt, job_id)
    with pytest.raises(JourneyError):
        jobs.close_course_restart_limit(job_id, "closer", fence, stale)
    with pytest.raises((JourneyError, CourseError)):
        jobs.close_course_restart_limit(job_id, "old-owner", fence - 1, stale)
    assert course_rows(env, attempt, job_id) == before
    fresh = recovery.inspect(job_id, "closer", fence)
    closed = jobs.close_course_restart_limit(job_id, "closer", fence, fresh)
    assert closed["state"] == "failed" and final_row(env, attempt)["phase"] == "free"
    sealed = course_rows(env, attempt, job_id)
    assert jobs.close_course_restart_limit(job_id, "closer", fence, fresh) == closed  # Same seal: no write.
    assert course_rows(env, attempt, job_id) == sealed
    with pytest.raises(JourneyError) as error:
        jobs.claim(job_id, "late-worker", 60)
    assert error.value.code == "INVALID_STATE"
    assert len(calls) == CALCULATION_RESTART_LIMIT + 1


# ---------------------------------------------------------------------------
# Legacy transaction predicates on the seeded legacy job (table with GSI1)
# ---------------------------------------------------------------------------

class LegacyJobs:
    """The seeded queued legacy job driven directly through the job repository."""

    def __init__(self, store):
        self.h, seeded, _ = seeded_legacy(store)
        self.attempt_id, self.job_id = seeded.attempts["queued"]["attempt_id"], seeded.attempts["queued"]["job_id"]
        self.client, self.table = store.client, store.table
        self.jobs = self.job_repo(self.client)

    def job_repo(self, client):
        return DynamoJobRepository(DynamoStateRepository(client, self.table, clock=self.h.clock))

    def started(self, *, owner="worker-a"):
        action, job = self.jobs.claim(self.job_id, owner, 60)
        assert action == "execute"
        permitted, job = self.jobs.begin_calculation(
            job["job_id"], owner, job["fence"], str(uuid.uuid4()),
            {"bucket": "private-test-bucket", "key": "calculation/" + uuid.uuid4().hex},
        )
        assert permitted
        return job

    def rows(self):
        return (self.jobs.get_job(self.job_id), attempt_row(self.h, self.attempt_id), user_row(self.h))


def restarted_to_limit(w):
    job = w.started()
    for number in range(1, CALCULATION_RESTART_LIMIT + 1):
        w.h.advance(61)
        owner = f"worker-{number}"
        action, job = w.jobs.claim(job["job_id"], owner, 60)
        assert action == "recover"
        allowed, job = w.jobs.begin_calculation(
            job["job_id"], owner, job["fence"], str(uuid.uuid4()),
            {"bucket": "private-test-bucket", "key": "calculation/" + uuid.uuid4().hex},
            previous_call_id=job["call_id"])
        assert allowed and job["calculation_restarts"] == number
    w.h.advance(61)
    action, job = w.jobs.claim(job["job_id"], "closer", 60)
    assert action == "recover"
    return job


class ConflictingClient:
    """Every transaction loses the USER revision race; conditions are real DynamoDB Local."""

    def __init__(self, world):
        self.world = world

    def __getattr__(self, name):
        return getattr(self.world.client, name)

    def transact_write_items(self, **kwargs):
        self.world.client.update_item(
            TableName=self.world.table, Key=encode_item({"PK": "USER#" + LEGACY_PRINCIPAL, "SK": "STATE"}),
            UpdateExpression="SET #revision = #revision + :one",
            ExpressionAttributeNames={"#revision": "revision"}, ExpressionAttributeValues={":one": {"N": "1"}},
        )
        return self.world.client.transact_write_items(**kwargs)


def test_legacy_limit_refuses_sixth_call_and_closes_atomically(store):
    w = LegacyJobs(store)
    job = restarted_to_limit(w)
    allowed, same = w.jobs.begin_calculation(
        job["job_id"], "closer", job["fence"], str(uuid.uuid4()),
        {"bucket": "private-test-bucket", "key": "calculation/sixth"}, previous_call_id=job["call_id"])
    assert allowed is False and same == w.jobs.get_job(job["job_id"]) == job
    before = w.rows()
    assert before[2]["slots"][LEGACY_SLOT]["open_attempts"] == 1
    with pytest.raises(JobLeaseLost):
        w.jobs.close_restart_limit(job["job_id"], "worker-5", job["fence"] - 1)
    with pytest.raises(JourneyError) as error:
        w.job_repo(ConflictingClient(w)).close_restart_limit(job["job_id"], "closer", job["fence"])
    assert error.value.code == "TEMPORARILY_UNAVAILABLE"
    after = w.rows()
    assert after[:2] == before[:2]  # Neither JOB nor ATTEMPT changed without the USER write.
    assert after[2]["slots"][LEGACY_SLOT]["open_attempts"] == before[2]["slots"][LEGACY_SLOT]["open_attempts"] == 1
    closed = w.jobs.close_restart_limit(job["job_id"], "closer", job["fence"])
    assert closed["state"] == "failed" and closed["failure_basis"] == RESTART_LIMIT_BASIS
    stored = attempt_row(w.h, w.attempt_id)
    assert stored["state"] == "failed" and stored["error_code"] == "CALCULATION_FAILED"
    assert stored["evaluation"] is None and stored["progress_application"] is None
    assert user_row(w.h)["slots"][LEGACY_SLOT] == {**after[2]["slots"][LEGACY_SLOT], "open_attempts": 0}
    assert w.jobs.claim(job["job_id"], "late", 60)[0] == "done"
    assert w.jobs.due_jobs(limit=20)[0] == []
    with pytest.raises(JobLeaseLost):
        w.jobs.close_restart_limit(job["job_id"], "closer", job["fence"])
    assert w.jobs.get_job(job["job_id"]) == closed


def test_legacy_limit_close_requires_the_limit(store):
    w = LegacyJobs(store)
    job = w.started()
    w.h.advance(61)
    _, job = w.jobs.claim(job["job_id"], "recoverer", 60)
    before = w.rows()
    with pytest.raises(JourneyError) as error:
        w.jobs.close_restart_limit(job["job_id"], "recoverer", job["fence"])
    assert error.value.code == "INVALID_STATE"
    assert w.rows() == before

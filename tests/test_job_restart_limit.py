"""Q4 restart limit, Q17 legacy reason order and legacy slots (R1-R3); SDK calls are scripted, not emulated.

Real DynamoDB condition semantics and the full worker runtime are covered in
integration_tests/test_job_restart_limit_dynamodb.py.
"""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from mock_journey.course_submission import _progress as course_progress
from mock_journey.errors import JourneyError
from mock_journey.jobs import (
    CALCULATION_RESTART_LIMIT, RESTART_LIMIT_BASIS, JobLeaseLost, calculation_restarts,
)
from mock_journey.state import decode_item
from mock_journey.worker import JourneyWorker
from services.operational_logs import _operation_fields
from tests.job_restart_support import (  # noqa: F401 (re-export)
    DEFINITION, FINAL_REF, PENDING_DEFINITION, SLOT, attempt_row, close_calls, evaluation, finalize_job, publication,
    repo, started_job, user_row,
)
from tests.mock_state_support import item


def written(client):
    """Decoded Put items and condition keys of the one transaction that was sent."""
    operation, request = client.calls[-1]
    assert operation == "write"
    puts, checks = {}, []
    for action in request["TransactItems"]:
        if "Put" in action:
            row = decode_item(action["Put"]["Item"])
            puts[row["PK"].split("#", 1)[0]] = (row, action["Put"])
        else:
            checks.append(decode_item(action["ConditionCheck"]["Key"]))
    return puts, checks


def begin(jobs, **kwargs):
    return jobs.begin_calculation("job", "worker", 2, "new-call", {"bucket": "b", "key": "new"},
                                  previous_call_id="old-call", **kwargs)


# (a)/(c) Counting happens in the CAS that issues the replacement call.

def test_approved_limit_is_five_restarts():
    # User decision Q4=A (2026-09-28): N=5; the sixth confirmed interruption closes the job.
    assert CALCULATION_RESTART_LIMIT == 5 and RESTART_LIMIT_BASIS == "calculation_restart_limit"


@pytest.mark.parametrize("stored,expected", [(None, 1), (0, 1), (3, 4), (4, 5)])
def test_restart_is_counted_in_the_same_conditioned_write(stored, expected):
    job = started_job() if stored is None else started_job(calculation_restarts=stored)
    jobs, client = repo([("get", {"Item": item(job)}), ("write", {})])
    allowed, updated = begin(jobs)
    assert allowed is True and updated["calculation_restarts"] == expected
    puts, _ = written(client)
    row, put = puts["JOB"]
    assert row["calculation_restarts"] == expected and row["call_id"] == "new-call"
    # The count cannot be committed by a stale owner, fence, lease or revision.
    assert {"owner", "fence", "lease_until", "revision", "call_id"} <= set(put["ExpressionAttributeNames"].values())
    assert decode_item(put["ExpressionAttributeValues"])[":revision"] == 3


def test_first_call_is_not_a_restart_and_older_rows_read_zero():
    job = started_job(call_phase="not_started", call_id=None, planned_candidate_ref=None, execution_fence=0)
    jobs, client = repo([("get", {"Item": item(job)}), ("write", {})])
    allowed, updated = jobs.begin_calculation("job", "worker", 2, "first", {"bucket": "b", "key": "first"})
    assert allowed is True and "calculation_restarts" not in updated
    assert "calculation_restarts" not in written(client)[0]["JOB"][0]
    assert calculation_restarts(updated) == 0


@pytest.mark.parametrize("stored", [CALCULATION_RESTART_LIMIT, CALCULATION_RESTART_LIMIT + 3])
def test_sixth_interruption_never_issues_another_call(stored):
    jobs, client = repo([("get", {"Item": item(started_job(calculation_restarts=stored))})])
    allowed, job = begin(jobs)
    assert allowed is False and job["call_id"] == "old-call"
    assert [operation for operation, _ in client.calls] == ["get"]


@pytest.mark.parametrize("stored", [True, -1, "5", {"count": 1}])
def test_corrupt_restart_count_is_unavailable_without_a_new_call(stored):
    jobs, client = repo([("get", {"Item": item(started_job(calculation_restarts=stored))})])
    with pytest.raises(JourneyError) as error:
        begin(jobs)
    assert error.value.code == "TEMPORARILY_UNAVAILABLE"
    assert [operation for operation, _ in client.calls] == ["get"]


# (d) Legacy closure reuses the existing failure write-set, with its own predicates.


def test_legacy_limit_closure_is_one_failed_write_set_without_progress():
    job = started_job(calculation_restarts=CALCULATION_RESTART_LIMIT)
    jobs, client = repo(close_calls(job))
    result = jobs.close_restart_limit("job", "worker", 2)
    puts, checks = written(client)
    stored_job, put = puts["JOB"]
    assert result == stored_job
    assert stored_job["state"] == "failed" and stored_job["error_code"] == "CALCULATION_FAILED"
    assert stored_job["failure_basis"] == RESTART_LIMIT_BASIS
    assert stored_job["owner"] is None and stored_job["lease_until"] == 0
    assert not {"GSI1PK", "GSI1SK", "next_due_at"} & set(stored_job)  # No further due wake.
    assert stored_job["calculation_restarts"] == CALCULATION_RESTART_LIMIT and stored_job["call_id"] == "old-call"
    assert {"owner", "fence", "lease_until"} <= set(put["ExpressionAttributeNames"].values())
    attempt = puts["ATTEMPT"][0]
    assert attempt["state"] == "failed" and attempt["error_code"] == "CALCULATION_FAILED"
    assert attempt["evaluation"] is None and attempt["progress_application"] is None
    assert attempt["active_counted"] is False and "failure_basis" not in attempt
    user = puts["USER"][0]
    assert user["slots"][SLOT]["open_attempts"] == 0 and user["slots"][SLOT]["completed"] is False
    assert not checks


@pytest.mark.parametrize("change", [
    {"calculation_restarts": CALCULATION_RESTART_LIMIT - 1},
    {"calculation_restarts": CALCULATION_RESTART_LIMIT, "candidate_ref": {"bucket": "b", "key": "old"}},
    {"calculation_restarts": CALCULATION_RESTART_LIMIT, "call_phase": "candidate_saved"},
    {"calculation_restarts": CALCULATION_RESTART_LIMIT, "chart_snapshot": {"kind": "no_chart", "revision": 1}},
    # A committed final file (the lease check would already refuse a done job; the
    # read-side guard is pinned on its own because no write golden can see it).
    {"calculation_restarts": CALCULATION_RESTART_LIMIT, "final_ref": FINAL_REF},
])
def test_legacy_limit_closure_requires_the_limit_and_an_uncommitted_call(change):
    job = started_job(**change)
    jobs, client = repo(close_calls(job, write=False))
    with pytest.raises(JourneyError) as error:
        jobs.close_restart_limit("job", "worker", 2)
    assert error.value.code == "INVALID_STATE"
    assert "write" not in [operation for operation, _ in client.calls]


def test_course_bound_job_cannot_use_the_legacy_failure_path():
    job = started_job(calculation_restarts=CALCULATION_RESTART_LIMIT)
    bound = attempt_row(course_binding={"scope_key": "s"})
    jobs, client = repo(close_calls(job, bound, write=False))
    with pytest.raises(JourneyError):
        jobs.close_restart_limit("job", "worker", 2)
    assert "write" not in [operation for operation, _ in client.calls]


@pytest.mark.parametrize("change", [{"owner": "other"}, {"fence": 3}, {"lease_until": 20}, {"state": "failed"}])
def test_stale_or_finished_lease_cannot_close(change):
    job = started_job(calculation_restarts=CALCULATION_RESTART_LIMIT, **change)
    jobs, client = repo(close_calls(job, write=False))
    with pytest.raises(JobLeaseLost):
        jobs.close_restart_limit("job", "worker", 2)
    assert "write" not in [operation for operation, _ in client.calls]


def test_course_seal_rejects_untyped_evidence_before_any_read():
    jobs, client = repo([])
    for forged in (None, SimpleNamespace(action="retry_local_call", job_id="job", snapshot_json=b"{}")):
        with pytest.raises(JourneyError) as error:
            jobs.close_course_restart_limit("job", "worker", 2, forged)
        assert error.value.code == "INVALID_STATE"
    assert not client.calls


# (f) Q17: legacy finalize uses the course_submission._progress reason order.


@pytest.mark.parametrize("reset,pending,completed,met,passed,reason", [
    (True, True, True, True, True, "PROGRESS_RESET"),
    (True, False, False, True, True, "PROGRESS_RESET"),
    (False, True, True, True, True, "GOAL_POLICY_UNRESOLVED"),
    (False, True, False, True, False, "GOAL_POLICY_UNRESOLVED"),
    # Changed by Q17: an already completed slot wins over a failed retake.
    (False, False, True, False, True, "ALREADY_COMPLETED"),
    (False, False, True, True, False, "ALREADY_COMPLETED"),
    (False, False, True, True, True, "ALREADY_COMPLETED"),
    (False, False, False, True, False, "REQUIREMENTS_NOT_MET"),
    (False, False, False, False, True, "REQUIREMENTS_NOT_MET"),
    (False, False, False, True, True, "APPLIED"),
])
def test_legacy_reason_order_matches_course_progress(reset, pending, completed, met, passed, reason):
    definition = PENDING_DEFINITION if pending else DEFINITION
    job = finalize_job(definition)
    attempt = attempt_row(definition_json=definition)
    user = user_row(completed=completed, epoch="epoch-b" if reset else "epoch-a")
    jobs, client = repo(close_calls(job, attempt, user))
    result = jobs.finalize("job", "worker", 2, {"bucket": "b", "key": "final", "sha256": "c" * 64, "size": 1},
                           evaluation(met=met, passed=passed, pending=pending), publication(job))
    applied = reason == "APPLIED"
    assert result["progress_application"] == {
        "applied": applied, "applied_epoch": "epoch-a" if applied else None, "reason": reason,
    }
    # The v2 function itself gives the same answer (its evaluations always carry goal.status).
    course_view = deepcopy(result["evaluation"])
    course_view["goal"].setdefault("status", "evaluated")
    assert course_progress(course_view, reset=reset, item_already_completed=completed,
                           attempt_epoch="epoch-a") == result["progress_application"]
    puts, checks = written(client)
    if reset:
        assert "USER" not in puts and checks  # Only the epoch/revision condition of USER.
        return
    slot = puts["USER"][0]["slots"][SLOT]
    assert slot["open_attempts"] == 0
    # Only APPLIED changes the completion slot; ALREADY_COMPLETED keeps the original owner.
    assert slot["completed"] is (completed or applied)
    assert slot["completed_by_attempt"] == ("attempt" if applied else None)


# R1-R3: a legacy job whose USER row has no usable slots. Such a row cannot be
# reached from stored data (legacy attempts were created on rows with slots and
# no write removes them, R2); the legacy write-set still never invents a slot.

UNUSABLE_SLOTS = [("absent", ...), ("null", None), ("list", []), ("string", "slots")]


def user_without_usable_slots(slots, *, epoch="epoch-a"):
    user = user_row(epoch=epoch)
    if slots is ...:
        del user["slots"]
    else:
        user["slots"] = slots
    return user


@pytest.mark.parametrize("slots", [row[1] for row in UNUSABLE_SLOTS], ids=[row[0] for row in UNUSABLE_SLOTS])
@pytest.mark.parametrize("counted", [True, False], ids=["close_count", "already_completed_check"])
def test_same_epoch_legacy_finalize_without_usable_slots_fails_closed_without_writing(slots, counted):
    job = finalize_job(DEFINITION)
    attempt = attempt_row(active_counted=counted)
    jobs, client = repo(close_calls(job, attempt, user_without_usable_slots(slots), write=False))
    with pytest.raises(JourneyError) as error:
        jobs.finalize("job", "worker", 2, FINAL_REF, evaluation(met=True, passed=True), publication(job))
    assert error.value.code == "TEMPORARILY_UNAVAILABLE"
    assert [operation for operation, _ in client.calls] == ["get", "read"]


@pytest.mark.parametrize("slots", [row[1] for row in UNUSABLE_SLOTS], ids=[row[0] for row in UNUSABLE_SLOTS])
def test_same_epoch_legacy_failure_without_usable_slots_fails_closed_without_writing(slots):
    jobs, client = repo(close_calls(started_job(), attempt_row(), user_without_usable_slots(slots), write=False))
    with pytest.raises(JourneyError) as error:
        jobs.mark_failed("job", "worker", 2, "STORED_INPUT_INVALID")
    assert error.value.code == "TEMPORARILY_UNAVAILABLE"
    assert [operation for operation, _ in client.calls] == ["get", "read"]


@pytest.mark.parametrize("slots", [row[1] for row in UNUSABLE_SLOTS], ids=[row[0] for row in UNUSABLE_SLOTS])
def test_old_epoch_legacy_finalize_without_usable_slots_only_checks_the_user_row(slots):
    job = finalize_job(DEFINITION)
    user = user_without_usable_slots(slots, epoch="epoch-b")
    jobs, client = repo(close_calls(job, attempt_row(), user))
    result = jobs.finalize("job", "worker", 2, FINAL_REF, evaluation(met=True, passed=True), publication(job))
    assert result["progress_application"] == {"applied": False, "applied_epoch": None, "reason": "PROGRESS_RESET"}
    puts, checks = written(client)
    assert "USER" not in puts and checks == [{"PK": "USER#tester", "SK": "STATE"}]


# Worker: the sixth interruption closes; a valid candidate always resumes.

class Recorder:
    def __init__(self):
        self.events = []

    def record(self, category, name, fields):
        self.events.append((name, _operation_fields(fields)))


class FakeJobs:
    def __init__(self, job, *, course=False):
        self.job, self.course, self.calls = job, course, []
        self.close_error = None

    def is_course_job(self, job_id):
        return self.course

    def get_job(self, job_id):
        return deepcopy(self.job)

    def claim(self, job_id, owner, lease_seconds):
        self.job = {**self.job, "owner": owner, "fence": self.job["fence"] + 1}
        return "recover", deepcopy(self.job)

    def renew_lease(self, *args):
        self.calls.append(("renew_lease",))

    def begin_calculation(self, job_id, owner, fence, call_id, planned, *, previous_call_id=None):
        self.calls.append(("begin_calculation", previous_call_id))
        return False, deepcopy(self.job)

    def mark_calculation_saved(self, job_id, owner, fence, candidate_ref):
        self.calls.append(("mark_calculation_saved",))
        raise JobLeaseLost()  # Stop after the resume decision; later stages are tested elsewhere.

    def _close(self, name, *args):
        self.calls.append((name,) + args)
        if self.close_error is not None:
            raise self.close_error

    def close_restart_limit(self, job_id, owner, fence):
        self._close("close_restart_limit", fence)

    def close_course_restart_limit(self, job_id, owner, fence, evidence):
        self._close("close_course_restart_limit", fence, evidence.action)

    def defer_course_recovery(self, *args, **kwargs):
        self.calls.append(("defer_course_recovery",))


class FakeStorage:
    def __init__(self, candidate):
        self.candidate = candidate

    def load_input(self, reference, binding):
        return SimpleNamespace(projected=SimpleNamespace(payload={}))

    def load_calculation(self, planned, binding):
        return self.candidate

    def planned_calculation(self, binding):
        return {"bucket": "b", "key": "new"}


class Adapter:
    can_calculate = True

    def calculate(self, *args):
        pytest.fail("A restart-limited job invoked the calculator.")


class FakeRecovery:
    def __init__(self, *actions):
        self.actions = list(actions)

    def inspect(self, job_id, owner, fence):
        return SimpleNamespace(action=self.actions.pop(0))


def worker_for(jobs, *, candidate=None, recovery=None):
    recorder = Recorder()
    worker = JourneyWorker(jobs, FakeStorage(candidate), SimpleNamespace(resolve=lambda *a: Adapter()),
                           lease_seconds=60, retry_seconds=5, clock=lambda: 20, operations=recorder,
                           completion_plan=object() if recovery else None)
    worker.course_recovery = recovery
    return worker, recorder


JOB_ID = "8d7f2a3c-1b4e-4f5a-9c6d-7e8f9a0b1c2d"


def worker_job(restarts):
    job = {**started_job(job_id=JOB_ID, fence=1), "owner": None, "lease_until": 0}
    return job if restarts is None else {**job, "calculation_restarts": restarts}


def test_worker_restarts_below_the_limit_and_older_rows_count_zero():
    for restarts in (None, CALCULATION_RESTART_LIMIT - 1):
        jobs = FakeJobs(worker_job(restarts))
        worker, _ = worker_for(jobs)
        assert worker.process(JOB_ID) is False
        assert ("begin_calculation", "old-call") in jobs.calls
        assert not any(call[0] == "close_restart_limit" for call in jobs.calls)


def test_worker_closes_legacy_job_at_the_limit_without_calculating():
    jobs = FakeJobs(worker_job(CALCULATION_RESTART_LIMIT))
    worker, recorder = worker_for(jobs)
    assert worker.process(JOB_ID) is True
    assert jobs.calls == [("close_restart_limit", 2)]  # No heartbeat, begin or calculator call first.
    failed = [fields for name, fields in recorder.events if name == "calculation_failed"]
    assert len(failed) == 1
    assert failed[0]["error_code"] == "CALCULATION_FAILED" and failed[0]["state"] == "failed"
    assert failed[0]["job_id"] == JOB_ID
    assert (failed[0]["failure_basis"], failed[0]["calculation_restarts"]) == ("calculation_restart_limit", 5)


def test_closure_log_reports_the_stored_restart_count_not_the_limit():
    # A row past the limit cannot be produced by begin_calculation; the closure
    # still reports the count it read (the guard is >=), not the constant.
    jobs = FakeJobs(worker_job(CALCULATION_RESTART_LIMIT + 2))
    worker, recorder = worker_for(jobs)
    assert worker.process(JOB_ID) is True and jobs.calls == [("close_restart_limit", 2)]
    failed = [fields for name, fields in recorder.events if name == "calculation_failed"]
    assert len(failed) == 1 and failed[0]["calculation_restarts"] == 7


@pytest.mark.parametrize("error", [JobLeaseLost(), JourneyError("INVALID_STATE"), JourneyError("TEMPORARILY_UNAVAILABLE")])
def test_refused_closure_only_defers(error):
    jobs = FakeJobs(worker_job(CALCULATION_RESTART_LIMIT))
    jobs.close_error = error
    worker, recorder = worker_for(jobs)
    assert worker.process(JOB_ID) is False
    assert [name for name, _ in recorder.events if name.startswith("calculation_")][-1] == "calculation_deferred"
    assert not any(name == "calculation_failed" for name, _ in recorder.events)


def test_valid_candidate_resumes_regardless_of_the_limit():
    jobs = FakeJobs(worker_job(CALCULATION_RESTART_LIMIT + 1))
    worker, _ = worker_for(jobs, candidate=b"{}")
    assert worker.process(JOB_ID) is False  # FakeJobs stops right after the resume commit.
    assert ("mark_calculation_saved",) in jobs.calls
    assert not any(call[0] in {"begin_calculation", "close_restart_limit"} for call in jobs.calls)


def test_worker_seals_course_job_with_fresh_retry_evidence():
    jobs = FakeJobs(worker_job(CALCULATION_RESTART_LIMIT), course=True)
    worker, recorder = worker_for(jobs, recovery=FakeRecovery("retry_local_call", "retry_local_call"))
    assert worker.process(JOB_ID) is True
    assert ("close_course_restart_limit", 2, "retry_local_call") in jobs.calls
    assert not any(call[0] in {"begin_calculation", "close_restart_limit", "defer_course_recovery"}
                   for call in jobs.calls)
    assert any(name == "calculation_failed" for name, _ in recorder.events)


def test_course_closure_waits_when_fresh_inspection_is_no_longer_a_restart():
    jobs = FakeJobs(worker_job(CALCULATION_RESTART_LIMIT), course=True)
    worker, _ = worker_for(jobs, recovery=FakeRecovery("retry_local_call", "wait_integrity"))
    assert worker.process(JOB_ID) is False
    assert not any(call[0].startswith("close") for call in jobs.calls)


def test_course_candidate_resumes_regardless_of_the_limit():
    jobs = FakeJobs(worker_job(CALCULATION_RESTART_LIMIT), course=True)
    worker, _ = worker_for(jobs, candidate=b"{}", recovery=FakeRecovery("resume_candidate"))
    assert worker.process(JOB_ID) is False
    assert ("mark_calculation_saved",) in jobs.calls
    assert not any(call[0].startswith("close") or call[0] == "begin_calculation" for call in jobs.calls)

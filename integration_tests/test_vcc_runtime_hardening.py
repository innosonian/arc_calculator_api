"""Real DynamoDB transactions and real binary calculation at VCC runtime boundaries."""

from types import SimpleNamespace
from dataclasses import replace

import pytest

from mock_journey.course_errors import CourseError
from mock_journey.course_contracts import AssignmentBinding, sealed_bundle
from mock_journey.course_response import course_detail_data
from mock_journey.errors import JourneyError
from mock_journey.state import _encode
from mock_journey.worker import JourneyWorker
from tests.vcc_runtime_support import runtime, start_attempt, submit
from tests.test_vcc_policy import completed_progress
from mock_journey.typed import json_bytes, parse_json


def final_attempt(env):
    """Valid prerequisite fixture isolates final recovery; HTTP suite learns all items."""
    view = env.app.course_service.get_course(env.auth, course_id=101, enrollment_id=501)
    key = {"PK": f"COURSE#{view.scope_key}", "SK": f"EPOCH#{view.gate.epoch}#HEAD"}
    head = env.app.state.control_get(key)
    progress = completed_progress(view.bundle, 1001, 1002, 1003, 1004)
    head.update(progress_json=json_bytes(progress).decode(),
                completed_placements=progress["completed_placements"], revision=head["revision"]+1)
    env.client.put_item(TableName=env.table, Item=_encode(head))
    return start_attempt(env, 1005)


def final_row(env, attempt):
    return env.app.state.control_get({"PK": f"COURSE#{attempt['course_binding']['scope_key']}",
        "SK": f"EPOCH#{attempt['epoch']}#FINAL"})


def saved(env, attempt):
    return env.app.state.get_attempt(env.auth, attempt["attempt_id"])


def test_missing_completion_plan_never_claims_or_writes_legacy_slot(dynamodb_client, dynamodb_table):
    env = runtime(dynamodb_client, dynamodb_table)
    attempt = start_attempt(env, 1003)
    job_id = submit(env, attempt)
    worker = JourneyWorker(env.worker.jobs, env.worker.storage, env.worker.adapters,
                           lease_seconds=60, retry_seconds=5, clock=env.clock)
    assert worker.process(job_id) is False
    assert env.worker.jobs.get_job(job_id)["state"] == "queued"
    assert saved(env, attempt)["state"] == "queued"
    assert env.app.state.get_progress(env.auth)["slots"]["mock-compression-only:adult"]["completed"] is False
    assert env.worker.process(job_id) is True
    view = env.app.course_service.get_course(env.auth, course_id=101, enrollment_id=501)
    item = next(p for p in course_detail_data(view)["courseItems"] if p["courseItemLinkId"] == 1003)
    assert item["isCompleted"] is True
    assert saved(env, attempt)["submit_arc"]["status"] == "disabled"
    assert env.app.state.get_progress(env.auth)["slots"]["mock-compression-only:adult"]["completed"] is False


def test_app_rebuild_reads_same_private_bundle_and_completed_progress(dynamodb_client, dynamodb_table):
    env = runtime(dynamodb_client, dynamodb_table)
    attempt = start_attempt(env, 1003)
    assert env.worker.process(submit(env, attempt)) is True
    before = env.app.course_service.get_course(env.auth, course_id=101, enrollment_id=501)
    rebuilt = env.rebuild()
    assert rebuilt.repository.blob_store is not env.app.repository.blob_store
    after = rebuilt.course_service.get_course(rebuilt.auth.authenticate(env.token), course_id=101, enrollment_id=501)
    assert after.bundle == before.bundle
    assert course_detail_data(after) == course_detail_data(before)


@pytest.mark.parametrize("failure_point", ["before_candidate_commit", "finalize"])
def test_candidate_crash_resumes_without_recalculation(dynamodb_client, dynamodb_table, monkeypatch, failure_point):
    env = runtime(dynamodb_client, dynamodb_table)
    attempt = final_attempt(env)
    job_id = submit(env, attempt)
    calls = []
    calculate = env.adapter.calculate
    def counted(*args):
        calls.append(1)
        return calculate(*args)
    monkeypatch.setattr(env.adapter, "calculate", counted)
    method = "mark_calculation_saved" if failure_point == "before_candidate_commit" else "finalize"
    original = getattr(env.worker.jobs, method)
    def crash(*args, **kwargs):
        raise OSError("injected crash")
    monkeypatch.setattr(env.worker.jobs, method, crash)
    assert env.worker.process(job_id) is False
    assert len(calls) == 1
    assert final_row(env, attempt)["phase"] == "recovery_required"
    monkeypatch.setattr(env.worker.jobs, method, original)
    env.now[0] += 6
    assert env.make_worker().process(job_id) is True
    assert len(calls) == 1
    assert saved(env, attempt)["state"] == "evaluated"
    assert saved(env, attempt)["evaluation"]["program_completed"] is True
    assert final_row(env, attempt)["phase"] == "passed"


def test_lost_committed_candidate_never_recalculates_or_unlocks(dynamodb_client, dynamodb_table, monkeypatch):
    env = runtime(dynamodb_client, dynamodb_table)
    attempt = final_attempt(env)
    job_id = submit(env, attempt)
    original = env.worker.jobs.finalize
    monkeypatch.setattr(env.worker.jobs, "finalize", lambda *a, **k: (_ for _ in ()).throw(OSError("crash")))
    assert env.worker.process(job_id) is False
    job = env.worker.jobs.get_job(job_id)
    ref = job["candidate_ref"]
    del env.objects.objects[(ref["bucket"], ref["key"])]  # This test's private in-memory object only.
    monkeypatch.setattr(env.worker.jobs, "finalize", original)
    monkeypatch.setattr(env.adapter, "calculate", lambda *a: pytest.fail("committed candidate must not recalculate"))
    env.now[0] += 6
    assert env.worker.process(job_id) is False
    assert final_row(env, attempt)["phase"] == "recovery_required"
    assert saved(env, attempt).get("result_ref") is None
    assert env.worker.jobs.get_job(job_id)["candidate_ref"] == ref


def test_unknown_outcome_survives_repeated_worker_deliveries(dynamodb_client, dynamodb_table, monkeypatch):
    env = runtime(dynamodb_client, dynamodb_table)
    attempt = final_attempt(env)
    job_id = submit(env, attempt)
    calls = []
    def unknown(*args):
        calls.append(1)
        raise JourneyError("CALCULATION_OUTCOME_UNKNOWN")
    monkeypatch.setattr(env.adapter, "calculate", unknown)
    for _ in range(4):
        assert env.make_worker().process(job_id) is False
        assert len(calls) == 1
        job = env.worker.jobs.get_job(job_id)
        assert job["error_code"] == "CALCULATION_OUTCOME_UNKNOWN"
        assert job["state"] == "running" and job.get("terminal_seal") is None
        assert saved(env, attempt)["evaluation"] is None
        assert final_row(env, attempt)["phase"] == "recovery_required"
        env.now[0] += 6


@pytest.mark.parametrize("restore_original", [False, True])
def test_final_identity_change_preserves_result_but_holds_current_progress(
    dynamodb_client, dynamodb_table, restore_original,
):
    env = runtime(dynamodb_client, dynamodb_table)
    attempt = final_attempt(env)
    job_id = submit(env, attempt)
    repo = env.app.repository
    original = env.app.course_service.get_course(env.auth, course_id=101, enrollment_id=501).bundle
    last = original.placements[-1]
    identity = parse_json(last.content_identity_json)
    identity["training_program_id"] = "replacement-program"
    changed = sealed_bundle(replace(original, placements=original.placements[:-1] + (
        replace(last, content_identity_json=identity),
    )))
    for bundle in ((changed, original) if restore_original else (changed,)):
        binding = AssignmentBinding(bundle.scope, bundle.public_ids)
        ticket = repo.begin_inventory(env.auth, bundle.scope.learner)
        repo.apply_inventory(env.auth, ticket, (binding,))
        refresh = repo.begin_refresh(env.auth, binding, ticket)
        assert repo.apply_refresh(env.auth, refresh, bundle).state == "reconciliation_required"
    assert env.worker.process(job_id) is True
    result = saved(env, attempt)
    assert result["state"] == "evaluated" and result["evaluation"]["program_completed"] is True
    assert result["progress_application"] == {
        "applied": False, "applied_epoch": None, "reason": "PROGRESS_RECONCILIATION_REQUIRED",
    }
    assert final_row(env, attempt)["phase"] == "passed"
    view = env.app.course_service.get_course(env.auth, course_id=101, enrollment_id=501)
    assert view.gate.state == "reconciliation_required"
    final_item = course_detail_data(view)["courseItems"][-1]
    assert final_item["isCompleted"] is False and final_item["isPassed"] is None


def test_proven_local_error_seals_and_releases_only_own_final(dynamodb_client, dynamodb_table, monkeypatch):
    env = runtime(dynamodb_client, dynamodb_table)
    attempt = final_attempt(env)
    job_id = submit(env, attempt)
    def local_failure(*args):
        raise JourneyError("CALCULATION_FAILED")
    monkeypatch.setattr(env.adapter, "calculate", local_failure)
    assert env.worker.process(job_id) is True
    job = env.worker.jobs.get_job(job_id)
    assert job["state"] == "failed" and job["terminal_seal"]
    assert "next_due_at" not in job
    assert saved(env, attempt)["evaluation"] is None
    assert final_row(env, attempt)["phase"] == "free"
    assert start_attempt(env, 1005)["attempt_id"] != attempt["attempt_id"]
    with pytest.raises(JourneyError) as failure:
        env.worker.jobs.claim(job_id, "late-worker", 60)
    assert failure.value.code == "INVALID_STATE"


def test_failed_unsealed_candidate_job_is_actually_requeued_and_completed(dynamodb_client, dynamodb_table, monkeypatch):
    env = runtime(dynamodb_client, dynamodb_table)
    attempt = final_attempt(env)
    job_id = submit(env, attempt)
    original = env.worker.jobs.finalize
    def old_failure(ident, owner, fence, *args, **kwargs):
        env.worker.jobs.mark_failed(ident, owner, fence, "CALCULATION_FAILED")
        raise JourneyError("CALCULATION_FAILED")
    monkeypatch.setattr(env.worker.jobs, "finalize", old_failure)
    assert env.worker.process(job_id) is False
    assert env.worker.jobs.get_job(job_id)["state"] == "failed"
    monkeypatch.setattr(env.worker.jobs, "finalize", original)
    monkeypatch.setattr(env.adapter, "calculate", lambda *a: pytest.fail("candidate recovery recalculated"))
    assert env.worker.process(job_id) is True
    assert env.worker.jobs.get_job(job_id)["recovery_from"]["state"] == "failed"
    assert saved(env, attempt)["state"] == "evaluated"
    assert final_row(env, attempt)["phase"] == "passed"


def test_forged_and_stale_recovery_evidence_cannot_mutate(dynamodb_client, dynamodb_table, monkeypatch):
    env = runtime(dynamodb_client, dynamodb_table)
    attempt = final_attempt(env)
    job_id = submit(env, attempt)
    monkeypatch.setattr(env.worker.jobs, "finalize", lambda *a, **k: (_ for _ in ()).throw(OSError("crash")))
    assert env.worker.process(job_id) is False
    jobs = env.worker.jobs
    before = jobs.get_job(job_id)
    forged = SimpleNamespace(action="resume_candidate", job_id=job_id)
    with pytest.raises(JourneyError) as failure:
        jobs.reopen_course_recovery(job_id, forged)
    assert failure.value.code == "INVALID_STATE"
    env.now[0] += 6
    _, claimed = jobs.claim(job_id, "verified-worker", 60)
    evidence = env.worker.course_recovery.inspect(job_id, "verified-worker", claimed["fence"])
    assert evidence.action == "resume_candidate"
    jobs.renew_lease(job_id, "verified-worker", claimed["fence"], 60)
    snapshot = jobs.get_job(job_id)
    with pytest.raises((JourneyError, CourseError)):
        jobs.reopen_course_recovery(job_id, evidence)
    assert jobs.get_job(job_id) == snapshot
    assert final_row(env, attempt)["active_attempt_id"] == attempt["attempt_id"]
    assert before.get("terminal_seal") is None

"""Course Worker recovery on real DynamoDB Local through the public /api/v2 harness.

Moved from integration_tests/test_mock_journey.py (v1 routes) to course
attempts. Every scenario uses a test-only adapter registered under the current
adapter/projection versions (ScriptedCalculator; values test state, not
scoring). The expected outcomes follow the course recovery rules
(mock_journey/course_recovery.py, jobs.defer_course_recovery): an unproven
failure keeps the attempt ``outcome_unknown`` with its evidence, a final
assessment stays ``recovery_required`` instead of being unlocked, and nothing
is recalculated or turned into an educational Fail. The legacy (no course
binding) Worker paths that D103 keeps are in test_legacy_worker_recovery.py.
"""

import json

import pytest

from integration_tests.worker_journey_support import (  # noqa: F401 (store fixture)
    DUMMY_SUBMISSION, TYPE_MARKER, ScriptedCalculator, accepted, attempt_row, calculation, chart_link,
    course_rows, item_view, job_row, journey, progress_facts, recovery_facts, start, start_reply, store, submit,
)
from mock_journey.catalog import PROGRAMS, TARGETS
from mock_journey.contracts import CalculatorRegistry
from mock_journey.errors import JourneyError
from mock_journey.worker import call_binding
from tests.journey_support import MEASUREMENT, dummy_course


CYCLE_PROGRAMS = {"mock-cpr", "mock-two-rescuer-cpr", "mock-two-rescuer-aed"}
COMPRESSION = ("mock-compression-only", "adult")


def unavailable(*args, **kwargs):
    raise JourneyError("TEMPORARILY_UNAVAILABLE")


def world(store):
    h = journey(store, ScriptedCalculator())
    return h, dummy_course(*COMPRESSION)


def accepted_role(h, course, role, **kwargs):
    """A practice attempt, or a final assessment after the practice item was completed."""
    token = h.login().token
    if role == "final":
        _, _, practice_job = accepted(h, course, token=token, data=MEASUREMENT)
        assert h.work(job_id=practice_job) is True
        h.calculator.calls.clear()
        h.calculator.inputs.clear()
    return accepted(h, course, token=token, role=role, data=MEASUREMENT, **kwargs)


def assert_outcome_unknown(h, token, attempt_id, error_code):
    stored = attempt_row(h, attempt_id)
    assert stored["state"] == "outcome_unknown" and stored["error_code"] == error_code
    assert stored["evaluation"] is None and stored["progress_application"] is None
    assert stored.get("result_ref") is None and stored["active_counted"] is False
    reply = calculation(h, token, attempt_id)
    assert reply.status == 503 and reply.error["code"] == "CALCULATION_OUTCOME_UNKNOWN"
    return stored


@pytest.mark.parametrize("program,target", [(program[0], target) for program in PROGRAMS for target in TARGETS])
def test_each_dummy_course_accepts_scores_once_and_commits_the_completion_contract(store, program, target):
    h = journey(store, ScriptedCalculator())
    course = dummy_course(program, target)
    # D136: cycles goals are evaluated too; the scripted adapter reports the required count.
    _, _, kind, required = next(row for row in PROGRAMS if row[0] == program)
    assert (program in CYCLE_PROGRAMS) is (kind == "cycles")
    token, attempt, job_id = accepted(h, course, data=MEASUREMENT)
    assert h.work(job_id=job_id) is True
    assert h.work(job_id=job_id) is True
    assert len(h.calculator.calls) == 1 and h.calculator.inputs == [MEASUREMENT]
    reply = calculation(h, token, attempt["attemptId"])
    assert reply.status == 200 and reply.data["calculationStatus"] == "succeeded"
    body = reply.data["calculation"]
    assert body["type_preservation"] == TYPE_MARKER
    assert [type(value) for value in body["type_preservation"]] == [int, float, type(None), bool, str]
    assert not {"submit_arc", "submit_hstm", "hstm_document", "evaluation"} & set(body)
    assert reply.data["submit_arc"] == DUMMY_SUBMISSION
    stored = attempt_row(h, attempt["attemptId"])
    assert stored["evaluation"] == reply.data["evaluation"]
    assert stored["evaluation"]["program_completed"] is True
    assert stored["evaluation"]["goal"]["status"] == "evaluated"
    assert stored["evaluation"]["goal"] == {"kind": kind, "required": required, "observed": required,
                                            "met": True, "status": "evaluated"}
    assert stored["progress_application"]["reason"] == "APPLIED"
    replay = submit(h, token, attempt, data=MEASUREMENT, extra={"access_token": "PRIVATE-TOKEN-MARKER"})
    assert replay.status == 200 and replay.body == reply.body
    assert not any(b"PRIVATE-TOKEN-MARKER" in value["Body"] for value in h.objects.objects.values())
    assert len(h.calculator.calls) == 1
    assert item_view(h, token, course, course.practice_link_id)["isCompleted"] is True
    # D130: completed or not, the practice item accepts another start.
    later = start_reply(h, token, course)
    assert later.status == 201, later.body
    assert item_view(h, token, course, course.practice_link_id)["isCompleted"] is True


def test_response_certification_override_cannot_grant_course_completion(store):
    h, course = world(store)
    h.calculator.overall = 70
    token, attempt, job_id = accepted(h, course, data=MEASUREMENT, extra={
        "Custom": {"CertificateAdult": True}, "Open_Skill": {"Passing_Score": 0}})
    assert h.work(job_id=job_id) is True
    stored = attempt_row(h, attempt["attemptId"])
    assert stored["evaluation"]["program_completed"] is False
    assert stored["evaluation"]["reason_codes"] == ["SCORE_NOT_PASS"]
    assert stored["progress_application"]["reason"] == "REQUIREMENTS_NOT_MET"
    reply = calculation(h, token, attempt["attemptId"])
    assert reply.data["calculation"]["certification"]["Target"] == "adult"
    assert item_view(h, token, course, course.practice_link_id)["isCompleted"] is False


def test_late_result_after_other_session_logout_is_stored_without_new_progress(store):
    h, course = world(store)
    token, other = h.login().token, h.login().token
    token, attempt, job_id = accepted(h, course, token=token, data=MEASUREMENT)
    original = course_rows(h, attempt["attemptId"])
    h.logout(other)
    h.calculator.on_calculate = lambda: h.advance(31)  # Past the app's 30s wait; never a server deadline.
    assert h.work(job_id=job_id) is True
    stored = attempt_row(h, attempt["attemptId"])
    assert stored["evaluation"]["program_completed"] is True
    assert stored["progress_application"] == {"applied": False, "applied_epoch": None, "reason": "PROGRESS_RESET"}
    reply = calculation(h, token, attempt["attemptId"])
    assert reply.status == 200
    assert reply.data["submit_arc"]["exclusionReasons"] == ["dummy", "progress_reset_before_result"]
    # The original epoch rows are not rewritten and the new epoch starts without completion.
    assert course_rows(h, attempt["attemptId"]) == original
    h.refresh(token)
    assert item_view(h, token, course, course.practice_link_id)["isCompleted"] is False


@pytest.mark.parametrize("role", ["practice", "final"])
def test_saved_candidate_recovers_after_reference_commit_failure_without_recalculation(store, monkeypatch, role):
    h, course = world(store)
    token, attempt, job_id = accepted_role(h, course, role)
    monkeypatch.setattr(h.worker.jobs, "mark_calculation_saved", unavailable)
    assert h.work(job_id=job_id) is False
    assert_outcome_unknown(h, token, attempt["attemptId"], "TEMPORARILY_UNAVAILABLE")
    job = job_row(h, job_id)
    assert job["candidate_ref"] is None and job["owner"] is None
    assert h.worker.storage.load_calculation(job["planned_candidate_ref"], call_binding(job)) is not None
    if role == "final":
        final = course_rows(h, attempt["attemptId"]).final
        assert (final["phase"], final["active_attempt_id"]) == ("recovery_required", attempt["attemptId"])
    monkeypatch.undo()
    h.now[0] = job["next_due_at"]
    assert h.work(job_id=job_id) is True
    result = attempt_row(h, attempt["attemptId"])
    assert result["state"] == "evaluated" and result.get("error_code") is None
    assert result["evaluation"]["program_completed"] is True
    assert job_row(h, job_id)["call_id"] == job["call_id"]
    assert len(h.calculator.calls) == 1
    if role == "final":
        assert course_rows(h, attempt["attemptId"]).final["phase"] == "passed"


def test_missing_candidate_recalculates_under_new_call_and_old_late_write_is_isolated(store):
    h, course = world(store)
    h.calculator.timeout = True
    token, attempt, job_id = accepted(h, course, data=MEASUREMENT)
    assert h.work(job_id=job_id) is False
    old = job_row(h, job_id)
    assert old["call_phase"] == "started" and old["candidate_ref"] is None
    assert h.worker.storage.load_calculation(old["planned_candidate_ref"], call_binding(old)) is None
    assert_outcome_unknown(h, token, attempt["attemptId"], "TEMPORARILY_UNAVAILABLE")
    h.now[0] = old["next_due_at"]
    h.calculator.timeout = False
    h.calculator.overall = 91
    assert h.work(job_id=job_id) is True
    current = job_row(h, job_id)
    assert current["call_id"] != old["call_id"]
    assert current["planned_candidate_ref"] != old["planned_candidate_ref"]
    assert current["execution_fence"] > old["execution_fence"]
    assert current["calculation_restarts"] == 1
    assert len(h.calculator.calls) == 2
    original = calculation(h, token, attempt["attemptId"])
    assert original.data["calculation"]["cpr_score"]["total_score"]["overall"] == 91
    h.worker.storage.save_calculation(old["planned_candidate_ref"], h.calculator.responses[old["call_id"]],
                                      call_binding(old))
    assert calculation(h, token, attempt["attemptId"]).body == original.body
    assert h.work(job_id=job_id) is True
    assert len(h.calculator.calls) == 2


def test_chart_selection_survives_final_commit_failure_and_late_writers(store, monkeypatch):
    h, course = world(store)
    h.calculator.chart_kind = "snapshot"
    token, attempt, job_id = accepted(h, course, data=MEASUREMENT)
    monkeypatch.setattr(h.worker.jobs, "finalize", unavailable)
    assert h.work(job_id=job_id) is False
    selected = job_row(h, job_id)["chart_snapshot"]
    assert selected["kind"] == "snapshot"
    assert_outcome_unknown(h, token, attempt["attemptId"], "TEMPORARILY_UNAVAILABLE")
    h.advance(61)
    monkeypatch.undo()
    assert h.work(job_id=job_id) is True
    job = job_row(h, job_id)
    assert job["chart_snapshot"] == selected and len(h.calculator.calls) == 1 and h.calculator.chart_calls == 1
    final_keys = [key for key in h.objects.put_keys if "-final-" in key]
    assert len(final_keys) == 2 and len(set(final_keys)) == 2
    frozen = calculation(h, token, attempt["attemptId"])
    assert frozen.status == 200
    refreshed = chart_link(h, token, attempt["attemptId"])
    assert refreshed.status == 200 and h.objects.sign_calls[-1][-1] == 300
    assert calculation(h, token, attempt["attemptId"]).body == frozen.body
    h.logout(token)
    assert (h.worker.storage.bucket, job["chart_publication"]["key"]) in h.objects.objects


@pytest.mark.parametrize("role", ["practice", "final"])
def test_missing_retained_adapter_and_tampered_raw_never_invoke_a_fallback(store, role):
    h, course = world(store)
    token, attempt, job_id = accepted_role(h, course, role)
    registry = h.worker.adapters
    h.worker.adapters = CalculatorRegistry([])
    assert h.work(job_id=job_id) is False
    assert job_row(h, job_id)["call_phase"] == "not_started"
    assert_outcome_unknown(h, token, attempt["attemptId"], "TEMPORARILY_UNAVAILABLE")
    h.worker.adapters = registry
    h.advance(61)
    # Only this attempt's raw object is replaced (a final's practice raw was scored earlier).
    raw_key = _attempt_raw_key(h, job_id)
    original = h.objects.objects[raw_key]["Body"]
    assert original == MEASUREMENT
    h.objects.objects[raw_key]["Body"] = b"corrupted"
    for _ in range(2):
        assert h.work(job_id=job_id) is False
        h.advance(61)
    stored = assert_outcome_unknown(h, token, attempt["attemptId"], "STORED_INPUT_INVALID")
    job = job_row(h, job_id)
    assert job["state"] == "running" and job["call_phase"] == "not_started" and "terminal_seal" not in job
    assert not h.calculator.calls
    if role == "final":
        final = course_rows(h, attempt["attemptId"]).final
        assert (final["phase"], final["active_attempt_id"]) == ("recovery_required", attempt["attemptId"])
    # The exact original bytes, not a replacement calculation, recover the attempt.
    h.objects.objects[raw_key]["Body"] = original
    assert h.work(job_id=job_id) is True
    assert attempt_row(h, attempt["attemptId"])["evaluation"]["program_completed"] is True
    assert len(h.calculator.calls) == 1 and h.calculator.inputs == [MEASUREMENT]
    assert stored["input_digest"] == attempt_row(h, attempt["attemptId"])["input_digest"]


def _attempt_raw_key(h, job_id):
    job = job_row(h, job_id)
    ref = job["input_manifest_ref"]
    manifest = json.loads(h.objects.objects[(ref["bucket"], ref["key"])]["Body"])
    return ref["bucket"], manifest["raw_base"] + ".bin"


@pytest.mark.parametrize("role", ["practice", "final"])
def test_committed_candidate_loss_waits_for_integrity_without_recalculation(store, monkeypatch, role):
    h, course = world(store)
    token, attempt, job_id = accepted_role(h, course, role)
    monkeypatch.setattr(h.worker.storage, "save_final", unavailable)
    assert h.work(job_id=job_id) is False
    job = job_row(h, job_id)
    assert job["call_phase"] == "candidate_saved" and job["candidate_ref"] is not None
    del h.objects.objects[(job["candidate_ref"]["bucket"], job["candidate_ref"]["key"])]
    monkeypatch.undo()
    for _ in range(2):
        h.advance(61)
        assert h.work(job_id=job_id) is False
    # The Worker defers with a retryable code; the recovery proof names the lost committed candidate.
    stored = assert_outcome_unknown(h, token, attempt["attemptId"], "TEMPORARILY_UNAVAILABLE")
    current = job_row(h, job_id)
    evidence = h.worker.course_recovery.inspect(job_id, None, current["fence"])
    assert (evidence.candidate_state, evidence.action, evidence.code) == (
        "missing_committed", "wait_integrity", "STORED_INPUT_INVALID")
    assert (current["call_id"], current["candidate_ref"]) == (job["call_id"], job["candidate_ref"])
    assert current["state"] == "running" and "terminal_seal" not in current and stored["job_id"] == job_id
    assert len(h.calculator.calls) == 1
    if role == "final":
        assert course_rows(h, attempt["attemptId"]).final["phase"] == "recovery_required"


@pytest.mark.parametrize("role", ["practice", "final"])
@pytest.mark.parametrize("artifact", ["manifest", "raw", "meta"])
def test_confirmed_input_loss_is_held_without_calculator_execution_or_progress(store, artifact, role):
    h, course = world(store)
    token, attempt, job_id = accepted_role(h, course, role)
    before = progress_facts(course_rows(h, attempt["attemptId"]))
    job = job_row(h, job_id)
    ref = job["input_manifest_ref"]
    manifest = json.loads(h.objects.objects[(ref["bucket"], ref["key"])]["Body"])
    key = ref["key"] if artifact == "manifest" else manifest["raw_base"] + {"raw": ".bin", "meta": ".meta.json"}[artifact]
    del h.objects.objects[(ref["bucket"], key)]
    assert h.work(job_id=job_id) is False
    failed = assert_outcome_unknown(h, token, attempt["attemptId"], "STORED_INPUT_INVALID")
    after = progress_facts(course_rows(h, attempt["attemptId"]))
    if role == "final":
        assert before["final"] == {"phase": "active", "active_attempt_id": attempt["attemptId"]}
        assert after["final"] == {"phase": "recovery_required", "active_attempt_id": attempt["attemptId"]}
        denied = start_reply(h, token, course, role="final")
        assert denied.status == 409 and denied.error["code"] == "FINAL_ASSESSMENT_RECOVERY_REQUIRED"
        before["final"] = after["final"]
    assert after == before
    h.advance(61)
    assert h.work(job_id=job_id) is False
    assert recovery_facts(attempt_row(h, attempt["attemptId"])) == recovery_facts(failed)
    assert progress_facts(course_rows(h, attempt["attemptId"])) == after
    assert not h.calculator.calls


@pytest.mark.parametrize("artifact", ["final", "chart"])
def test_confirmed_result_loss_returns_fixed_error_without_rewriting_completion(store, artifact):
    h, course = world(store)
    h.calculator.chart_kind = "snapshot"
    token, attempt, job_id = accepted(h, course, data=MEASUREMENT)
    assert h.work(job_id=job_id) is True
    evaluated, rows = attempt_row(h, attempt["attemptId"]), course_rows(h, attempt["attemptId"])
    job = job_row(h, job_id)
    ref = job["final_ref"] if artifact == "final" else job["chart_publication"]
    del h.objects.objects[(h.worker.storage.bucket, ref["key"])]
    reply = (calculation if artifact == "final" else chart_link)(h, token, attempt["attemptId"])
    assert reply.status == 503 and reply.error["code"] == "STORED_INPUT_INVALID"
    assert ref["key"] not in json.dumps(reply.body)
    assert h.work(job_id=job_id) is True
    assert attempt_row(h, attempt["attemptId"]) == evaluated
    assert course_rows(h, attempt["attemptId"]) == rows
    assert len(h.calculator.calls) == 1


def test_confirmed_chart_snapshot_loss_cannot_refetch_or_publish_new_data(store, monkeypatch):
    h, course = world(store)
    h.calculator.chart_kind = "snapshot"
    token, attempt, job_id = accepted(h, course, data=MEASUREMENT)
    monkeypatch.setattr(h.worker.storage, "publish_selected_chart", unavailable)
    assert h.work(job_id=job_id) is False
    job = job_row(h, job_id)
    ref = job["chart_snapshot"]["snapshot_ref"]
    del h.objects.objects[(ref["bucket"], ref["key"])]
    monkeypatch.undo()
    for _ in range(2):
        h.advance(61)
        assert h.work(job_id=job_id) is False
    assert_outcome_unknown(h, token, attempt["attemptId"], "STORED_INPUT_INVALID")
    current = job_row(h, job_id)
    assert current["chart_snapshot"] == job["chart_snapshot"] and current["final_ref"] is None
    assert current.get("chart_publication") is None and "terminal_seal" not in current
    assert len(h.calculator.calls) == 1 and h.calculator.chart_calls == 1


def test_input_loss_in_concurrent_attempt_preserves_existing_item_completion(store):
    h, course = world(store)
    token = h.login().token
    first = start(h, token, course)
    second = start(h, token, course)
    for attempt in (first, second):
        assert submit(h, token, attempt, data=MEASUREMENT).status == 202
    assert h.work(first["attemptId"]) is True
    before = progress_facts(course_rows(h, first["attemptId"]))
    assert before["item"]["completed"] is True and before["item"]["completed_by_attempt"] == first["attemptId"]
    second_job = job_row(h, h.job_id(second["attemptId"]))
    ref = second_job["input_manifest_ref"]
    del h.objects.objects[(ref["bucket"], ref["key"])]
    for _ in range(2):
        assert h.work(job_id=second_job["job_id"]) is False
        h.advance(61)
    failed = attempt_row(h, second["attemptId"])
    assert failed["state"] == "outcome_unknown" and failed["evaluation"] is None
    assert progress_facts(course_rows(h, second["attemptId"])) == before
    assert item_view(h, token, course, course.practice_link_id)["isCompleted"] is True
    assert len(h.calculator.calls) == 1

"""L1 real DynamoDB transactions with the bundled calculator and test object IO.

Course attempts through the public /api/v2 harness (Dummy Dev courses, real
internal calculator). The in-memory object IO is deliberate; this is not
evidence for the filesystem, default CLI, or actual app/manikin acceptance
phases. No v1 API route or v1-only service method is used. Legacy (no course
binding) finalize is checked on the captured legacy rows
(tests/fixtures/legacy_mock_v1_rows) with a USER slot already completed.
"""

import json

import pytest

from integration_tests.legacy_slot_support import PREVIOUS_ATTEMPT, complete_legacy_slot, other_slots
from integration_tests.worker_journey_support import (  # noqa: F401 (store fixture)
    DUMMY_SUBMISSION, OneTransactionInterruption, accepted, attempt_row, calculation, course_rows, item_view,
    job_row, jobs_over, journey, progress_facts, seeded_legacy, start_reply, store, submit, user_row,
)
from mock_journey.errors import JourneyError
from mock_journey.contracts import PENDING_GOAL_ADAPTER_VERSION, PENDING_GOAL_PROFILE_VERSION
from mock_journey.worker import call_binding
from tests._synth import WEAK_RAMP, comp_session, cpr_session
from tests.journey_support import LocalDefinitions, dummy_course, encode_item


class PendingDefinitions(LocalDefinitions):
    """Kept for integration_tests/test_local_database_migration.py (another unit's seed)."""

    def get_definition(self, program_id, target):
        definition = super().get_definition(program_id, target)
        definition.update(adapter_version=PENDING_GOAL_ADAPTER_VERSION,
                          profile_version=PENDING_GOAL_PROFILE_VERSION)
        return definition


CPR = dummy_course("mock-cpr", "adult")
CPR_DATA = cpr_session([(30, 2)] * 3)


def unavailable(*args, **kwargs):
    raise JourneyError("TEMPORARILY_UNAVAILABLE")


MINIMUM = "MINIMUM_QUANTITY_NOT_MET"


@pytest.mark.parametrize("program,data,observed,met,decision,complete,reasons", [
    # D136: three closed cpr cycles meet the CPR goal; fewer do not. D139: a session below the ARC
    # minimum quantity (90 compressions and 6 ventilations for an adult) is scored, not nulled, but
    # does not pass: decision "fail" with MINIMUM_QUANTITY_NOT_MET (the score itself is no reason).
    ("mock-cpr", cpr_session([(30, 2)] * 3), 3, True, "pass", True, []),                       # 90 / 6
    ("mock-cpr", cpr_session([(30, 2)]), 1, False, "fail", False, ["GOAL_NOT_MET", MINIMUM]),    # 30 / 2, overall 99
    ("mock-cpr", cpr_session([(30, 2)] * 2 + [(30, 0)]), 2, False, "fail", False, ["GOAL_NOT_MET", MINIMUM]),  # 90 / 4
    # Compression Only has no CPR minimum: 60 (and 59) compressions are judged by goal and score alone.
    ("mock-compression-only", comp_session(60), 60, True, "pass", True, []),
    ("mock-compression-only", comp_session(59), 59, False, "pass", False, ["GOAL_NOT_MET"]),
    ("mock-compression-only", comp_session(60, WEAK_RAMP), 60, True, "fail", False, ["SCORE_NOT_PASS"]),
], ids=("cpr-three-cycles-complete", "cpr-one-cycle-incomplete", "cpr-open-last-group-incomplete",
        "only-pass-complete", "only-short-incomplete", "only-fail-incomplete"))
def test_real_scores_and_completion_commit_once(store, program, data, observed, met, decision, complete, reasons):
    h = journey(store)
    course = dummy_course(program, "adult")
    token, other = h.login().token, h.login().token
    token, attempt, job_id = accepted(h, course, token=token, data=data)
    attempt_id = attempt["attemptId"]
    assert item_view(h, other, course, course.practice_link_id)["isCompleted"] is False
    assert h.work(job_id=job_id) is True
    stored = attempt_row(h, attempt_id)
    assessment = stored["evaluation"]
    assert stored["state"] == "evaluated" and stored["active_counted"] is False
    assert assessment["goal"]["status"] == "evaluated"
    assert assessment["goal"]["observed"] == observed and type(assessment["goal"]["observed"]) is int
    assert assessment["goal"]["met"] is met
    assert assessment["score"]["decision"] == decision
    assert assessment["program_completed"] is complete
    assert assessment["reason_codes"] == reasons
    assert stored["progress_application"]["reason"] == ("APPLIED" if complete else "REQUIREMENTS_NOT_MET")
    shared = h.course(other, course)
    item = next(value for value in shared["courseItems"] if value["courseItemLinkId"] == course.practice_link_id)
    assert item["isCompleted"] is complete
    job = job_row(h, job_id)
    assert job["state"] == "done" and "GSI1PK" not in job and "GSI1SK" not in job
    response = calculation(h, token, attempt_id)
    assert response.status == 200
    body = response.data["calculation"]
    assert response.data["submit_arc"] == DUMMY_SUBMISSION and "submit_arc" not in body
    assert response.data["evaluation"] == assessment and "evaluation" not in body
    assert body["chart_dataset_url"]
    if program == "mock-cpr" and observed == 1:
        # D139: below the ARC minimum the score is shown (99), not null, although the decision is
        # "fail". The null path of the retained v4 adapter is covered by
        # integration_tests/test_eof_truncated_ventilation_dynamodb.py.
        assert body["cpr_score"]["total_score"]["overall"] == 99
    for _ in range(2):
        assert h.work(job_id=job_id) is True
        assert calculation(h, token, attempt_id).body == response.body
        assert submit(h, token, attempt, data=data).body == response.body
    assert len(h.calculator.calls) == 1
    assert attempt_row(h, attempt_id) == stored
    assert h.course(other, course) == shared
    # D130: completed or not, the practice item accepts another start; the shared view is untouched.
    next_attempt = start_reply(h, token, course)
    assert next_attempt.status == 201, next_attempt.body
    assert h.course(other, course) == shared


def test_pending_candidate_recovers_without_calculation_after_reference_commit_failure(store, monkeypatch):
    h = journey(store)
    token, attempt, job_id = accepted(h, CPR, data=CPR_DATA)
    monkeypatch.setattr(h.worker.jobs, "mark_calculation_saved", unavailable)
    assert h.work(job_id=job_id) is False
    old = job_row(h, job_id)
    candidate = h.worker.storage.load_calculation(old["planned_candidate_ref"], call_binding(old))
    assert candidate is not None and old["candidate_ref"] is None
    assert json.loads(candidate)["goal"] == {"kind": "cycles", "observed": 3, "status": "evaluated"}
    assert attempt_row(h, attempt["attemptId"])["state"] == "outcome_unknown"
    monkeypatch.undo()
    monkeypatch.setattr(h.calculator, "calculate", lambda *a, **k: pytest.fail("Saved candidate was recalculated."))
    h.now[0] = old["next_due_at"]
    assert h.work(job_id=job_id) is True
    assert job_row(h, job_id)["call_id"] == old["call_id"]
    stored = attempt_row(h, attempt["attemptId"])
    assert stored["state"] == "evaluated"
    assert stored["evaluation"]["program_completed"] is True
    assert stored["progress_application"]["reason"] == "APPLIED"
    assert item_view(h, token, CPR, CPR.practice_link_id)["isCompleted"] is True
    assert len(h.calculator.calls) == 1


def test_unstored_pending_candidate_rotates_call_and_old_late_write_is_isolated(store):
    h = journey(store)
    token, attempt, job_id = accepted(h, CPR, data=CPR_DATA)

    def interrupted():
        raise RuntimeError("Test interruption before candidate persistence.")

    h.calculator.after_calculate = interrupted
    assert h.work(job_id=job_id) is False
    old = job_row(h, job_id)
    assert h.worker.storage.load_calculation(old["planned_candidate_ref"], call_binding(old)) is None
    h.calculator.after_calculate = lambda: None
    h.now[0] = old["next_due_at"]
    assert h.work(job_id=job_id) is True
    current = job_row(h, job_id)
    assert current["call_id"] != old["call_id"] and current["planned_candidate_ref"] != old["planned_candidate_ref"]
    assert current["execution_fence"] > old["execution_fence"] and current["calculation_restarts"] == 1
    snapshot = calculation(h, token, attempt["attemptId"])
    assert snapshot.status == 200
    saved_rows = course_rows(h, attempt["attemptId"])
    h.worker.storage.save_calculation(old["planned_candidate_ref"], h.calculator.calls[0][1], call_binding(old))
    assert h.work(job_id=job_id) is True
    assert calculation(h, token, attempt["attemptId"]).body == snapshot.body
    assert course_rows(h, attempt["attemptId"]) == saved_rows
    assert len(h.calculator.calls) == 2


def test_logout_at_final_transaction_keeps_pending_result_without_applying_to_new_epoch(store, monkeypatch):
    h = journey(store)
    token, other = h.login().token, h.login().token
    token, attempt, job_id = accepted(h, CPR, token=token, data=CPR_DATA)
    original_epoch = user_row(h)["epoch"]
    original_rows = course_rows(h, attempt["attemptId"])
    wrapped = OneTransactionInterruption(h.store.client, before=lambda: h.logout(other))
    monkeypatch.setattr(h.worker.jobs, "finalize", jobs_over(h, wrapped).finalize)
    assert h.work(job_id=job_id) is True
    assert wrapped.used
    saved = attempt_row(h, attempt["attemptId"])
    assert saved["state"] == "evaluated" and saved["evaluation"]["program_completed"] is True
    assert saved["progress_application"] == {"applied": False, "applied_epoch": None, "reason": "PROGRESS_RESET"}
    assert user_row(h)["epoch"] != original_epoch
    # The original epoch's course rows are not rewritten by the late result.
    assert course_rows(h, attempt["attemptId"]) == original_rows
    assert calculation(h, token, attempt["attemptId"]).status == 200
    h.refresh(token)
    assert item_view(h, token, CPR, CPR.practice_link_id)["isCompleted"] is False


def test_lost_final_commit_response_does_not_close_activity_twice(store, monkeypatch):
    h = journey(store)
    token = h.login().token
    # The practice item must be complete before the final assessment starts.
    course = dummy_course("mock-compression-only", "adult")
    _, _, practice_job = accepted(h, course, token=token, data=comp_session(60))
    assert h.work(job_id=practice_job) is True
    token, attempt, job_id = accepted(h, course, token=token, role="final", data=comp_session(60))
    assert course_rows(h, attempt["attemptId"]).final["phase"] == "active"
    wrapped = OneTransactionInterruption(h.store.client, lose_response=True)
    monkeypatch.setattr(h.worker.jobs, "finalize", jobs_over(h, wrapped).finalize)
    assert h.work(job_id=job_id) is False
    saved, rows = attempt_row(h, attempt["attemptId"]), course_rows(h, attempt["attemptId"])
    assert saved["state"] == "evaluated" and job_row(h, job_id)["state"] == "done"
    assert (rows.final["phase"], rows.final["active_attempt_id"]) == ("passed", None)
    response = calculation(h, token, attempt["attemptId"])
    assert response.status == 200
    monkeypatch.undo()
    assert h.work(job_id=job_id) is True
    assert calculation(h, token, attempt["attemptId"]).body == response.body
    assert attempt_row(h, attempt["attemptId"]) == saved and course_rows(h, attempt["attemptId"]) == rows
    assert len(h.calculator.calls) == 2  # practice + final, each exactly once


def test_completing_cpr_result_preserves_preexisting_completed_item(store):
    """An item completed earlier keeps its completion evidence; the new result is recorded only (D131)."""
    h = journey(store)
    token, attempt, job_id = accepted(h, CPR, data=CPR_DATA)
    rows = course_rows(h, attempt["attemptId"])
    placement = rows.item["placement_key"]
    # Represent an already existing completion from another accepted definition.
    progress = json.loads(rows.head["progress_json"])
    progress["items"][placement].update(completed=True, passed=True)
    progress.update(completed_placements=[placement], course_status="IN_PROGRESS")
    head = {**rows.head, "progress_json": json.dumps(progress), "completed_placements": [placement],
            "revision": rows.head["revision"] + 1}
    item = {**rows.item, "completed": True, "passed": True, "completed_by_attempt": "previous-committed-attempt",
            "completion_definition_hash": rows.head["definition_hash"], "revision": rows.item["revision"] + 1}
    for row in (head, item):
        h.store.client.put_item(TableName=h.store.table, Item=encode_item(row))
    before = progress_facts(course_rows(h, attempt["attemptId"]))
    assert h.work(job_id=job_id) is True
    stored = attempt_row(h, attempt["attemptId"])
    assert stored["evaluation"]["program_completed"] is True
    assert stored["progress_application"] == {"applied": False, "applied_epoch": None, "reason": "ALREADY_COMPLETED"}
    after = progress_facts(course_rows(h, attempt["attemptId"]))
    assert after["item"] == before["item"] and after["head"]["completed_placements"] == [placement]
    assert json.loads(after["head"]["progress_json"])["items"][placement] == progress["items"][placement]
    assert item_view(h, token, CPR, CPR.practice_link_id)["isCompleted"] is True


def _finalize_legacy_on_completed_slot(h, token, attempt_id, reason, *, program_completed, before, slot, slot_key):
    """Finalize one legacy (no course binding) job whose USER slot is already completed; shared checks."""
    assert h.work(attempt_id) is True
    stored = attempt_row(h, attempt_id)
    assert stored["state"] == "evaluated" and stored["active_counted"] is False
    assert stored["evaluation"]["program_completed"] is program_completed
    assert stored["progress_application"] == {"applied": False, "applied_epoch": None, "reason": reason}
    after = user_row(h)
    # Only the open count closes; the earlier completion is neither undone nor reassigned.
    assert after["slots"][slot_key] == {**slot, "open_attempts": 0}
    assert after["slots"][slot_key]["completed_by_attempt"] == PREVIOUS_ATTEMPT
    assert other_slots(after, slot_key) == other_slots(before, slot_key)
    assert (after["epoch"], after["revision"]) == (before["epoch"], before["revision"] + 1)
    reply = calculation(h, token, attempt_id)
    assert reply.status == 200 and reply.data["progressApplication"] == stored["progress_application"]
    assert h.work(attempt_id) is True  # A redelivered done job rewrites nothing.
    assert attempt_row(h, attempt_id) == stored and user_row(h) == after


def _legacy_on_completed_slot(store, label):
    h, seeded, token = seeded_legacy(store)
    attempt_id = seeded.attempts[label]["attempt_id"]
    attempt = attempt_row(h, attempt_id)
    assert attempt.get("course_binding") is None
    slot_key = f'{attempt["program_id"]}:{attempt["target"]}'
    slot, before = complete_legacy_slot(h, slot_key)
    return h, token, attempt_id, slot_key, slot, before


def test_legacy_finalize_preserves_preexisting_completed_slot(store):
    """Legacy (no course binding) finalize never undoes or reassigns a completed USER slot (D103).

    Real DynamoDB Local counterpart of the removed legacy
    test_pending_result_preserves_preexisting_completed_slot. Uses only the
    D103 compatibility surface: the captured queued compression-only job is
    finalized as stored (no upload). The slot is marked completed by an earlier
    attempt; this fixture does not claim a new CPR completion policy.
    """
    h, token, attempt_id, slot_key, slot, before = _legacy_on_completed_slot(store, "queued")
    assert attempt_row(h, attempt_id)["state"] == "queued"
    _finalize_legacy_on_completed_slot(h, token, attempt_id, "ALREADY_COMPLETED", program_completed=True,
                                       before=before, slot=slot, slot_key=slot_key)


def test_legacy_created_upload_pending_finalize_preserves_completed_slot(store):
    """A pending-policy (cycles goal) legacy finalize keeps an already completed USER slot.

    The captured fixture's only CPR (cycles goal) legacy attempt is ``created``
    and its only queued job is compression-only, whose goal is never
    pending_policy (worker.evaluate / jobs._evaluation reject that). So the
    pending branch of the legacy finalize can only be reached by first
    uploading to the legacy created attempt through /api/v2. That first upload
    is allowed (2026-09-28 user decision recorded under D103, same intent as
    D23: data measured before the switch can still be submitted to its
    attempt); it stays in this separate test so the finalize property above
    does not depend on it.
    """
    h, token, attempt_id, slot_key, slot, before = _legacy_on_completed_slot(store, "created")
    assert attempt_row(h, attempt_id)["state"] == "created"
    h.upload(token, attempt_id, h.attempt(token, attempt_id)["condition"])  # expects 202
    assert attempt_row(h, attempt_id)["state"] == "queued"
    assert user_row(h) == before, "Accepting input must not touch the completed slot or the USER row."
    _finalize_legacy_on_completed_slot(h, token, attempt_id, "GOAL_POLICY_UNRESOLVED", program_completed=False,
                                       before=before, slot=slot, slot_key=slot_key)
    assert attempt_row(h, attempt_id)["evaluation"]["goal"]["status"] == "pending_policy"

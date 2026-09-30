"""Legacy (no course binding) Worker finalization kept by D103, on real DynamoDB Local.

The rows are the captured legacy (v1 API) fixture (tests/fixtures/legacy_mock_v1_rows):
a queued compression-only job whose private input files exist, an evaluated
attempt with its final result and chart, and a USER row with slots. A current
Worker (real bundled calculator) finishes the queued job through the legacy
write-set (slot progress, terminal STORED_INPUT_INVALID on confirmed input
loss) and the /api/v2 routes read the legacy results. Moved from the legacy
paths of integration_tests/test_mock_journey.py. A slot that an earlier
attempt already completed is written directly (complete_legacy_slot); the
terminal failure must keep that completion and close only its own count.
"""

from copy import deepcopy
import json

import pytest

from integration_tests.legacy_slot_support import PREVIOUS_ATTEMPT, complete_legacy_slot, other_slots
from integration_tests.worker_journey_support import (  # noqa: F401 (store fixture)
    LEGACY_SUBMISSION, ScriptedCalculator, attempt_row, calculation, chart_link, job_row, seeded_legacy, store,
    user_row,
)
from mock_journey.contracts import CalculatorRegistry, PENDING_GOAL_ADAPTER_VERSION
from mock_journey.errors import JourneyError
from mock_journey.worker import call_binding
from tests.journey_support import encode_item


SLOT = "mock-compression-only:adult"


def unavailable(*args, **kwargs):
    raise JourneyError("TEMPORARILY_UNAVAILABLE")


def queued(seeded):
    value = seeded.attempts["queued"]
    return value["attempt_id"], value["job_id"]


def assert_slot_applied(h, attempt_id):
    slot = user_row(h)["slots"][SLOT]
    assert slot["completed"] is True and slot["completed_by_attempt"] == attempt_id and slot["open_attempts"] == 0


def test_legacy_saved_candidate_recovers_without_recalculation_and_applies_the_slot(store, monkeypatch):
    h, seeded, token = seeded_legacy(store)
    attempt_id, job_id = queued(seeded)
    assert user_row(h)["slots"][SLOT]["open_attempts"] == 1
    monkeypatch.setattr(h.worker.jobs, "mark_calculation_saved", unavailable)
    assert h.work(job_id=job_id) is False
    processing = attempt_row(h, attempt_id)
    assert processing["state"] == "processing" and processing["evaluation"] is None
    assert calculation(h, token, attempt_id).status == 202
    job = job_row(h, job_id)
    assert job["candidate_ref"] is None
    assert h.worker.storage.load_calculation(job["planned_candidate_ref"], call_binding(job)) is not None
    monkeypatch.undo()
    h.now[0] = job["next_due_at"]
    assert h.work(job_id=job_id) is True
    stored = attempt_row(h, attempt_id)
    assert stored["state"] == "evaluated" and stored.get("error_code") is None
    assert stored["evaluation"]["program_completed"] is True
    assert stored["progress_application"]["reason"] == "APPLIED"
    assert job_row(h, job_id)["call_id"] == job["call_id"] and len(h.calculator.calls) == 1
    assert_slot_applied(h, attempt_id)
    reply = calculation(h, token, attempt_id)
    assert reply.status == 200 and reply.data["submit_arc"] == LEGACY_SUBMISSION


def test_legacy_missing_candidate_rotates_call_and_old_late_write_is_isolated(store):
    # A scripted adapter whose score changes between calls, so a late write of the
    # old call's candidate would be visible (the removed v1 test changed overall to 91).
    h, seeded, token = seeded_legacy(store, ScriptedCalculator(version=PENDING_GOAL_ADAPTER_VERSION))
    attempt_id, job_id = queued(seeded)
    h.calculator.timeout = True
    assert h.work(job_id=job_id) is False
    old = job_row(h, job_id)
    assert old["call_phase"] == "started" and old["candidate_ref"] is None
    assert h.worker.storage.load_calculation(old["planned_candidate_ref"], call_binding(old)) is None
    h.now[0] = max(old["lease_until"] + 1, old.get("next_due_at", 0))
    h.calculator.timeout = False
    h.calculator.overall = 91
    assert h.work(job_id=job_id) is True
    current = job_row(h, job_id)
    assert current["call_id"] != old["call_id"] and current["planned_candidate_ref"] != old["planned_candidate_ref"]
    assert current["execution_fence"] > old["execution_fence"] and current["calculation_restarts"] == 1
    assert len(h.calculator.calls) == 2
    snapshot, user = calculation(h, token, attempt_id), user_row(h)
    assert snapshot.status == 200
    assert snapshot.data["calculation"]["cpr_score"]["total_score"]["overall"] == 91
    # The old call's candidate (score 90) arrives late: it is never adopted.
    h.worker.storage.save_calculation(old["planned_candidate_ref"], h.calculator.responses[old["call_id"]],
                                      call_binding(old))
    assert calculation(h, token, attempt_id).body == snapshot.body and user_row(h) == user
    assert h.work(job_id=job_id) is True
    assert calculation(h, token, attempt_id).body == snapshot.body and user_row(h) == user
    assert len(h.calculator.calls) == 2
    assert_slot_applied(h, attempt_id)


@pytest.mark.parametrize("artifact", ["manifest", "raw", "meta"])
def test_legacy_confirmed_input_loss_terminates_once_without_calculator_execution(store, artifact):
    h, seeded, token = seeded_legacy(store)
    attempt_id, job_id = queued(seeded)
    job = job_row(h, job_id)
    ref = job["input_manifest_ref"]
    manifest = json.loads(h.objects.objects[(ref["bucket"], ref["key"])]["Body"])
    key = ref["key"] if artifact == "manifest" else manifest["raw_base"] + {"raw": ".bin", "meta": ".meta.json"}[artifact]
    del h.objects.objects[(ref["bucket"], key)]
    assert h.work(job_id=job_id) is True
    failed = attempt_row(h, attempt_id)
    assert failed["state"] == "failed" and failed["error_code"] == "STORED_INPUT_INVALID"
    assert failed["evaluation"] is None and failed["active_counted"] is False
    user = user_row(h)
    assert user["slots"][SLOT] == {**seeded.rows[("USER#dummy-tester", "STATE")]["slots"][SLOT], "open_attempts": 0}
    closed = job_row(h, job_id)
    assert closed["state"] == "failed" and "GSI1PK" not in closed
    assert h.work(job_id=job_id) is True
    assert attempt_row(h, attempt_id) == failed and user_row(h) == user and job_row(h, job_id) == closed
    reply = calculation(h, token, attempt_id)
    assert reply.status == 503 and reply.error["code"] == "STORED_INPUT_INVALID"
    assert not h.calculator.calls


def test_legacy_missing_adapter_then_tampered_raw_never_invoke_a_fallback(store):
    h, seeded, token = seeded_legacy(store)
    attempt_id, job_id = queued(seeded)
    registry = h.worker.adapters
    h.worker.adapters = CalculatorRegistry([])
    assert h.work(job_id=job_id) is False
    assert job_row(h, job_id)["call_phase"] == "not_started"
    assert attempt_row(h, attempt_id)["state"] == "processing"
    h.worker.adapters = registry
    h.advance(61)
    ref = job_row(h, job_id)["input_manifest_ref"]
    manifest = json.loads(h.objects.objects[(ref["bucket"], ref["key"])]["Body"])
    h.objects.objects[(ref["bucket"], manifest["raw_base"] + ".bin")]["Body"] = b"corrupted"
    assert h.work(job_id=job_id) is True
    failed = attempt_row(h, attempt_id)
    assert failed["state"] == "failed" and failed["error_code"] == "STORED_INPUT_INVALID"
    assert failed["evaluation"] is None and not h.calculator.calls


def test_legacy_committed_candidate_loss_is_an_integrity_failure_not_endless_unknown(store, monkeypatch):
    h, seeded, token = seeded_legacy(store)
    attempt_id, job_id = queued(seeded)
    monkeypatch.setattr(h.worker.storage, "save_final", unavailable)
    assert h.work(job_id=job_id) is False
    job = job_row(h, job_id)
    assert job["call_phase"] == "candidate_saved" and job["candidate_ref"] is not None
    del h.objects.objects[(job["candidate_ref"]["bucket"], job["candidate_ref"]["key"])]
    monkeypatch.undo()
    h.advance(61)
    assert h.work(job_id=job_id) is True
    failed = attempt_row(h, attempt_id)
    assert failed["state"] == "failed" and failed["error_code"] == "STORED_INPUT_INVALID"
    assert user_row(h)["slots"][SLOT]["completed"] is False
    assert len(h.calculator.calls) == 1


def test_legacy_chart_snapshot_loss_cannot_refetch_or_publish_new_data(store, monkeypatch):
    h, seeded, token = seeded_legacy(store)
    attempt_id, job_id = queued(seeded)
    monkeypatch.setattr(h.worker.storage, "publish_selected_chart", unavailable)
    assert h.work(job_id=job_id) is False
    job = job_row(h, job_id)
    assert job["chart_snapshot"]["kind"] == "snapshot"
    ref = job["chart_snapshot"]["snapshot_ref"]
    del h.objects.objects[(ref["bucket"], ref["key"])]
    monkeypatch.undo()
    h.advance(61)
    assert h.work(job_id=job_id) is True
    failed = attempt_row(h, attempt_id)
    assert failed["state"] == "failed" and failed["error_code"] == "STORED_INPUT_INVALID"
    assert failed["evaluation"] is None and failed["active_counted"] is False
    assert len(h.calculator.calls) == 1 and h.calculator.chart_calls == 1


@pytest.mark.parametrize("artifact", ["final", "chart"])
def test_legacy_confirmed_result_loss_returns_fixed_error_without_rewriting_completion(store, artifact):
    h, seeded, token = seeded_legacy(store)
    evaluated = seeded.attempts["evaluated"]
    attempt_id = evaluated["attempt_id"]
    before, user = attempt_row(h, attempt_id), user_row(h)
    assert before == seeded.rows[(f"ATTEMPT#{attempt_id}", "META")]
    job = job_row(h, evaluated["job_id"])
    ref = job["final_ref"] if artifact == "final" else job["chart_publication"]
    del h.objects.objects[(ref["bucket"] if "bucket" in ref else h.worker.storage.bucket, ref["key"])]
    reply = (calculation if artifact == "final" else chart_link)(h, token, attempt_id)
    assert reply.status == 503 and reply.error["code"] == "STORED_INPUT_INVALID"
    assert ref["key"] not in json.dumps(reply.body)
    assert h.work(job_id=evaluated["job_id"]) is True
    assert attempt_row(h, attempt_id) == before and user_row(h) == user
    assert not h.calculator.calls


def test_legacy_result_after_legacy_session_logout_is_stored_without_slot_progress(store):
    h, seeded, token = seeded_legacy(store)
    attempt_id, job_id = queued(seeded)
    h.logout(token)
    reset = user_row(h)
    assert reset["epoch"] != seeded.rows[("USER#dummy-tester", "STATE")]["epoch"]
    assert all(slot["open_attempts"] == 0 and slot["completed"] is False for slot in reset["slots"].values())
    assert h.work(job_id=job_id) is True
    stored = attempt_row(h, attempt_id)
    assert stored["state"] == "evaluated" and stored["evaluation"]["program_completed"] is True
    assert stored["progress_application"] == {"applied": False, "applied_epoch": None, "reason": "PROGRESS_RESET"}
    assert user_row(h) == reset


# A concurrent legacy attempt already completed the queued attempt's slot (T3 gap,
# formerly test_mock_journey::test_input_loss_in_concurrent_attempt_preserves_existing_slot_completion,
# legacy path). Only the queued attempt's open count changes; the completion keeps its owner.

CONCURRENT_OWNER = "legacy-concurrent-attempt"


def complete_slot_concurrently(h):
    """Raw conditional USER put: SLOT completed by another legacy attempt, one revision later."""
    user = user_row(h)
    assert user["slots"][SLOT] == {"completed": False, "completed_at": None, "completed_by_attempt": None,
                                   "open_attempts": 1}
    completed = deepcopy(user)
    completed["slots"][SLOT].update(completed=True, completed_by_attempt=CONCURRENT_OWNER,
                                    completed_at=int(user["updated_at"]))
    completed["revision"] = user["revision"] + 1
    h.store.client.put_item(TableName=h.store.table, Item=encode_item(completed),
                            ConditionExpression="#r = :revision AND #e = :epoch",
                            ExpressionAttributeNames={"#r": "revision", "#e": "epoch"},
                            ExpressionAttributeValues=encode_item({":revision": user["revision"],
                                                                   ":epoch": user["epoch"]}))
    stored = user_row(h)
    assert stored == completed
    return stored


def assert_only_the_open_count_closed(h, completed):
    user = user_row(h)
    assert user["slots"][SLOT] == {**completed["slots"][SLOT], "open_attempts": 0}
    assert user["slots"][SLOT]["completed_by_attempt"] == CONCURRENT_OWNER
    assert {key: slot for key, slot in user["slots"].items() if key != SLOT} == {
        key: slot for key, slot in completed["slots"].items() if key != SLOT}
    assert user["epoch"] == completed["epoch"] and user["revision"] == completed["revision"] + 1
    return user


def test_legacy_input_loss_keeps_a_slot_completed_by_a_concurrent_attempt(store):
    h, seeded, token = seeded_legacy(store)
    attempt_id, job_id = queued(seeded)
    completed = complete_slot_concurrently(h)
    ref = job_row(h, job_id)["input_manifest_ref"]
    manifest = json.loads(h.objects.objects[(ref["bucket"], ref["key"])]["Body"])
    del h.objects.objects[(ref["bucket"], manifest["raw_base"] + ".bin")]
    assert h.work(job_id=job_id) is True
    failed = attempt_row(h, attempt_id)
    assert failed["state"] == "failed" and failed["error_code"] == "STORED_INPUT_INVALID"
    assert failed["active_counted"] is False and failed["evaluation"] is None
    user = assert_only_the_open_count_closed(h, completed)
    assert h.work(job_id=job_id) is True and user_row(h) == user
    assert not h.calculator.calls


def test_legacy_success_on_a_slot_completed_by_a_concurrent_attempt_is_already_completed(store):
    h, seeded, token = seeded_legacy(store)
    attempt_id, job_id = queued(seeded)
    completed = complete_slot_concurrently(h)
    assert h.work(job_id=job_id) is True
    stored = attempt_row(h, attempt_id)
    assert stored["state"] == "evaluated" and stored["evaluation"]["program_completed"] is True
    assert stored["active_counted"] is False
    assert stored["progress_application"]["applied"] is False
    assert stored["progress_application"]["reason"] == "ALREADY_COMPLETED"
    user = assert_only_the_open_count_closed(h, completed)
    reply = calculation(h, token, attempt_id)
    assert reply.status == 200 and reply.data["submit_arc"] == LEGACY_SUBMISSION
    assert h.work(job_id=job_id) is True and user_row(h) == user
    assert len(h.calculator.calls) == 1


def test_legacy_terminal_failure_keeps_an_existing_slot_completion_and_closes_only_the_count(store):
    # Legacy counterpart of the removed integration_tests/test_mock_journey.py::
    # test_input_loss_in_concurrent_attempt_preserves_existing_slot_completion (mark_failed/_close_count).
    h, seeded, token = seeded_legacy(store)
    attempt_id, job_id = queued(seeded)
    slot, before = complete_legacy_slot(h, SLOT)
    ref = job_row(h, job_id)["input_manifest_ref"]
    del h.objects.objects[(ref["bucket"], ref["key"])]
    assert h.work(job_id=job_id) is True
    failed = attempt_row(h, attempt_id)
    assert failed["state"] == "failed" and failed["error_code"] == "STORED_INPUT_INVALID"
    assert failed["evaluation"] is None and failed["progress_application"] is None
    assert failed["active_counted"] is False
    after = user_row(h)
    # Only the terminal attempt's count closes; the earlier completion is neither undone nor reassigned.
    assert after["slots"][SLOT] == {**slot, "open_attempts": 0}
    assert after["slots"][SLOT]["completed_by_attempt"] == PREVIOUS_ATTEMPT
    assert other_slots(after, SLOT) == other_slots(before, SLOT)
    assert (after["epoch"], after["revision"]) == (before["epoch"], before["revision"] + 1)
    closed = job_row(h, job_id)
    assert closed["state"] == "failed" and "GSI1PK" not in closed
    assert h.work(job_id=job_id) is True  # A redelivered terminal job changes nothing.
    assert attempt_row(h, attempt_id) == failed and user_row(h) == after and job_row(h, job_id) == closed
    reply = calculation(h, token, attempt_id)
    assert reply.status == 503 and reply.error["code"] == "STORED_INPUT_INVALID"
    assert not h.calculator.calls

"""L1 real DynamoDB transactions with the bundled calculator and test object IO.

The fixture deliberately reuses in-memory object IO; this is not evidence for
the later filesystem, default CLI, or actual app/manikin acceptance phases.
"""

from copy import deepcopy
import json

import pytest

from integration_tests.test_mock_journey import (
    LocalDefinitions, api, create, journey as legacy_journey, login, stored_attempt, submit,
)
from integration_tests.test_mock_state_dynamodb import OneTransactionInterruption, _encode
from mock_journey.catalog import Catalog
from mock_journey.contracts import CalculatorRegistry
from mock_journey.errors import JourneyError
from mock_journey.internal_calculator import (
    InternalCalculator, PENDING_GOAL_ADAPTER_VERSION, PENDING_GOAL_PROFILE_VERSION,
)
from mock_journey.jobs import DynamoJobRepository
from mock_journey.state import DynamoStateRepository
from mock_journey.worker import JourneyWorker, call_binding
from tests._synth import WEAK_RAMP, comp_session, cpr_session


class PendingDefinitions(LocalDefinitions):
    def get_definition(self, program_id, target):
        definition = super().get_definition(program_id, target)
        definition.update(adapter_version=PENDING_GOAL_ADAPTER_VERSION,
                          profile_version=PENDING_GOAL_PROFILE_VERSION)
        return definition


class TrackedCalculator(InternalCalculator):
    """Fault hooks surround actual calculation; no score is replaced."""

    def __init__(self):
        super().__init__(version=PENDING_GOAL_ADAPTER_VERSION, projection_version="test-projection",
                         stage="development", allow_pending_cycle_goal=True)
        self.calls = []
        self.after_calculate = lambda: None

    def calculate(self, loaded, binding, heartbeat):
        raw = super().calculate(loaded, binding, heartbeat)
        self.calls.append((deepcopy(binding), raw))
        self.after_calculate()
        return raw


@pytest.fixture
def pending_journey(legacy_journey):
    world = legacy_journey
    world.service.catalog = Catalog(PendingDefinitions())
    world.adapter = TrackedCalculator()
    world.worker = JourneyWorker(world.jobs, world.storage, CalculatorRegistry([world.adapter]),
                                 lease_seconds=60, retry_seconds=5, clock=lambda: world.now[0])
    return world


def progress(world, token):
    return world.state.get_progress(world.auth.authenticate(token))


def result(world, token, attempt):
    return api(world, "GET", "attempts/" + attempt["attempt_id"] + "/calculation", token=token)


def accept(world, token, *, data=None, program="mock-cpr", request_id="first"):
    attempt = create(world, token, program=program, request_id=request_id)
    assert submit(world, token, attempt, data=data if data is not None else cpr_session([(30, 2)] * 3))["statusCode"] == 202
    return attempt, stored_attempt(world, token, attempt)["job_id"]


@pytest.mark.parametrize("program,data,pending,met,decision,complete", [
    ("mock-cpr", cpr_session([(30, 2)] * 3), True, None, "pass", False),
    ("mock-cpr", cpr_session([(30, 2)]), True, None, "fail", False),
    ("mock-compression-only", comp_session(60), False, True, "pass", True),
    ("mock-compression-only", comp_session(59), False, False, "pass", False),
    ("mock-compression-only", comp_session(60, WEAK_RAMP), False, True, "fail", False),
], ids=("cpr-pass-pending", "cpr-null-pending", "only-pass-complete", "only-short-incomplete", "only-fail-incomplete"))
def test_real_scores_and_completion_commit_once(pending_journey, program, data, pending, met, decision, complete):
    world = pending_journey
    token, other = login(world), login(world)
    attempt, job_id = accept(world, token, data=data, program=program)
    slot = program + ":adult"
    assert progress(world, other)["slots"][slot]["open_attempts"] == 1
    assert world.worker.process(job_id)
    stored = stored_attempt(world, token, attempt)
    assessment = stored["evaluation"]
    assert stored["state"] == "evaluated" and stored["active_counted"] is False
    assert assessment["goal"]["status"] == ("pending_policy" if pending else "evaluated")
    assert assessment["goal"]["met"] is met
    assert assessment["score"]["decision"] == decision
    assert assessment["program_completed"] is complete
    if pending:
        assert assessment["goal"]["observed"] is None
        assert assessment["reason_codes"] == ["GOAL_POLICY_UNRESOLVED"] + ([] if decision == "pass" else ["SCORE_NOT_PASS"])
        assert stored["progress_application"]["reason"] == "GOAL_POLICY_UNRESOLVED"
    shared = progress(world, other)
    assert shared["slots"][slot]["open_attempts"] == 0
    assert shared["slots"][slot]["completed"] is complete
    job = world.jobs.get_job(job_id)
    assert job["state"] == "done" and "GSI1PK" not in job and "GSI1SK" not in job
    response = result(world, token, attempt)
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["submit_arc"] == {"status": "disabled", "ok": False, "error": "arc_contract_pending"}
    assert body["chart_dataset_url"]
    assert "evaluation" not in body
    if pending and decision == "fail":
        assert body["cpr_score"]["total_score"]["overall"] is None
    for _ in range(2):
        assert world.worker.process(job_id)
        assert result(world, token, attempt) == response
        assert submit(world, token, attempt, data=data) == response
    assert len(world.adapter.calls) == 1
    assert stored_attempt(world, token, attempt) == stored
    assert progress(world, other) == shared
    next_attempt = api(world, "POST", "attempts", {"client_request_id": "next", "catalog_version": "mock-catalog-v1",
                                                   "program_id": program, "target": "adult"}, token)
    assert next_attempt["statusCode"] == (409 if complete else 201)


def test_pending_candidate_recovers_without_calculation_after_reference_commit_failure(pending_journey, monkeypatch):
    world = pending_journey
    token = login(world)
    attempt, job_id = accept(world, token)
    mark = world.jobs.mark_calculation_saved

    def unavailable(*args, **kwargs):
        raise JourneyError("TEMPORARILY_UNAVAILABLE")

    monkeypatch.setattr(world.jobs, "mark_calculation_saved", unavailable)
    assert not world.worker.process(job_id)
    old = world.jobs.get_job(job_id)
    candidate = world.storage.load_calculation(old["planned_candidate_ref"], call_binding(old))
    assert candidate is not None and old["candidate_ref"] is None
    assert json.loads(candidate)["goal"]["status"] == "pending_policy"
    monkeypatch.setattr(world.jobs, "mark_calculation_saved", mark)
    monkeypatch.setattr(world.adapter, "calculate", lambda *a, **k: pytest.fail("Saved pending candidate was recalculated."))
    world.now[0] = old["next_due_at"]
    assert world.worker.process(job_id)
    assert world.jobs.get_job(job_id)["call_id"] == old["call_id"]
    assert stored_attempt(world, token, attempt)["progress_application"]["reason"] == "GOAL_POLICY_UNRESOLVED"
    assert progress(world, token)["slots"]["mock-cpr:adult"]["open_attempts"] == 0
    assert len(world.adapter.calls) == 1


def test_unstored_pending_candidate_rotates_call_and_old_late_write_is_isolated(pending_journey):
    world = pending_journey
    token = login(world)
    attempt, job_id = accept(world, token)

    def interrupted():
        raise RuntimeError("Test interruption before candidate persistence.")

    world.adapter.after_calculate = interrupted
    assert not world.worker.process(job_id)
    old = world.jobs.get_job(job_id)
    assert world.storage.load_calculation(old["planned_candidate_ref"], call_binding(old)) is None
    world.adapter.after_calculate = lambda: None
    world.now[0] = old["next_due_at"]
    assert world.worker.process(job_id)
    current = world.jobs.get_job(job_id)
    assert current["call_id"] != old["call_id"] and current["planned_candidate_ref"] != old["planned_candidate_ref"]
    assert current["execution_fence"] > old["execution_fence"]
    snapshot = result(world, token, attempt)
    saved_progress = progress(world, token)
    world.storage.save_calculation(old["planned_candidate_ref"], world.adapter.calls[0][1], call_binding(old))
    assert world.worker.process(job_id)
    assert result(world, token, attempt) == snapshot and progress(world, token) == saved_progress
    assert len(world.adapter.calls) == 2


def test_logout_at_final_transaction_keeps_pending_result_without_applying_to_new_epoch(pending_journey, monkeypatch):
    world = pending_journey
    token, other = login(world), login(world)
    attempt, job_id = accept(world, token)
    original_epoch = progress(world, token)["epoch"]
    wrapped = OneTransactionInterruption(world.state.client, before=lambda: world.state.logout(world.auth.authenticate(other)))
    jobs = DynamoJobRepository(DynamoStateRepository(wrapped, world.state.table_name, clock=lambda: world.now[0]))
    monkeypatch.setattr(world.jobs, "finalize", jobs.finalize)
    assert world.worker.process(job_id)
    saved = stored_attempt(world, token, attempt)
    assert saved["state"] == "evaluated" and saved["evaluation"]["goal"]["status"] == "pending_policy"
    assert saved["progress_application"] == {"applied": False, "applied_epoch": None, "reason": "PROGRESS_RESET"}
    shared = progress(world, token)
    assert shared["epoch"] != original_epoch
    assert all(slot["open_attempts"] == 0 and slot["completed"] is False for slot in shared["slots"].values())
    assert result(world, token, attempt)["statusCode"] == 200


def test_lost_final_commit_response_does_not_close_activity_twice(pending_journey, monkeypatch):
    world = pending_journey
    token = login(world)
    attempt, job_id = accept(world, token)
    wrapped = OneTransactionInterruption(world.state.client, lose_response=True)
    jobs = DynamoJobRepository(DynamoStateRepository(wrapped, world.state.table_name, clock=lambda: world.now[0]))
    monkeypatch.setattr(world.jobs, "finalize", jobs.finalize)
    assert not world.worker.process(job_id)
    saved = stored_attempt(world, token, attempt)
    shared = progress(world, token)
    assert saved["state"] == "evaluated" and world.jobs.get_job(job_id)["state"] == "done"
    assert shared["slots"]["mock-cpr:adult"]["open_attempts"] == 0
    response = result(world, token, attempt)
    assert response["statusCode"] == 200
    assert world.worker.process(job_id)
    assert result(world, token, attempt) == response
    assert stored_attempt(world, token, attempt) == saved and progress(world, token) == shared
    assert len(world.adapter.calls) == 1


def test_pending_result_preserves_preexisting_completed_slot(pending_journey):
    world = pending_journey
    token = login(world)
    attempt, job_id = accept(world, token)
    user = progress(world, token)
    # Represent an already existing completion from another accepted definition;
    # this fixture does not claim a new CPR completion policy.
    slot = user["slots"]["mock-cpr:adult"]
    slot.update(completed=True, completed_by_attempt="previous-committed-attempt", completed_at=999)
    user["revision"] += 1
    world.state.client.put_item(TableName=world.state.table_name, Item=_encode(user))
    assert world.worker.process(job_id)
    saved = progress(world, token)["slots"]["mock-cpr:adult"]
    assert saved == {**slot, "open_attempts": 0}
    assert stored_attempt(world, token, attempt)["evaluation"]["program_completed"] is False
    assert stored_attempt(world, token, attempt)["progress_application"]["reason"] == "GOAL_POLICY_UNRESOLVED"

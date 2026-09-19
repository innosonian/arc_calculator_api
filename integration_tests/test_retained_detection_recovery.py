"""Real local DB recovery of historical v2 candidates; object storage is a double."""

import base64
from copy import deepcopy
import json
from pathlib import Path
import uuid

import pytest

from integration_tests.test_mock_journey import journey, login, create  # noqa: F401
from mock_journey import typed
from mock_journey.calculation import CalculationService
from mock_journey.catalog import Catalog
from mock_journey.contracts import CalculatorRegistry, RETAINED_PENDING_GOAL_ADAPTER_VERSION
from mock_journey.execution_definitions import execution_catalog
from mock_journey.internal_calculator import InternalCalculator
from mock_journey.projection import ProjectedInput, ProjectionSchema, typed_identity
from mock_journey.worker import JourneyWorker, call_binding


CASES = json.loads((Path(__file__).parents[1] / "tests/fixtures/retained_v2_candidates.json").read_text())["cases"]


@pytest.mark.parametrize("row", CASES, ids=lambda row: row["id"])
@pytest.mark.parametrize("phase", ["queued", "started", "candidate_saved", "done"])
def test_old_job_recovery_preserves_meaning_without_new_core(journey, row, phase, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Historical job invoked the current calculator.")
    monkeypatch.setattr("main.run_calculator", forbidden)
    old = RETAINED_PENDING_GOAL_ADAPTER_VERSION
    projection = row["binding"]["projection_version"]
    current = execution_catalog()

    class OldDefinitions:
        def get_definition(self, program, target):
            value = current.get_definition(program, target)
            value.update(adapter_version=old, projection_version=projection)
            return value

    journey.service.catalog = Catalog(OldDefinitions())
    journey.service.calculation = CalculationService(journey.state, journey.jobs, journey.storage,
                                                    {projection: ProjectionSchema(projection, {})},
                                                    payload_limit=2000000, clock=lambda: journey.now[0])
    token = login(journey)
    attempt = create(journey, token, program=row["id"])
    auth = journey.auth.authenticate(token)
    actual = journey.state.get_attempt(auth, attempt["attempt_id"])
    projected = ProjectedInput(base64.b64decode(row["cpr_base64"]), b"", deepcopy(row["payload"]))
    # Keep the historical input payload; only this test's owner/job envelope is
    # rebound. No score, chart, count or candidate policy is regenerated.
    assert typed.canonical_bytes(json.loads(actual["definition_json"])) == typed.canonical_bytes(projected.payload["definition"])
    binding = {"attempt_id": actual["attempt_id"], "epoch": actual["epoch"], "input_digest": typed_identity(projected),
               "adapter_version": old, "projection_version": projection}
    saved = journey.storage.save_input(projected, binding)
    accepted = journey.jobs.accept_input(auth, actual["attempt_id"], binding["input_digest"], saved["manifest_ref"],
                                         job_id=str(uuid.uuid4()), adapter_version=old, next_due_at=journey.now[0])
    job_id = accepted["job_id"]
    candidate_ref = candidate_raw = None
    if phase != "queued":
        status, job = journey.jobs.claim(job_id, "old-owner", 60)
        assert status == "execute"
        proposed = {**binding, "job_id": job_id, "call_id": str(uuid.uuid4())}
        planned = journey.storage.planned_calculation(proposed)
        permitted, job = journey.jobs.begin_calculation(job_id, "old-owner", job["fence"], proposed["call_id"], planned)
        assert permitted
        if phase in ("candidate_saved", "done"):
            candidate = typed.parse_json(base64.b64decode(row["candidate_base64"]))
            candidate["binding"] = call_binding(job)
            candidate_raw = typed.json_bytes(candidate)
            candidate_ref = journey.storage.save_calculation(planned, candidate_raw, call_binding(job))
            journey.jobs.mark_calculation_saved(job_id, "old-owner", job["fence"], candidate_ref)
        journey.now[0] += 61
    adapter = InternalCalculator(version=old, projection_version=projection, stage="development", allow_pending_cycle_goal=True)
    worker = JourneyWorker(journey.jobs, journey.storage, CalculatorRegistry([adapter]),
                           lease_seconds=60, retry_seconds=5, clock=lambda: journey.now[0])
    if phase == "done":
        assert worker.process(job_id) is True
    before = journey.jobs.get_job(job_id)
    before_objects = deepcopy(journey.objects.objects)
    original_input = deepcopy(before["input_manifest_ref"])
    original_call = before.get("call_id")
    if phase in ("queued", "started"):
        assert worker.process(job_id) is False
        after = journey.jobs.get_job(job_id)
        assert after["call_id"] == original_call
        assert after.get("final_ref") is None and after.get("candidate_ref") is None
        assert journey.state.get_attempt(auth, actual["attempt_id"])["evaluation"] is None
    else:
        assert worker.process(job_id) is True
        after = journey.jobs.get_job(job_id)
        assert journey.storage.load_calculation(planned, call_binding(after)) == candidate_raw
        if phase == "done":
            assert after == before
            assert journey.objects.objects == before_objects
        result = journey.service.calculation.result(auth, actual["attempt_id"])
        assert result[0] == 200
        response = typed.parse_json(result[1]) if type(result[1]) is bytes else result[1]
        assert response["action_count"] == row["old_counts"]
        snapshot = deepcopy(journey.objects.objects)
        terminal = deepcopy(after)
        assert worker.process(job_id) is True
        assert journey.jobs.get_job(job_id) == terminal
        assert journey.objects.objects == snapshot
    after = journey.jobs.get_job(job_id)
    assert after["adapter_version"] == old
    assert after["projection_version"] == projection
    assert after["input_manifest_ref"] == original_input
    assert after["input_digest"] == binding["input_digest"]

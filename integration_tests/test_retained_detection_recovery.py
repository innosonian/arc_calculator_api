"""Real local DB recovery of historical v2 candidates; object storage is a double.

Historical ``arc-local-calculator-pending-v2`` jobs are legacy (no course
binding) jobs. The owner envelope is the captured legacy fixture
(tests/fixtures/legacy_mock_v1_rows): its active session, USER row with slots
and a created-attempt template. Each case adds one created legacy attempt for
its program exactly as the legacy create write-set did (ATTEMPT row with a
fresh resume digest, USER slot open count + 1), then accepts the historical
input through the job repository. No v1 API route or v1-only method is used.
"""

import base64
from copy import deepcopy
import json
from pathlib import Path
import uuid

import pytest

from integration_tests.worker_journey_support import (  # noqa: F401 (store fixture)
    attempt_row, calculation, job_row, seeded_legacy, store, user_row,
)
from mock_journey import typed
from mock_journey.catalog import Catalog
from mock_journey.contracts import PENDING_GOAL_PROFILE_VERSION, RETAINED_PENDING_GOAL_ADAPTER_VERSION
from mock_journey.execution_definitions import execution_catalog
from mock_journey.internal_calculator import InternalCalculator
from mock_journey.projection import ProjectedInput, typed_identity
from mock_journey.worker import call_binding
from tests.journey_support import encode_item


CASES = json.loads((Path(__file__).parents[1] / "tests/fixtures/retained_v2_candidates.json").read_text())["cases"]
TARGET = "adult"


def legacy_created_attempt(h, seeded, program, definition_json):
    """One created legacy attempt as the legacy create transaction wrote it (ATTEMPT + USER slot count).

    Synthesized from the fixture's captured created attempt, not produced by a real
    legacy create call. The SESSION CREATE idempotency row and the create-time
    ``slots.<slot>.completed = false`` condition are left out on purpose: these tests
    check historical candidate recovery, not the (removed) create transaction.
    """
    template = seeded.rows[(f"ATTEMPT#{seeded.attempts['created']['attempt_id']}", "META")]
    attempt_id = str(uuid.uuid4())
    row = {key: value for key, value in template.items()
           if key not in ("resume_nonce", "resume_key_version", "resume_digest")}
    row.update(PK=f"ATTEMPT#{attempt_id}", attempt_id=attempt_id, program_id=program, target=TARGET,
               definition_json=definition_json, created_at=h.clock(), state="created", revision=0,
               active_counted=True, evaluation=None, progress_application=None)
    row = h.api.auth.prepare_resume(row)
    user = user_row(h)
    changed = deepcopy(user)
    changed["slots"][f"{program}:{TARGET}"]["open_attempts"] += 1
    changed.update(revision=user["revision"] + 1, updated_at=h.clock())
    h.store.client.transact_write_items(TransactItems=[
        {"Put": {"TableName": h.store.table, "Item": encode_item(row),
                 "ConditionExpression": "attribute_not_exists(PK)"}},
        {"Put": {"TableName": h.store.table, "Item": encode_item(changed),
                 "ConditionExpression": "#revision = :revision",
                 "ExpressionAttributeNames": {"#revision": "revision"},
                 "ExpressionAttributeValues": {":revision": {"N": str(user["revision"])}}}},
    ])
    return attempt_row(h, attempt_id)


def old_worker(h, adapter):
    from mock_journey.assembly import build_worker
    from mock_journey.settings import WorkerSettings
    return build_worker(WorkerSettings(h.api_settings.state, h.api_settings.storage, 60, 5),
                        dynamodb_client=h.store.client, s3_client=h.objects, legacy_bindings=h.bindings,
                        adapters=[adapter], required_bindings=[(adapter.version, adapter.projection_version)],
                        clock=h.clock)


@pytest.mark.parametrize("row", CASES, ids=lambda row: row["id"])
@pytest.mark.parametrize("phase", ["queued", "started", "candidate_saved", "done"])
def test_old_job_recovery_preserves_meaning_without_new_core(store, row, phase, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Historical job invoked the current calculator.")
    monkeypatch.setattr("main.run_calculator", forbidden)
    old = RETAINED_PENDING_GOAL_ADAPTER_VERSION
    projection = row["binding"]["projection_version"]
    current = execution_catalog()

    class OldDefinitions:
        def get_definition(self, program, target):
            value = current.get_definition(program, target)
            # The retained v2 definitions carry the pending profile (D136 keeps them as stored).
            value.update(adapter_version=old, profile_version=PENDING_GOAL_PROFILE_VERSION,
                         projection_version=projection)
            return value

    h, seeded, token = seeded_legacy(store)
    # The legacy create stored exactly this catalog definition text.
    definition_json = Catalog(OldDefinitions()).definition(row["id"], TARGET)
    projected = ProjectedInput(base64.b64decode(row["cpr_base64"]), b"", deepcopy(row["payload"]))
    # Keep the historical input payload; only this test's owner/job envelope is
    # rebound. No score, chart, count or candidate policy is regenerated.
    assert typed.canonical_bytes(json.loads(definition_json)) == typed.canonical_bytes(projected.payload["definition"])
    actual = legacy_created_attempt(h, seeded, row["id"], definition_json)
    assert actual.get("course_binding") is None and actual["state"] == "created"
    auth = h.api.auth.authenticate(token)
    adapter = InternalCalculator(version=old, projection_version=projection, stage="development",
                                 allow_pending_cycle_goal=True)
    worker = old_worker(h, adapter)
    jobs, storage = worker.jobs, worker.storage
    binding = {"attempt_id": actual["attempt_id"], "epoch": actual["epoch"], "input_digest": typed_identity(projected),
               "adapter_version": old, "projection_version": projection}
    saved = storage.save_input(projected, binding)
    accepted = jobs.accept_input(auth, actual["attempt_id"], binding["input_digest"], saved["manifest_ref"],
                                 job_id=str(uuid.uuid4()), adapter_version=old, next_due_at=h.clock())
    job_id = accepted["job_id"]
    candidate_raw = None
    if phase != "queued":
        status, job = jobs.claim(job_id, "old-owner", 60)
        assert status == "execute"
        proposed = {**binding, "job_id": job_id, "call_id": str(uuid.uuid4())}
        planned = storage.planned_calculation(proposed)
        permitted, job = jobs.begin_calculation(job_id, "old-owner", job["fence"], proposed["call_id"], planned)
        assert permitted
        if phase in ("candidate_saved", "done"):
            candidate = typed.parse_json(base64.b64decode(row["candidate_base64"]))
            candidate["binding"] = call_binding(job)
            candidate_raw = typed.json_bytes(candidate)
            candidate_ref = storage.save_calculation(planned, candidate_raw, call_binding(job))
            jobs.mark_calculation_saved(job_id, "old-owner", job["fence"], candidate_ref)
        h.advance(61)
    if phase == "done":
        assert worker.process(job_id) is True
    before = job_row(h, job_id)
    before_objects = deepcopy(h.objects.objects)
    original_input = deepcopy(before["input_manifest_ref"])
    original_call = before.get("call_id")
    if phase in ("queued", "started"):
        assert worker.process(job_id) is False
        after = job_row(h, job_id)
        assert after["call_id"] == original_call
        assert after.get("final_ref") is None and after.get("candidate_ref") is None
        assert attempt_row(h, actual["attempt_id"])["evaluation"] is None
    else:
        assert worker.process(job_id) is True
        after = job_row(h, job_id)
        assert storage.load_calculation(planned, call_binding(after)) == candidate_raw
        if phase == "done":
            assert after == before
            assert h.objects.objects == before_objects
        result = calculation(h, token, actual["attempt_id"])
        assert result.status == 200
        assert result.data["calculation"]["action_count"] == row["old_counts"]
        snapshot = deepcopy(h.objects.objects)
        terminal = deepcopy(after)
        assert worker.process(job_id) is True
        assert job_row(h, job_id) == terminal
        assert h.objects.objects == snapshot
    after = job_row(h, job_id)
    assert after["adapter_version"] == old
    assert after["projection_version"] == projection
    assert after["input_manifest_ref"] == original_input
    assert after["input_digest"] == binding["input_digest"]

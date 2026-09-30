"""Finalize/seal against DynamoDB Local. No ARC outbound queue is created.

Attempts without course_binding are the captured /mock/v1 rows
(tests/fixtures/legacy_mock_v1_rows, seeded by tests/legacy_rows_support.py);
the removed create route is not used. D103 keeps finishing their calculation.
"""

import uuid

import pytest

from mock_journey.errors import JourneyError
from mock_journey.jobs import DynamoJobRepository
from mock_journey.models import AuthContext
from mock_journey.state import DynamoStateRepository
from tests.journey_support import JourneyStore, V2Journey, dynamodb_local_store
from tests.legacy_rows_support import seed_legacy_rows


def _keys(store):
    return {(row["PK"], row["SK"]) for row in store.rows()}


@pytest.mark.parametrize("path", ["stored_queued_job", "v2_upload"])
def test_finalize_without_course_binding_writes_no_submission(dynamodb_client, path):
    with dynamodb_local_store(dynamodb_client) as store:
        h = V2Journey(store)
        seeded = seed_legacy_rows(store, objects=h.objects)
        h.advance(seeded.meta["clock_at_end"] - h.clock())
        if path == "stored_queued_job":
            label = "queued"  # accepted by /mock/v1, never relayed or processed
            attempt_id = seeded.attempts[label]["attempt_id"]
        else:
            label = "created"  # created by /mock/v1, measurement uploaded through /api/v2 now
            attempt_id = seeded.attempts[label]["attempt_id"]
            token = seeded.issue_session_token()
            condition = h.attempt(token, attempt_id)["condition"]
            h.upload(token, attempt_id, condition)
        attempt = store.row(f"ATTEMPT#{attempt_id}", "META")
        assert attempt.get("course_binding") is None
        program_slot = f'{attempt["program_id"]}:{attempt["target"]}'
        before_keys = _keys(store)
        before_user = store.row(f"USER#{seeded.principal}", "STATE")
        assert before_user["slots"][program_slot]["open_attempts"] == 1
        job_id = attempt["job_id"]
        assert h.work(job_id=job_id) is True
        finished = store.row(f"ATTEMPT#{attempt_id}", "META")
        assert finished["state"] == "evaluated"
        assert store.row(f"JOB#{job_id}", "STATE")["state"] == "done"
        user = store.row(f"USER#{seeded.principal}", "STATE")
        assert user["slots"][program_slot]["open_attempts"] == 0
        if label == "queued":
            # Compression goal: a pass completes the legacy slot (legacy finalize kept, D103).
            assert finished["evaluation"]["program_completed"] is True
            assert finished["progress_application"] == {
                "applied": True, "applied_epoch": attempt["epoch"], "reason": "APPLIED"}
            assert user["slots"][program_slot]["completed_by_attempt"] == attempt_id
        else:
            # Cycle goal: pending policy never completes the legacy slot.
            assert finished["evaluation"]["program_completed"] is False
            assert finished["progress_application"]["reason"] == "GOAL_POLICY_UNRESOLVED"
            assert user["slots"][program_slot] == before_user["slots"][program_slot] | {"open_attempts": 0}
        # Finalize writes no new DynamoDB row: no SUBMISSION, no ARC outbox. The
        # only OUTBOX rows are the normal calculation dispatch, one per job.
        assert _keys(store) == before_keys
        assert not [key for key in before_keys if key[0].startswith("SUBMISSION#")]
        outboxes = sorted(key[0] for key in before_keys if key[0].startswith("OUTBOX#"))
        jobs = sorted("OUTBOX#" + key[0].removeprefix("JOB#") for key in before_keys if key[0].startswith("JOB#"))
        assert outboxes == jobs
        if label == "created":
            result = h.result(token, attempt_id)
            assert result["calculationStatus"] == "succeeded"
            assert result["evaluation"] == finished["evaluation"]


def test_sealed_job_blocks_claim(dynamodb_client, dynamodb_table):
    store = JourneyStore(dynamodb_client, dynamodb_table)
    seeded = seed_legacy_rows(store)
    now = seeded.meta["clock_at_end"]
    state = DynamoStateRepository(dynamodb_client, dynamodb_table, clock=lambda: now)
    jobs = DynamoJobRepository(state)
    session = store.row(f"SESSION#{seeded.session_id}", "AUTH")
    auth = AuthContext(session["session_id"], session["principal"], session["revision"], session["expires_at"])
    attempt = seeded.attempts["created"]["row"]
    accepted = jobs.accept_input(
        auth, attempt["attempt_id"], "a" * 64,
        {"bucket": "b", "key": "k", "sha256": "a" * 64, "size": 1},
        job_id=str(uuid.uuid4()), adapter_version="arc-internal-detection-pending-v3",
        next_due_at=now,
    )
    job_id = accepted["job_id"]
    owner = "worker-a"
    action, job = jobs.claim(job_id, owner, 60)
    assert action == "execute"
    dynamodb_client.update_item(
        TableName=dynamodb_table,
        Key={"PK": {"S": f"JOB#{job_id}"}, "SK": {"S": "STATE"}},
        UpdateExpression="SET terminal_seal = :seal, #s = :failed",
        ExpressionAttributeNames={"#s": "state"},
        ExpressionAttributeValues={
            ":seal": {"M": {"evidence_digest": {"S": "e" * 64}, "job_id": {"S": job_id}}},
            ":failed": {"S": "failed"},
        },
    )
    with pytest.raises(JourneyError) as failure:
        jobs.claim(job_id, "worker-b", 60)
    assert failure.value.code == "INVALID_STATE"

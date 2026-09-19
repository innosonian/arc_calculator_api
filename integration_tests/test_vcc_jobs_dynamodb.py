"""Finalize/seal against DynamoDB Local. No ARC outbound queue is created."""

import json
import uuid
from types import SimpleNamespace

from mock_journey.errors import JourneyError
from mock_journey.jobs import DynamoJobRepository
from mock_journey.state import DynamoStateRepository


def _session(state, principal="dummy-tester"):
    session = {
        "session_id": str(uuid.uuid4()), "principal": principal,
        "token_hash": "c" * 64, "issued_at": state._now(), "expires_at": state._now() + 86400,
        "status": "active", "revision": 0,
    }
    state.create_session(session, ["mock-compression-only:adult"])
    from mock_journey.models import AuthContext
    return AuthContext(session["session_id"], principal, 0, session["expires_at"]), session


def test_finalize_without_course_binding_writes_no_submission(dynamodb_client, dynamodb_table):
    from tests.vcc_runtime_support import runtime, submit
    env = runtime(dynamodb_client, dynamodb_table)
    status, created = env.app.journey.create_attempt(env.auth, {
        "client_request_id": str(uuid.uuid4()), "catalog_version": "mock-catalog-v1",
        "program_id": "mock-compression-only", "target": "adult",
    })
    assert status == 201
    attempt = env.app.state.get_attempt(env.auth, created["attempt_id"])
    assert attempt.get("course_binding") is None
    job_id = submit(env, attempt)
    assert env.worker.process(job_id) is True
    finished = env.app.state.get_attempt(env.auth, attempt["attempt_id"])
    assert finished["state"] == "evaluated"
    assert finished["evaluation"]["program_completed"] is True
    assert env.app.calculation.jobs.get_job(job_id)["state"] == "done"
    keys = [item["PK"]["S"] for item in dynamodb_client.scan(TableName=dynamodb_table)["Items"]]
    assert not any(key.startswith("SUBMISSION#") for key in keys)
    # The normal calculation dispatch outbox is allowed; no ARC outbox is made.
    assert sum(key.startswith("OUTBOX#") for key in keys) == 1


def test_sealed_job_blocks_claim(dynamodb_client, dynamodb_table):
    state = DynamoStateRepository(dynamodb_client, dynamodb_table, clock=lambda: 1_800_000_000)
    jobs = DynamoJobRepository(state)
    auth, _ = _session(state)
    template = {
        "attempt_id": str(uuid.uuid4()), "principal": auth.principal,
        "creator_session_id": auth.session_id, "bound_session_id": auth.session_id,
        "program_id": "mock-compression-only", "target": "adult", "profile_name": "tester",
        "definition_json": json.dumps({
            "condition": {"target": "adult"}, "calculation_profile": {},
            "profile_version": "tester-goal-pending-v2",
            "adapter_version": "arc-internal-detection-pending-v3",
            "projection_version": "arc-local-projection-v1",
            "goal": {"kind": "compressions", "required": 60},
            "catalog_version": "mock-catalog-v1",
        }),
        "resume_nonce": "n", "resume_key_version": "v1", "resume_digest": "d" * 64,
    }
    attempt = state.create_attempt(auth, str(uuid.uuid4()), "digest-seal", template)
    accepted = jobs.accept_input(
        auth, attempt["attempt_id"], "a" * 64,
        {"bucket": "b", "key": "k", "sha256": "a" * 64, "size": 1},
        job_id=str(uuid.uuid4()), adapter_version="arc-internal-detection-pending-v3",
        next_due_at=1_800_000_000,
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
    try:
        jobs.claim(job_id, "worker-b", 60)
        raise AssertionError("sealed job must not be claimed")
    except JourneyError as error:
        assert error.code == "INVALID_STATE"

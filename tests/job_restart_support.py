"""Scripted job/attempt/user rows and finalize inputs shared by restart-limit tests.

Moved unchanged from tests/test_job_restart_limit.py so other test modules no
longer import a test module; that module re-exports the same objects.
"""

import hashlib
import json

from mock_journey.jobs import DynamoJobRepository
from mock_journey.state import DynamoStateRepository
from tests.mock_state_support import ScriptedClient, item, snapshot


DEFINITION = json.dumps({"goal": {"kind": "compressions", "required": 60},
                         "adapter_version": "v1", "projection_version": "v1"})
PENDING_DEFINITION = json.dumps({"goal": {"kind": "cycles", "required": 3},
                                 "adapter_version": "arc-internal-detection-pending-v3",
                                 "profile_version": "tester-goal-pending-v2", "projection_version": "v1"})
SLOT = "mock-compression-only:adult"


def started_job(**changes):
    return {
        "PK": "JOB#job", "SK": "STATE", "job_id": "job", "attempt_id": "attempt", "principal": "tester",
        "epoch": "epoch-a", "input_digest": "a" * 64, "adapter_version": "v1", "projection_version": "v1",
        "input_manifest_ref": {"bucket": "b", "key": "input", "sha256": "a" * 64, "size": 1},
        "definition_sha256": hashlib.sha256(DEFINITION.encode()).hexdigest(),
        "state": "running", "call_phase": "started", "call_id": "old-call", "execution_fence": 1,
        "fence": 2, "owner": "worker", "lease_until": 100, "revision": 3,
        "planned_candidate_ref": {"bucket": "b", "key": "old"}, "candidate_ref": None,
        "chart_snapshot": {"kind": "unset", "revision": 0}, "final_ref": None, "chart_publication": None,
        "error_code": None, "next_due_at": 100, "GSI1PK": "DUE#JOB", "GSI1SK": 100, **changes,
    }


def attempt_row(**changes):
    return {
        "PK": "ATTEMPT#attempt", "SK": "META", "attempt_id": "attempt", "job_id": "job",
        "principal": "tester", "bound_session_id": "session", "epoch": "epoch-a", "input_digest": "a" * 64,
        "definition_json": DEFINITION, "program_id": "mock-compression-only", "target": "adult",
        "state": "processing", "revision": 4, "active_counted": True, "evaluation": None,
        "progress_application": None, **changes,
    }


def user_row(*, completed=False, open_attempts=1, epoch="epoch-a"):
    return {"PK": "USER#tester", "SK": "STATE", "principal": "tester", "epoch": epoch, "revision": 7,
            "slots": {SLOT: {"completed": completed, "completed_by_attempt": None, "completed_at": None,
                             "open_attempts": open_attempts}}}


def repo(calls):
    client = ScriptedClient(calls)
    return DynamoJobRepository(DynamoStateRepository(client, "restart-test", clock=lambda: 20)), client


def close_calls(job, attempt=None, user=None, *, write=True):
    attempt = attempt or attempt_row()
    user = user or user_row()
    return [("get", {"Item": item(job)}), ("read", snapshot(job, attempt, user))] + ([("write", {})] if write else [])


def finalize_job(definition):
    return started_job(call_phase="candidate_saved", call_id="call", execution_fence=2,
                       planned_candidate_ref={"bucket": "b", "key": "k"},
                       candidate_ref={"bucket": "b", "key": "k", "sha256": "b" * 64, "size": 1},
                       chart_snapshot={"kind": "no_chart", "revision": 1},
                       definition_sha256=hashlib.sha256(definition.encode()).hexdigest())


def evaluation(*, met, passed, pending=False):
    if pending:
        return {"goal": {"kind": "cycles", "required": 3, "observed": None, "met": None,
                         "status": "pending_policy"},
                "score": {"decision": "pass" if passed else "fail"}, "program_completed": False,
                "reason_codes": ["GOAL_POLICY_UNRESOLVED"] + ([] if passed else ["SCORE_NOT_PASS"])}
    return {"goal": {"kind": "compressions", "required": 60, "observed": 60 if met else 10, "met": met},
            "score": {"decision": "pass" if passed else "fail"}, "program_completed": met and passed,
            "reason_codes": ([] if met else ["GOAL_NOT_MET"]) + ([] if passed else ["SCORE_NOT_PASS"])}


def publication(job):
    keys = ("attempt_id", "epoch", "input_digest", "adapter_version", "projection_version", "job_id", "call_id")
    return {**{key: job[key] for key in keys}, "kind": "no_chart", "selection_revision": 1}


FINAL_REF = {"bucket": "b", "key": "final", "sha256": "c" * 64, "size": 1}

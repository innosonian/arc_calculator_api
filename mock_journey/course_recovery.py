"""Read-only recovery proof from an injected, consistent storage snapshot."""

import hashlib

from mock_journey.course_contracts import CourseBinding, RecoveryEvidence, require_hash, require_uuid
from mock_journey.course_errors import CourseError
from mock_journey.typed import digest, json_bytes, parse_json


_TERMINAL_CODES = frozenset({"STORED_INPUT_INVALID", "CALCULATOR_CONTRACT_MISMATCH", "CALCULATION_FAILED"})


def _fail(code="STORED_INPUT_INVALID"):
    raise CourseError(code)


def _row(snapshot, key):
    value = snapshot.get(key)
    if type(value) is not dict:
        _fail()
    return value


def _definition(attempt):
    value = attempt.get("definition_json")
    if type(value) is str:
        body = value.encode("utf-8")
    elif type(value) is bytes:
        body = value
    elif type(value) is dict:
        body = json_bytes(value)
    else:
        _fail()
    try:
        parsed = parse_json(body)
    except (TypeError, ValueError):
        _fail()
    if type(parsed) is not dict:
        _fail()
    return parsed, hashlib.sha256(body).hexdigest()


def _binding(attempt):
    value = attempt.get("course_binding")
    if type(value) is CourseBinding:
        return value
    if type(value) is not dict:
        _fail()
    try:
        return CourseBinding(**value)
    except (TypeError, CourseError):
        _fail()


def recovery_snapshot_context(snapshot) -> dict:
    """Immutable context that the transaction must re-read and compare."""
    job, attempt, user, head, final = (_row(snapshot, key) for key in ("job", "attempt", "user", "head", "final"))
    binding = _binding(attempt)
    definition, definition_sha256 = _definition(attempt)
    return {
        "user_revision": user.get("revision"), "user_epoch": user.get("epoch"),
        "head_revision": head.get("revision"), "head_definition_hash": head.get("definition_hash"),
        "final_revision": final.get("revision"), "final_active_attempt_id": final.get("active_attempt_id"),
        "final_phase": final.get("phase"), "binding_digest": digest(binding.__dict__),
        "definition_sha256": definition_sha256,
        "adapter_version": definition.get("adapter_version"),
        "projection_version": definition.get("projection_version"),
        "candidate_ref": job.get("candidate_ref"), "planned_candidate_ref": job.get("planned_candidate_ref"),
    }


def _validate_snapshot(snapshot, job_id, owner, fence):
    job, attempt, user, head, final = (_row(snapshot, key) for key in ("job", "attempt", "user", "head", "final"))
    if job.get("job_id") != job_id:
        _fail("NOT_FOUND")
    if type(fence) is not int or fence < 0 or (owner is not None and (type(owner) is not str or not owner)):
        _fail("INVALID_REQUEST")
    if type(job.get("fence")) is not int or job["fence"] < 0:
        _fail()
    if job.get("owner") != owner or job.get("fence") != fence:
        _fail("INVALID_STATE")
    if job.get("terminal_seal") or job.get("final_ref") is not None or attempt.get("result_ref") is not None:
        _fail("INVALID_STATE")
    if job.get("state") == "done" or attempt.get("state") == "evaluated":
        _fail("INVALID_STATE")
    require_uuid(attempt.get("attempt_id"))
    if job.get("attempt_id") != attempt["attempt_id"] or attempt.get("job_id") != job_id:
        _fail()
    binding = _binding(attempt)
    if job.get("epoch") != attempt.get("epoch") or binding.epoch != attempt.get("epoch"):
        _fail()
    principal = attempt.get("principal")
    if type(principal) is not str or not principal or job.get("principal") != principal or user.get("principal") != principal:
        _fail()
    require_hash(job.get("input_digest"))
    if attempt.get("input_digest") != job["input_digest"]:
        _fail()
    if type(snapshot.get("now")) is not int:
        _fail()
    for row in (job, attempt, user, head, final):
        if type(row.get("revision")) is not int or row["revision"] < 0:
            _fail()
    if type(user.get("epoch")) is not str or not user["epoch"]:
        _fail()
    for row in (head, final):
        if row.get("epoch") != binding.epoch or row.get("scope_key") != binding.scope_key:
            _fail()
    require_hash(head.get("definition_hash"))
    if binding.start_role == "final_assessment" and (
        final.get("active_attempt_id") != attempt["attempt_id"]
        or final.get("phase") not in {"active", "recovery_required"}
    ):
        _fail("INVALID_STATE")
    definition, definition_sha = _definition(attempt)
    if job.get("definition_sha256") != definition_sha:
        _fail()
    if any(type(definition.get(key)) is not str or not definition[key]
           or job.get(key) != definition[key] for key in ("adapter_version", "projection_version")):
        _fail()
    call_id = job.get("call_id")
    if type(call_id) is not str or not call_id or job.get("call_phase") not in {"started", "candidate_saved"}:
        _fail("INVALID_STATE")
    return job, attempt


def _input_verified(snapshot):
    proof, job = snapshot.get("input_lookup"), snapshot["job"]
    return type(proof) is dict and (
        proof.get("status") == "ok" and proof.get("input_digest") == job["input_digest"]
        and proof.get("hash_matches") is True and proof.get("definition_matches") is True
    )


def _candidate_state(snapshot):
    lookup, job = snapshot.get("candidate_lookup"), snapshot["job"]
    if type(lookup) is not dict or lookup.get("status") != "ok":
        return "unreadable"
    committed = lookup.get("committed_present")
    uncommitted = lookup.get("uncommitted_present")
    if type(committed) is not bool or type(uncommitted) is not bool:
        return "unreadable"
    saved = job.get("call_phase") == "candidate_saved" or job.get("candidate_ref") is not None
    if saved and committed is not True:
        return "missing_committed"
    if committed or uncommitted:
        if lookup.get("bytes_readable") is not True:
            return "unreadable"
        if lookup.get("hash_matches") is not True or lookup.get("response_valid") is not True:
            return "invalid"
        return "valid"
    return "absent_uncommitted"


def _lease_held(snapshot, owner, fence):
    job = snapshot["job"]
    return (
        type(owner) is str and bool(owner) and job.get("owner") == owner and job.get("fence") == fence
        and type(job.get("lease_until")) is int and job["lease_until"] > snapshot["now"]
    )


def _adapter_ready(snapshot):
    adapter = snapshot.get("adapter")
    return type(adapter) is dict and (
        adapter.get("present") is True and adapter.get("can_verify") is True
        and adapter.get("adapter_version") == snapshot["job"]["adapter_version"]
    )


def _action(state, snapshot, owner, fence):
    if not _input_verified(snapshot):
        return "wait_integrity"
    if not _adapter_ready(snapshot):
        return "wait_configuration"
    if state == "valid":
        return "resume_candidate"
    if state != "absent_uncommitted":
        return "wait_integrity"
    job = snapshot["job"]
    if job.get("state") == "outcome_unknown" or job.get("error_code") == "CALCULATION_OUTCOME_UNKNOWN":
        return "wait_integrity"
    terminal = snapshot.get("terminal") or {}
    if type(terminal) is not dict:
        return "wait_integrity"
    if terminal.get("proven_local_error") is True:
        if (
            _lease_held(snapshot, owner, fence) and job.get("error_code") in _TERMINAL_CODES
            and terminal.get("response_assembly_failure") is False
            and terminal.get("file_save_failure") is False
            and job.get("candidate_ref") is None
        ):
            return "close_terminal"
        return "wait_integrity"
    if snapshot["adapter"].get("can_calculate") is not True:
        return "wait_configuration"
    chart = job.get("chart_snapshot")
    if (
        job.get("state") == "running" and job.get("call_phase") == "started"
        and job.get("candidate_ref") is None and type(chart) is dict and chart.get("kind") == "unset"
        and _lease_held(snapshot, owner, fence)
    ):
        return "retry_local_call"
    return "wait_integrity"


class CourseRecovery:
    """Inspect only; callers must re-inspect and CAS snapshot_json before mutation."""

    def __init__(self, *, reader):
        if reader is None or not callable(getattr(reader, "load_consistent", None)):
            raise ValueError("A recovery reader is required.")
        self._reader = reader

    def inspect(self, job_id: str, owner: object, fence: object) -> RecoveryEvidence:
        if type(job_id) is not str or not job_id:
            _fail("INVALID_REQUEST")
        try:
            snapshot = self._reader.load_consistent(job_id, owner, fence)
        except CourseError:
            raise
        except (OSError, TimeoutError, ConnectionError):
            raise CourseError("TEMPORARILY_UNAVAILABLE") from None
        if snapshot is None:
            _fail("NOT_FOUND")
        if type(snapshot) is not dict:
            _fail()
        job, attempt = _validate_snapshot(snapshot, job_id, owner, fence)
        state = _candidate_state(snapshot)
        action = _action(state, snapshot, owner, fence)
        code = None
        if action == "close_terminal":
            code = job["error_code"]
        elif action == "wait_configuration":
            code = "TEMPORARILY_UNAVAILABLE"
        elif action == "wait_integrity":
            code = ("CALCULATION_OUTCOME_UNKNOWN" if job.get("error_code") == "CALCULATION_OUTCOME_UNKNOWN"
                    or job.get("state") == "outcome_unknown" else "STORED_INPUT_INVALID")
        return RecoveryEvidence(
            job_id, attempt["attempt_id"], job["revision"], attempt["revision"], attempt["epoch"],
            job["call_id"], job["fence"], job["input_digest"], job["call_phase"], state, code, action,
            snapshot_json=json_bytes(recovery_snapshot_context(snapshot)),
        )

"""Atomic finalize write-set and launch-only ARC intent. No SDK commit or network."""

from mock_journey.course_contracts import (
    CONTRACT_VERSION, EXCLUSION_REASON_ORDER, EXECUTION_KEYS, FINAL_PHASES, POLICY_VERSION,
    RECEIPT_FORBIDDEN_KEYS, ArcReceipt, CourseBinding, CourseBundle, WritePlan, parse_owned,
    require_hash, require_member, require_uuid,
)
from mock_journey.course_errors import CourseError
from mock_journey.course_policy import CoursePolicy, placement_key, scope_key
from mock_journey.typed import digest, json_bytes


SUBMISSION_POLICY_VERSION = "vcc-submission-policy-v1"
WRITE_PLAN_SCHEMA = "vcc-finalize-write-plan-v1"
ARC_GUIDELINES = frozenset({"ARC2020", "ARC2025"})
_EVALUATION_KEYS = frozenset({"goal", "score", "program_completed", "reason_codes"})
_TERMINAL_PROGRESS = frozenset({
    "APPLIED", "REQUIREMENTS_NOT_MET", "GOAL_POLICY_UNRESOLVED", "ALREADY_COMPLETED", "PROGRESS_RESET",
    "PROGRESS_RECONCILIATION_REQUIRED",
})

HOOK_REQUESTS = """
W5 hooks requested by W4. Do not implement in W4 files. Keep one existing finalize
transaction; never Mock-slot commit then course commit. V IDs: V12, V18, V19, V20, V21.

1) optional course_binding on the existing attempt template whitelist
   signature: AuthManager.prepare_resume(template) and DynamoStateRepository.create_attempt
              continue to require today's _TEMPLATE_FIELDS. Add optional
              template['course_binding'] only when type is CourseBinding (or its
              frozen field dict) and branch the whitelist. Do not make the field
              required on retained rows. Projection exact compare of the six-key
              condition is unchanged.
   conditions: CourseCalculationBridge.prepare already froze existing_template_json
               as 7-key execution + mapping_version, without resume secrets and
               without ExecutionCatalog.get_definition(course_id).
   errors: PROFILE_MISMATCH on condition drift; CALCULATOR_CONTRACT_MISMATCH on
           5-key definitions; EXECUTION_DEFINITION_MISSING/UNSUPPORTED from prepare.
   V IDs: V18.

2) jobs.finalize injects CourseCompletionPlan.build into the single existing transaction
   signature: DynamoJobRepository.finalize(job_id, owner, fence, final_ref, evaluation,
              chart_publication, *, completion_plan: CourseCompletionPlan | None = None)
              When attempt.course_binding is missing, keep the current legacy branch
              (WritePlan has no COURSE# / SUBMISSION# writes). When bound, merge
              plan.actions_json writes/conditions with the existing JOB/ATTEMPT/USER
              CAS. Do not call a second commit. Do not create an executable submit
              queue or OUTBOX item for ARC. DisabledArcGateway is not invoked from
              finalize in the launch version.
   conditions: owner/fence/lease/candidate/chart publication and USER epoch stay as
               today. Course ITEM/HEAD/FINAL and SUBMISSION#<attempt>/RESULT#<digest>
               join the same TransactWrite. Previous attempt.epoch != user.epoch
               must not Put/Update current COURSE HEAD/ITEM/FINAL. max actions stay
               inside CourseSettings.max_transaction_actions (fixture finalize=7).
               rows['bundle'] must be the verified CourseBundle selected by HEAD.bundle_ref.
               HEAD progress_json/completed_placements/course_complete are one projection.
               Conditions additionally bind head_definition_hash/final_phase/final_active_attempt_id.
               Replacement final definitions retain original ITEM and enrollment Pass while
               current-view completion stays unresolved; no Pass is copied to a new item.
   errors: TEMPORARILY_UNAVAILABLE after max_conflict_retries; CALCULATOR_CONTRACT_MISMATCH
           on evaluation shape; existing JobLeaseLost on stale fence.
   V IDs: V19, V20, V21.

3) DynamoJobRepository.close_course_terminal(job_id, owner, fence, evidence)
   signature: close_course_terminal(self, job_id: str, owner: object, fence: object,
              evidence: RecoveryEvidence) -> object
   conditions: only when evidence.action == 'close_terminal' and every T5 predicate
               already inspected (proven local terminal error, trusted binding/input,
               current owner/fence/lease, no committed result, candidate search
               complete, no recoverable candidate). Same transaction: JOB
               failed+terminal_seal+evidence_digest+owner=null+due removed; ATTEMPT
               failed/evaluation=null with file refs preserved; original-epoch FINAL
               free and active_attempt_id=null only for the self-reference; HEAD
               revision++. Completion set and scores unchanged. Seal fields:
               job_id, attempt_id, epoch, call_id, fence, evidence_digest.
               evidence_digest = typed.digest of the RecoveryEvidence identity
               (job_id, attempt_id, revisions, epoch, call_id, fence, input_digest,
               stage, candidate_state, code, action). Same seal re-call is a no-op
               and must not write a newer FINAL. Previous-epoch closure must not
               read/write current USER epoch HEAD/ITEM/FINAL.
   errors: INVALID_STATE when predicates fail; TEMPORARILY_UNAVAILABLE on conflict;
           never interpret failed/error_code/lease expiry/active_counted=false/app 30s
           as permission to free FINAL.
   V IDs: V12, V19.

4) DynamoJobRepository.reopen_course_recovery(job_id, evidence)
   signature: reopen_course_recovery(self, job_id: str, evidence: RecoveryEvidence) -> object
   conditions: exact input + valid candidate + adapter verify, no result, FINAL still
               self-references this attempt, JOB/ATTEMPT/HEAD/FINAL revisions match.
               Preserve failure history and create recovery due. Next claim issues a
               new owner/lease and higher fence then resume_candidate (recalc 0).
               Not for sealed jobs, changed input, or retained Mock failed rows
               without a valid candidate.
   errors: INVALID_STATE otherwise. Do not auto-reopen from claim().
   V IDs: V12, V19.

5) seal checks on claim / begin_calculation / mark_calculation_saved / finalize
   signature: each existing method; if job.terminal_seal is set, refuse mutation
              except close_course_terminal no-op on the same seal.
   conditions: sealed job has no automatic reopen. Late finalize after seal does
               not change current FINAL. Old worker fence cannot free a new FINAL.
   errors: INVALID_STATE or JobLeaseLost. CALCULATION_OUTCOME_UNKNOWN stays
           recovery_required, not close_terminal.
   V IDs: V12, V19.
"""


def _fail(code="INVALID_REQUEST"):
    raise CourseError(code)


def _mapping(row):
    if row is None:
        return None
    if type(row) is dict:
        return row
    if type(row) is CourseBinding:
        return {
            "scope_key": row.scope_key, "placement_key": row.placement_key,
            "start_role": row.start_role, "definition_hash": row.definition_hash,
            "content_version": row.content_version, "epoch": row.epoch,
            "policy_version": row.policy_version,
        }
    values = getattr(row, "__dict__", None)
    return values if type(values) is dict else None


def _get(row, name, default=None):
    data = _mapping(row)
    if data is None:
        return default
    return data.get(name, default)


def _binding(attempt):
    raw = _get(attempt, "course_binding")
    if raw is None:
        return None
    if type(raw) is CourseBinding:
        return raw
    data = _mapping(raw)
    if data is None:
        _fail()
    try:
        return CourseBinding(
            data["scope_key"], data["placement_key"], data["start_role"], data["definition_hash"],
            data["content_version"], data["epoch"], data["policy_version"],
        )
    except (KeyError, TypeError, CourseError):
        raise CourseError("INVALID_REQUEST") from None


def _json_object(value):
    if value is None:
        return None
    if type(value) is dict:
        return parse_owned(json_bytes(value))
    if type(value) is bytes:
        parsed = parse_owned(value)
        return parsed if type(parsed) is dict else _fail()
    if type(value) is str:
        parsed = parse_owned(value.encode("utf-8"))
        return parsed if type(parsed) is dict else _fail()
    _fail()


def _evaluation(verified_result):
    raw = verified_result if type(verified_result) is dict else _mapping(verified_result)
    if raw is None:
        _fail("CALCULATOR_CONTRACT_MISMATCH")
    if _EVALUATION_KEYS <= set(raw):
        payload = raw
    elif type(raw.get("evaluation")) is dict:
        payload = raw["evaluation"]
    else:
        _fail("CALCULATOR_CONTRACT_MISMATCH")
    if type(payload) is not dict or set(payload) != _EVALUATION_KEYS:
        _fail("CALCULATOR_CONTRACT_MISMATCH")
    goal, score = payload["goal"], payload["score"]
    if type(goal) is not dict or type(score) is not dict:
        _fail("CALCULATOR_CONTRACT_MISMATCH")
    status = goal.get("status")
    completed = payload["program_completed"]
    if type(completed) is not bool:
        _fail("CALCULATOR_CONTRACT_MISMATCH")
    if status == "pending_policy":
        if completed is True or goal.get("kind") != "cycles":
            _fail("CALCULATOR_CONTRACT_MISMATCH")
    elif status == "evaluated":
        if type(goal.get("observed")) is not int or type(goal.get("met")) is not bool:
            _fail("CALCULATOR_CONTRACT_MISMATCH")
    else:
        _fail("CALCULATOR_CONTRACT_MISMATCH")
    if type(score.get("decision")) is not str or score["decision"] not in ("pass", "fail"):
        _fail("CALCULATOR_CONTRACT_MISMATCH")
    return parse_owned(json_bytes(payload))


def _guideline(attempt, evaluation):
    for source in (_json_object(_get(attempt, "definition_json")), _json_object(_get(attempt, "condition")), evaluation):
        if type(source) is dict:
            condition = source.get("condition") if type(source.get("condition")) is dict else source
            value = condition.get("guideline") if type(condition) is dict else None
            if type(value) is str and value:
                return value
    _fail("CALCULATOR_CONTRACT_MISMATCH")


def _digest_of(value):
    if type(value) is str:
        return require_hash(value)
    return digest(value)


def submission_intent_id(attempt_id, input_digest, result_digest, *, policy_version=SUBMISSION_POLICY_VERSION):
    require_uuid(attempt_id)
    require_hash(input_digest)
    require_hash(result_digest)
    if type(policy_version) is not str or not policy_version:
        _fail()
    return digest([attempt_id, input_digest, result_digest, policy_version])


def collect_exclusion_reasons(*, is_dummy, guideline, attempt_epoch, current_epoch, already_finalized):
    if type(is_dummy) is not bool or type(guideline) is not str or not guideline:
        _fail()
    if type(attempt_epoch) is not str or not attempt_epoch or type(current_epoch) is not str or not current_epoch:
        _fail()
    if type(already_finalized) is not bool:
        _fail()
    found = []
    if is_dummy:
        found.append("dummy")
    if guideline not in ARC_GUIDELINES:
        found.append("non_arc_guideline")
    if not already_finalized and attempt_epoch != current_epoch:
        found.append("progress_reset_before_result")
    return tuple(reason for reason in EXCLUSION_REASON_ORDER if reason in found)


def classify_launch_status(reasons):
    owned = tuple(reasons)
    extra = set(owned) - set(EXCLUSION_REASON_ORDER)
    if extra or owned != tuple(reason for reason in EXCLUSION_REASON_ORDER if reason in owned):
        _fail()
    if owned:
        return {
            "status": "excluded", "ok": False, "error": None, "exclusion_reasons": list(owned),
        }
    return {
        "status": "disabled", "ok": False, "error": "arc_contract_pending", "exclusion_reasons": [],
    }


def classify_submission(
    binding, verified_result_json, *, is_dummy, current_epoch, already_finalized=False,
    attempt_epoch=None, guideline=None,
):
    """Pure launch classification. Eligible complete and incomplete stay disabled."""
    parsed = verified_result_json if type(verified_result_json) is dict else _json_object(verified_result_json)
    evaluation = _evaluation(parsed)
    bound = binding if type(binding) is CourseBinding else _binding({"course_binding": binding})
    epoch = attempt_epoch if attempt_epoch is not None else (bound.epoch if bound is not None else current_epoch)
    guide = guideline if guideline is not None else _guideline(parsed, evaluation)
    reasons = collect_exclusion_reasons(
        is_dummy=is_dummy, guideline=guide, attempt_epoch=epoch,
        current_epoch=current_epoch, already_finalized=already_finalized,
    )
    return json_bytes(classify_launch_status(reasons))


def final_phase_for_evaluation(evaluation, *, start_role):
    require_member(start_role, {"training", "final_assessment"})
    payload = _evaluation(evaluation)
    if start_role != "final_assessment":
        return None
    status = payload["goal"]["status"]
    if status == "pending_policy":
        phase = "policy_pending"
    elif payload["program_completed"] is True:
        phase = "passed"
    else:
        # Score Pass is not enrollment pass. Only 60 observed 59 stays free.
        phase = "free"
    require_member(phase, FINAL_PHASES)
    return phase


def _already_finalized(job, attempt):
    return _get(job, "state") == "done" or _get(attempt, "state") == "evaluated"


def _stored_submit(attempt, course_rows):
    for source in (attempt, course_rows):
        raw = _get(source, "submit_arc")
        if raw is None:
            continue
        payload = _json_object(raw)
        reasons = payload.get("exclusion_reasons", payload.get("exclusionReasons"))
        if payload.get("status") in ("disabled", "excluded") and type(reasons) is list:
            return {
                "status": payload["status"],
                "ok": False,
                "error": payload.get("error"),
                "exclusion_reasons": list(reasons),
            }
    return None


def _progress(evaluation, *, reset, item_already_completed, attempt_epoch):
    if reset:
        return {"applied": False, "applied_epoch": None, "reason": "PROGRESS_RESET"}
    if evaluation["goal"]["status"] == "pending_policy":
        return {"applied": False, "applied_epoch": None, "reason": "GOAL_POLICY_UNRESOLVED"}
    if item_already_completed:
        return {"applied": False, "applied_epoch": None, "reason": "ALREADY_COMPLETED"}
    if evaluation["program_completed"] is True:
        return {"applied": True, "applied_epoch": attempt_epoch, "reason": "APPLIED"}
    return {"applied": False, "applied_epoch": None, "reason": "REQUIREMENTS_NOT_MET"}


def _item_passed(evaluation, *, completed_write):
    if evaluation["goal"]["status"] == "pending_policy":
        return None
    return True if completed_write else False


def _forbid_secrets(value):
    if type(value) is dict:
        for key, nested in value.items():
            if key in RECEIPT_FORBIDDEN_KEYS or key in {
                "resume_credential", "resume_nonce", "resume_digest", "resume_key_version",
            }:
                _fail()
            _forbid_secrets(nested)
        return
    if type(value) is list:
        for nested in value:
            _forbid_secrets(nested)


def _course_keys(binding, epoch):
    pk = f"COURSE#{binding.scope_key}"
    return {
        "head": (pk, f"EPOCH#{epoch}#HEAD"),
        "item": (pk, f"EPOCH#{epoch}#ITEM#{binding.placement_key}"),
        "final": (pk, f"EPOCH#{epoch}#FINAL"),
    }


def _course_writes(binding, attempt, rows, evaluation, phase):
    """Build all derived course state from the same verified read snapshot."""
    attempt_id = _get(attempt, "attempt_id")
    bundle = rows.get("bundle")
    head, item, final = (rows.get(name) for name in ("head", "item", "final"))
    if type(bundle) is not CourseBundle or any(type(row) is not dict for row in (head, item, final)):
        _fail("TEMPORARILY_UNAVAILABLE")
    if scope_key(bundle.scope) != binding.scope_key or head.get("definition_hash") != bundle.definition_hash:
        _fail("TEMPORARILY_UNAVAILABLE")
    if any(type(row.get("revision")) is not int or row["revision"] < 0 for row in (head, item, final)):
        _fail("TEMPORARILY_UNAVAILABLE")
    if any(row.get("epoch") != binding.epoch for row in (head, item, final)):
        _fail("TEMPORARILY_UNAVAILABLE")
    if (head.get("scope_key") != binding.scope_key or item.get("placement_key") != binding.placement_key
            or placement_key(binding.scope_key, item.get("source_id")) != binding.placement_key):
        _fail("TEMPORARILY_UNAVAILABLE")
    if binding.start_role == "final_assessment" and (
        final.get("active_attempt_id") != attempt_id or final.get("phase") not in {"active", "recovery_required"}
    ):
        _fail("INVALID_STATE")
    current_progress = _json_object(head.get("progress_json"))
    if current_progress is None:
        _fail("TEMPORARILY_UNAVAILABLE")
    current = next((p for p in bundle.placements
                    if placement_key(binding.scope_key, p.source_id) == binding.placement_key), None)
    current_last = bundle.placements[-1] if bundle.placements else None
    started_definition = _json_object(_get(attempt, "definition_json"))
    current_definition = parse_owned(current.execution_json) if current and current.execution_json else None
    same_execution = (
        type(started_definition) is dict and type(current_definition) is dict
        and all(started_definition.get(key) == current_definition.get(key) for key in EXECUTION_KEYS)
    )
    saved_identity = _get(attempt, "content_identity_hash")
    same_content = current is not None and (
        saved_identity == digest(parse_owned(current.content_identity_json))
        if type(saved_identity) is str else binding.definition_hash == bundle.definition_hash
    )
    reconciliation = binding.start_role == "final_assessment" and (
        head.get("assessment_reconciliation_required") is True
        or current is None or current != current_last or current.kind != "assessment"
        or current.content_version != binding.content_version
        or not same_execution or not same_content
    )
    completed = item.get("completed") is True or evaluation["program_completed"] is True
    passed = item.get("passed") if item.get("completed") is True else _item_passed(
        evaluation, completed_write=evaluation["program_completed"] is True,
    )
    item_update = {"completed": completed, "passed": passed}
    if item.get("completed") is not True and completed:
        item_update.update(
            completed_by_attempt=attempt_id, completion_definition_hash=binding.definition_hash,
            content_version=binding.content_version,
        )
    final_update = {
        key: final.get(key) for key in (
            "phase", "active_attempt_id", "passed_attempt_id", "passed_placement_key",
            "passed_definition_hash", "passed_content_version",
        )
    }
    if binding.start_role == "final_assessment":
        final_update["phase"] = phase
        final_update["active_attempt_id"] = attempt_id if phase == "policy_pending" else None
        if phase == "passed":
            final_update.update(
                passed_attempt_id=attempt_id, passed_placement_key=binding.placement_key,
                passed_definition_hash=binding.definition_hash, passed_content_version=binding.content_version,
            )
    progress_items = dict(current_progress.get("items") or {})
    progress_items[binding.placement_key] = {
        "source_id": item.get("source_id"), "public_link_id": item.get("public_link_id"),
        "kind": "assessment" if binding.start_role == "final_assessment" else "training",
        "content_version": item_update.get("content_version", item.get("content_version")),
        "completed": completed, "passed": passed,
        "completion_definition_hash": item_update.get(
            "completion_definition_hash", item.get("completion_definition_hash")),
    }
    if reconciliation:
        # The original ITEM keeps the result; the current course view cannot
        # borrow that evidence for a replacement definition at the same key.
        progress_items[binding.placement_key]["completed"] = False
        progress_items[binding.placement_key]["passed"] = None
    current_progress["items"] = progress_items
    current_progress["assessment_reconciliation_required"] = (
        head.get("assessment_reconciliation_required") is True or reconciliation
    )
    completed_keys = list(current_progress.get("completed_placements") or [])
    if reconciliation:
        completed_keys = [key for key in completed_keys if key != binding.placement_key]
    if completed and not reconciliation and binding.placement_key not in completed_keys:
        completed_keys.append(binding.placement_key)
    current_progress["completed_placements"] = completed_keys
    current_progress["final"] = final_update
    # Do not carry an old generic evaluation across a new active final attempt.
    current_progress.pop("evaluation", None)
    aggregate = parse_owned(CoursePolicy.aggregate_progress(bundle, json_bytes(current_progress)))
    head_update = {
        "completed_placements": aggregate["completed_placements"],
        "course_complete": aggregate["course_complete"],
        "progress_json": json_bytes(aggregate).decode("utf-8"), "revision_bump": True,
    }
    if reconciliation:
        head_update.update(
            gate_state="reconciliation_required", reason="progress_reconciliation_required",
            reconciliation_reason="progress_reconciliation_required",
            assessment_reconciliation_required=True,
        )
    keys = _course_keys(binding, binding.epoch)
    writes = [
        {"kind": "COURSE_ITEM", "key": list(keys["item"]), "set": item_update},
        {"kind": "COURSE_HEAD", "key": list(keys["head"]), "set": head_update},
        {"kind": "COURSE_FINAL", "key": list(keys["final"]), "set": final_update},
    ]
    conditions = {
        "head_revision": head["revision"], "head_definition_hash": head["definition_hash"],
        "item_revision": item["revision"], "final_revision": final["revision"],
        "final_active_attempt_id": final.get("active_attempt_id"), "final_phase": final.get("phase"),
    }
    return writes, conditions, reconciliation


class CourseCompletionPlan:
    """One finalize write-set. W5 commits; this module never calls SDK."""

    def build(self, job, attempt, user, course_rows, verified_result) -> WritePlan:
        evaluation = _evaluation(verified_result)
        binding = _binding(attempt)
        attempt_id = _get(attempt, "attempt_id")
        job_id = _get(job, "job_id") or _get(attempt, "job_id")
        input_digest = _get(job, "input_digest") or _get(attempt, "input_digest")
        attempt_epoch = _get(attempt, "epoch")
        user_epoch = _get(user, "epoch")
        require_uuid(attempt_id)
        require_hash(input_digest)
        if type(job_id) is not str or not job_id or type(attempt_epoch) is not str or not attempt_epoch:
            _fail()
        if type(user_epoch) is not str or not user_epoch:
            _fail()
        result_digest = _get(verified_result, "result_digest")
        result_digest = _digest_of(result_digest) if result_digest is not None else digest(evaluation)
        already = _already_finalized(job, attempt)
        rows = course_rows if type(course_rows) is dict else _mapping(course_rows) or {}
        is_dummy = rows.get("is_dummy")
        if is_dummy is None:
            is_dummy = _get(attempt, "is_dummy")
        if is_dummy is None:
            is_dummy = _get(user, "is_dummy")
        if is_dummy is None:
            is_dummy = False
        if type(is_dummy) is not bool:
            _fail()
        guideline = _guideline(attempt, evaluation)
        if already:
            submit = _stored_submit(attempt, rows)
            if submit is None:
                submit = classify_launch_status(collect_exclusion_reasons(
                    is_dummy=is_dummy, guideline=guideline, attempt_epoch=attempt_epoch,
                    current_epoch=user_epoch, already_finalized=True,
                ))
        else:
            submit = classify_launch_status(collect_exclusion_reasons(
                is_dummy=is_dummy, guideline=guideline, attempt_epoch=attempt_epoch,
                current_epoch=user_epoch, already_finalized=False,
            ))
        intent_id = submission_intent_id(attempt_id, input_digest, result_digest)
        reset = attempt_epoch != user_epoch
        item_row = rows.get("item") if type(rows.get("item")) is dict else {}
        item_already = item_row.get("completed") is True
        progress = _progress(
            evaluation, reset=reset, item_already_completed=item_already and not reset,
            attempt_epoch=attempt_epoch,
        )
        if progress["reason"] not in _TERMINAL_PROGRESS:
            _fail()
        start_role = binding.start_role if binding is not None else "training"
        phase = final_phase_for_evaluation(evaluation, start_role=start_role) if binding is not None else None
        writes = []
        conditions = {
            "job_id": job_id,
            "job_revision": _get(job, "revision"),
            "attempt_id": attempt_id,
            "attempt_revision": _get(attempt, "revision"),
            "user_epoch": user_epoch,
            "user_revision": _get(user, "revision"),
            "owner": _get(job, "owner"),
            "fence": _get(job, "fence"),
            "attempt_epoch": attempt_epoch,
        }
        writes.append({
            "kind": "JOB",
            "key": [f"JOB#{job_id}", "STATE"],
            "set": {"state": "done", "owner": None, "error_code": None},
        })
        writes.append({
            "kind": "ATTEMPT",
            "key": [f"ATTEMPT#{attempt_id}", "META"],
            "set": {
                "state": "evaluated", "evaluation": evaluation,
                "progress_application": progress, "submit_arc": submit,
            },
        })
        affected = [
            (f"JOB#{job_id}", "STATE"),
            (f"ATTEMPT#{attempt_id}", "META"),
            (f"USER#{_get(user, 'principal') or _get(attempt, 'principal')}", "STATE"),
        ]
        if _get(attempt, "active_counted") is True and not reset:
            writes.append({
                "kind": "USER",
                "key": [f"USER#{_get(user, 'principal') or _get(attempt, 'principal')}", "STATE"],
                "set": {"epoch": user_epoch, "close_active": True},
            })
        if binding is None:
            branch = "legacy"
        else:
            branch = "course"
            submission_key = (f"SUBMISSION#{attempt_id}", f"RESULT#{result_digest}")
            writes.append({
                "kind": "SUBMISSION",
                "key": list(submission_key),
                "set": {
                    "intent_id": intent_id,
                    "status": submit["status"],
                    "ok": False,
                    "error": submit["error"],
                    "exclusion_reasons": list(submit["exclusion_reasons"]),
                    "policy_version": SUBMISSION_POLICY_VERSION,
                    "result_digest": result_digest,
                    "queue": [],
                },
            })
            affected.append(submission_key)
            if not reset:
                keys = _course_keys(binding, attempt_epoch)
                course_writes, course_conditions, reconciliation = _course_writes(
                    binding, attempt, rows, evaluation, phase,
                )
                writes.extend(course_writes)
                affected.extend(tuple(write["key"]) for write in course_writes)
                conditions.update(course_conditions)
                if reconciliation:
                    progress = {
                        "applied": False, "applied_epoch": None,
                        "reason": "PROGRESS_RECONCILIATION_REQUIRED",
                    }
                    writes[1]["set"]["progress_application"] = progress

        actions = {
            "schema": WRITE_PLAN_SCHEMA,
            "contract_version": CONTRACT_VERSION,
            "policy_version": POLICY_VERSION,
            "submission_policy_version": SUBMISSION_POLICY_VERSION,
            "branch": branch,
            "two_phase": False,
            "submit_queue": [],
            "intent": {
                "intent_id": intent_id,
                "status": submit["status"],
                "ok": False,
                "error": submit["error"],
                "exclusion_reasons": list(submit["exclusion_reasons"]),
                "result_digest": result_digest,
                "attempt_id": attempt_id,
                "input_digest": input_digest,
            },
            "conditions": conditions,
            "writes": writes,
        }
        receipt = {
            "attempt_id": attempt_id,
            "job_id": job_id,
            "evaluation": evaluation,
            "progress_application": progress,
            "submit_arc": submit,
            "intent_id": intent_id,
            "final_phase": phase,
            "result_digest": result_digest,
            "branch": branch,
        }
        _forbid_secrets(actions)
        _forbid_secrets(receipt)
        if actions["two_phase"] is not False or actions["submit_queue"] != []:
            _fail()
        return WritePlan(json_bytes(actions), json_bytes(receipt), tuple(affected))


class DisabledArcGateway:
    """Launch ArcGateway: disabled, zero network, no HSTM send."""

    def submit(self, intent) -> ArcReceipt:
        return ArcReceipt("disabled", None, None)

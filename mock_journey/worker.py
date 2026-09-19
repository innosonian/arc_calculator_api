"""Durable internal calculation, candidate recovery, and one committed result."""

from mock_journey.bootstrap import configure_imports

configure_imports()

from copy import deepcopy
from contextlib import nullcontext
import hashlib
import json
import time
import uuid

from mock_journey.contracts import (
    PENDING_GOAL_ADAPTER_VERSIONS, PENDING_GOAL_PROFILE_VERSION,
    VerifiedCalculation, VerifiedChart,
)
from mock_journey.errors import JourneyError
from mock_journey.course_errors import CourseError
from services.legacy_document import _is_pass
from services.legacy_response import DocumentSelection, finalize_legacy_response
from services.operational_logs import log_context, record_event, bind_identifiers, write_diagnostic


def input_binding(job):
    return {key: job[key] for key in (
        "attempt_id", "epoch", "input_digest", "adapter_version", "projection_version",
    )}


def call_binding(job):
    return {**input_binding(job), "job_id": job["job_id"], "call_id": job["call_id"]}


def evaluate(result, definition, verified):
    goal = definition["goal"]
    if verified.goal_kind != goal["kind"]:
        raise JourneyError("CALCULATOR_CONTRACT_MISMATCH")
    versioned_goal = definition.get("adapter_version") in PENDING_GOAL_ADAPTER_VERSIONS
    expected_status = ("pending_policy" if goal["kind"] == "cycles" else "evaluated") if versioned_goal else None
    if (verified.goal_status != expected_status
            or (versioned_goal and definition.get("profile_version") != PENDING_GOAL_PROFILE_VERSION)):
        raise JourneyError("CALCULATOR_CONTRACT_MISMATCH")
    pending = verified.goal_status == "pending_policy"
    met = None if pending else verified.observed >= goal["required"]
    passed = bool(_is_pass(result, None, None, None, definition["condition"]["target"]))
    assessed_goal = {**goal, "observed": verified.observed, "met": met}
    if versioned_goal:
        assessed_goal["status"] = verified.goal_status
    return {
        "goal": assessed_goal,
        "score": {"decision": "pass" if passed else "fail"},
        "program_completed": False if pending else met and passed,
        "reason_codes": (["GOAL_POLICY_UNRESOLVED"] if pending else [] if met else ["GOAL_NOT_MET"])
                        + ([] if passed else ["SCORE_NOT_PASS"]),
    }


class JourneyWorker:
    def __init__(self, jobs, storage, adapters, *, lease_seconds, retry_seconds, clock=time.time,
                 lease_guard_factory=None, operations=None, completion_plan=None):
        if any(type(value) is not int or value <= 0 for value in (lease_seconds, retry_seconds)):
            raise ValueError("Verified worker timing is required.")
        if lease_guard_factory is not None and not callable(lease_guard_factory):
            raise ValueError("Invalid worker lease guard factory.")
        self.lease_guard_factory = lease_guard_factory
        self.operations = operations
        self.completion_plan = completion_plan
        self.jobs, self.storage, self.adapters = jobs, storage, adapters
        self.lease_seconds, self.retry_seconds, self.clock = lease_seconds, retry_seconds, clock
        self.course_recovery = None
        if completion_plan is not None and callable(getattr(jobs, "recovery_snapshot", None)):
            from mock_journey.course_recovery import CourseRecovery
            from mock_journey.course_runtime_recovery import CourseRecoveryReader
            self.course_recovery = CourseRecovery(reader=CourseRecoveryReader(jobs, storage, adapters))
            jobs.course_recovery = self.course_recovery

    def process(self, job_id):
        with log_context(self.operations, job_id=job_id):
            return self._process(job_id)

    def _process(self, job_id):
        from mock_journey.jobs import JobLeaseLost

        owner = str(uuid.uuid4())
        is_course = callable(getattr(self.jobs, "is_course_job", None)) and self.jobs.is_course_job(job_id)
        if is_course:
            if self.completion_plan is None or self.course_recovery is None:
                return False  # A course result cannot be finalized into a legacy slot.
            existing = self.jobs.get_job(job_id)
            if existing.get("terminal_seal"):
                return True
            if existing["state"] == "failed":
                try:
                    evidence = self.course_recovery.inspect(job_id, None, existing["fence"])
                    if evidence.action != "resume_candidate":
                        return False
                    self.jobs.reopen_course_recovery(job_id, evidence)
                except (JourneyError, CourseError):
                    return False
        action, job = self.jobs.claim(job_id, owner, self.lease_seconds)
        if action == "busy":
            return False
        if action == "done":
            return True
        fence = job["fence"]
        try:
            bind_identifiers(attempt_id=job.get("attempt_id"), progress_epoch=job.get("epoch"))
        except Exception:
            pass  # Optional log context cannot bypass lease/error handling.
        record_event("calculation_started")
        calculating = False

        def heartbeat():
            self.jobs.renew_lease(job_id, owner, fence, self.lease_seconds)

        try:
            renew = heartbeat
            guard = (nullcontext(heartbeat) if self.lease_guard_factory is None
                     else self.lease_guard_factory(heartbeat))
            with guard as heartbeat:
                if not callable(heartbeat):
                    raise ValueError("Invalid worker lease guard.")
                adapter = self.adapters.resolve(job["adapter_version"], job["projection_version"])
                loaded = self.storage.load_input(job["input_manifest_ref"], input_binding(job))
                projected = loaded.projected
                raw = None
                previous_call_id = None
                if action == "recover":
                    if is_course:
                        evidence = self.course_recovery.inspect(job_id, owner, fence)
                        if evidence.action not in {"resume_candidate", "retry_local_call"}:
                            raise JourneyError("TEMPORARILY_UNAVAILABLE")
                    binding = call_binding(job)
                    planned = job["planned_candidate_ref"]
                    raw = self.storage.load_calculation(planned, binding)
                    if raw is None:
                        if job["call_phase"] == "candidate_saved" or job.get("candidate_ref") is not None:
                            raise JourneyError("STORED_INPUT_INVALID")
                        previous_call_id = job["call_id"]
                    else:
                        candidate_ref = {**planned, "sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw)}
                        job = self.jobs.mark_calculation_saved(job_id, owner, fence, candidate_ref)
                if raw is None:
                    if getattr(adapter, "can_calculate", True) is not True:
                        # Historical candidate readers must never start a new
                        # call using changed detection semantics.
                        raise JourneyError("TEMPORARILY_UNAVAILABLE")
                    heartbeat()
                    binding = {**input_binding(job), "job_id": job_id, "call_id": str(uuid.uuid4())}
                    planned = self.storage.planned_calculation(binding)
                    permitted, job = self.jobs.begin_calculation(
                        job_id, owner, fence, binding["call_id"], planned,
                        previous_call_id=previous_call_id,
                    )
                    if not permitted:
                        return False
                    # This runs the local calculator. Configuration/storage errors
                    # preserve the durable job for a later lease; domain failures
                    # are classified by the calculator and handled below.
                    calculating = True
                    raw = adapter.calculate(loaded, binding, heartbeat)
                    calculating = False
                    heartbeat()
                    candidate_ref = self.storage.save_calculation(planned, raw, binding)
                    job = self.jobs.mark_calculation_saved(job_id, owner, fence, candidate_ref)
                binding = call_binding(job)

                try:
                    verified = adapter.validate_response(raw, projected, binding)
                except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
                    raise JourneyError("CALCULATOR_CONTRACT_MISMATCH") from None
                if not isinstance(verified, VerifiedCalculation):
                    raise JourneyError("CALCULATOR_CONTRACT_MISMATCH")
                heartbeat()
                job = self.jobs.get_job(job_id)
                if job["chart_snapshot"]["kind"] == "unset":
                    if verified.chart_kind == "no_chart":
                        selection = {"kind": "no_chart", **binding}
                    else:
                        chart = adapter.get_chart(verified, binding, heartbeat)
                        if not isinstance(chart, VerifiedChart):
                            raise JourneyError("CALCULATOR_CONTRACT_MISMATCH")
                        selection = self.storage.save_chart_candidate(chart.data, chart.source_sha256, binding, fence)
                    self.jobs.pin_chart(job_id, owner, fence, selection)
                publication = self.storage.publish_selected_chart(
                    job_id, loaded.raw_base, binding,
                    lambda ident: self.jobs.get_job(ident)["chart_snapshot"],
                )
                core = deepcopy(verified.core_result)
                core["chart_dataset_url"] = self.storage.sign_chart(publication)
                payload = projected.payload
                body = {"condition": deepcopy(payload["calculation_input"]["condition"]),
                        **deepcopy(payload["response_context"])}
                document = payload["document_context"]
                try:
                    result = finalize_legacy_response(body, core, document_selection=DocumentSelection(
                        document["document_source"], deepcopy(document["document"]),
                    ))
                    evaluation = evaluate(result, payload["definition"], verified)
                    # Preserve the legacy JSON serialization rather than Decimal or
                    # reconstructing a response during subsequent GET requests.
                    response_bytes = json.dumps(result).encode("utf-8")
                except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
                    raise JourneyError("CALCULATION_FAILED") from None
                heartbeat()
                final_ref = self.storage.save_final(response_bytes, binding, fence, publication)
            # Quiesce the optional renewer and surface latent failures before
            # the final ownership CAS. It must not race a completed job.
            if self.lease_guard_factory is not None:
                final_renew = getattr(self.lease_guard_factory, "final_renew", None)
                if final_renew is None:
                    renew()
                else:
                    final_renew(renew)
            if self.completion_plan is None:
                finalized = self.jobs.finalize(
                    job_id, owner, fence, final_ref, evaluation, publication,
                )
            else:
                finalized = self.jobs.finalize(
                    job_id, owner, fence, final_ref, evaluation, publication,
                    completion_plan=self.completion_plan,
                )
            record_event("calculation_completed", state="evaluated")
            try:
                progress = finalized["progress_application"]
                record_event("progress_application", reason=progress["reason"], applied=progress["applied"],
                             program_completed=finalized["evaluation"]["program_completed"])
            except Exception:
                pass  # Logging metadata never overturns the committed result.
            return True
        except JobLeaseLost:
            record_event("calculation_deferred")
            return False
        except JourneyError as error:
            if is_course:
                try:
                    proven = calculating and error.code in {
                        "STORED_INPUT_INVALID", "CALCULATOR_CONTRACT_MISMATCH", "CALCULATION_FAILED",
                    }
                    self.jobs.defer_course_recovery(job_id, owner, fence, error.code,
                        next_due_at=int(self.clock()) + self.retry_seconds, proven_local_error=proven)
                    if proven:
                        evidence = self.course_recovery.inspect(job_id, owner, fence)
                        if evidence.action == "close_terminal":
                            self.jobs.close_course_terminal(job_id, owner, fence, evidence)
                            return True
                except (JobLeaseLost, JourneyError, CourseError):
                    pass
                record_event("calculation_deferred", error_code=error.code)
                return False
            if error.code in ("STORED_INPUT_INVALID", "CALCULATOR_CONTRACT_MISMATCH", "CALCULATION_FAILED"):
                try:
                    self.jobs.mark_failed(job_id, owner, fence, error.code)
                    record_event("calculation_failed", error_code=error.code, state="failed")
                    return True
                except (JobLeaseLost, JourneyError):
                    return False
            record_event("calculation_deferred", error_code=error.code)
            return False
        except Exception as error:
            # Retry only after a new lease. Recovery first checks the stored
            # candidate; a missing candidate gets a new fenced execution path.
            write_diagnostic("error", "request_failed", {"exception": error})
            if is_course:
                try:
                    self.jobs.defer_course_recovery(job_id, owner, fence, "TEMPORARILY_UNAVAILABLE",
                                                    next_due_at=int(self.clock()) + self.retry_seconds)
                except (JobLeaseLost, JourneyError, CourseError):
                    pass
            record_event("calculation_deferred")
            return False


def handle(event, context, worker):
    records = event.get("Records") if type(event) is dict else None
    if type(records) is not list:
        raise ValueError("Invalid queue envelope.")
    failures = []
    for record in records:
        if type(record) is not dict or type(record.get("messageId")) is not str or not record["messageId"]:
            raise ValueError("Invalid queue envelope.")
        try:
            reserve = getattr(worker, "processing_reserve_ms", None)
            if reserve is not None:
                from mock_journey.aws_logs import remaining_ms
                if remaining_ms(context) <= reserve:
                    failures.append({"itemIdentifier": record["messageId"]})
                    continue
            message = json.loads(record["body"])
            if type(message) is not dict or set(message) != {"job_id"} or type(message["job_id"]) is not str:
                raise ValueError("Invalid queue reference.")
            if str(uuid.UUID(message["job_id"])) != message["job_id"]:
                raise ValueError("Invalid queue reference.")
            if not worker.process(message["job_id"]):
                failures.append({"itemIdentifier": record["messageId"]})
        except Exception:
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}


def run(event, context):
    from mock_journey.worker_runtime import get_worker
    from mock_journey.aws_runtime import invocation
    try:
        worker = get_worker()
        with invocation(worker, context):
            return handle(event, context, worker)
    except Exception:
        # Retry the batch; never expose configuration, SDK, or incoming event text.
        raise JourneyError("TEMPORARILY_UNAVAILABLE") from None

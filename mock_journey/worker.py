"""Durable internal calculation, candidate recovery, and one committed result."""

from mock_journey.bootstrap import configure_imports

configure_imports()

from copy import deepcopy
from contextlib import nullcontext
from dataclasses import dataclass
import hashlib
import json
import time
import uuid

from mock_journey.contracts import (
    MINIMUM_QUANTITY_REASON, VerifiedCalculation, VerifiedChart,
    # input_binding/call_binding are defined in contracts and re-exported here.
    call_binding, expected_goal_status, expected_profile_version, gates_pass_on_minimum_quantity, input_binding,
    is_versioned_goal, minimum_quantity_policy,
)
from mock_journey.errors import JourneyError
from mock_journey.course_errors import CourseError
from services.legacy_document import _is_pass
from services.legacy_response import DocumentSelection, finalize_legacy_response
from services.operational_logs import log_context, record_event, bind_identifiers, write_diagnostic


class _RestartLimitReached(Exception):
    """Internal signal: the interrupted call may not be restarted again (Q4)."""


# A local calculation failure: a legacy job is marked failed; a course job that
# was calculating defers as a proven local error (its close is re-inspected).
_LOCAL_FAILURE_CODES = frozenset({"STORED_INPUT_INVALID", "CALCULATOR_CONTRACT_MISMATCH", "CALCULATION_FAILED"})
# _calculate's answer when begin_calculation refuses the call.
_REFUSED = object()


@dataclass
class _RunContext:
    """One claimed lease; the error classification reads these after a failed step."""
    job_id: str
    owner: str
    is_course: object  # is_course_job()'s value as returned; only its truth is used.
    action: str
    job: dict
    fence: object
    calculating: bool = False

    def refresh(self, job):
        """Replace the job snapshot with the row a commit returned; the later steps read this one."""
        self.job = job
        return job


def _minimum_quantity_met(definition, verified):
    """D139: whether the session meets the ARC minimum quantity (D07), for the adapters that gate the pass on it.

    The counts are the core result's whole-session action_count and the rule
    is the score policy's own predicate (contracts.minimum_quantity_policy):
    ARC2020/ARC2025 CPR only, both groups at or above their minimum. Adapters
    without the gate (their null policy already fails such a session) and
    conditions outside the policy are always met. A core without valid
    counts cannot be judged and is a contract error, never a pass.
    """
    if not gates_pass_on_minimum_quantity(definition.get("adapter_version")):
        return True
    core = verified.core_result
    counts = core.get("action_count") if type(core) is dict else None
    if (type(counts) is not dict or set(counts) != {"comp", "vent"}
            or any(type(value) is not int or value < 0 for value in counts.values())):
        raise JourneyError("CALCULATOR_CONTRACT_MISMATCH")
    return not minimum_quantity_policy(definition["condition"], counts["comp"], counts["vent"]).active


def evaluate(result, definition, verified):
    goal = definition["goal"]
    if verified.goal_kind != goal["kind"]:
        raise JourneyError("CALCULATOR_CONTRACT_MISMATCH")
    versioned_goal = is_versioned_goal(definition)
    adapter_version = definition.get("adapter_version")
    # None for an unversioned (legacy) definition: its goal carries no status.
    expected_status = expected_goal_status(goal["kind"], adapter_version)
    if (verified.goal_status != expected_status
            or (versioned_goal and definition.get("profile_version") != expected_profile_version(adapter_version))):
        raise JourneyError("CALCULATOR_CONTRACT_MISMATCH")
    pending = verified.goal_status == "pending_policy"
    met = None if pending else verified.observed >= goal["required"]
    # The unchanged tester rule on the score, and (D139) the ARC minimum
    # quantity as a separate pass condition: a session below the minimum does
    # not pass even when its displayed overall meets the threshold.
    score_passed = bool(_is_pass(result, None, None, None, definition["condition"]["target"]))
    minimum_met = _minimum_quantity_met(definition, verified)
    passed = score_passed and minimum_met
    assessed_goal = {**goal, "observed": verified.observed, "met": met}
    if versioned_goal:
        assessed_goal["status"] = verified.goal_status
    return {
        "goal": assessed_goal,
        "score": {"decision": "pass" if passed else "fail"},
        "program_completed": False if pending else met and passed,
        # Fixed order: goal reason, minimum quantity (D139), score.
        "reason_codes": (["GOAL_POLICY_UNRESOLVED"] if pending else [] if met else ["GOAL_NOT_MET"])
                        + ([] if minimum_met else [MINIMUM_QUANTITY_REASON])
                        + ([] if score_passed else ["SCORE_NOT_PASS"]),
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
        """One lease: pre-claim course checks, claim, guarded steps, final renew, commit, classification.

        Call-order contract (tests/test_worker_call_order.py): the course probe,
        the course pre-recovery and the claim stay outside the classified
        ``try``; an early ``return False`` (refused begin) happens inside the
        guard; the guard's heartbeat drives the steps while the final renew
        uses the original renewer; the handlers keep their order.
        """
        from mock_journey.jobs import JobLeaseLost

        owner = str(uuid.uuid4())
        is_course = callable(getattr(self.jobs, "is_course_job", None)) and self.jobs.is_course_job(job_id)
        if is_course:
            prepared = self._prepare_course(job_id)
            if prepared is not None:
                return prepared
        finished, run = self._claim(job_id, owner, is_course)
        if run is None:
            return finished
        try:
            bind_identifiers(attempt_id=run.job.get("attempt_id"), progress_epoch=run.job.get("epoch"))
        except Exception:
            pass  # Optional log context cannot bypass lease/error handling.
        record_event("calculation_started")
        renew = self._renewer(run)
        try:
            guard = nullcontext(renew) if self.lease_guard_factory is None else self.lease_guard_factory(renew)
            with guard as heartbeat:
                if not callable(heartbeat):
                    raise ValueError("Invalid worker lease guard.")
                produced = self._produce(run, heartbeat)
                if produced is None:
                    return False  # begin_calculation refused this call; leave inside the guard.
            # A guard that swallowed an error leaves ``produced`` unbound: the
            # final renew still runs, then UnboundLocalError is an unexpected error.
            self._final_renew(renew)
            return self._commit(run, *produced)
        except JobLeaseLost:
            record_event("calculation_deferred")
            return False
        except _RestartLimitReached:
            return self._close_restart_limit(run)
        except JourneyError as error:
            return self._on_journey_error(run, error)
        except Exception as error:
            return self._on_unexpected_error(run, error)

    # -- before the lease -----------------------------------------------------

    def _prepare_course(self, job_id):
        """A course job before claim: the process() result, or None to continue with the claim."""
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
        return None

    def _claim(self, job_id, owner, is_course):
        """(process() result, None) when there is nothing to run, else (None, the claimed run)."""
        action, job = self.jobs.claim(job_id, owner, self.lease_seconds)
        if action == "busy":
            return False, None
        if action == "done":
            return True, None
        return None, _RunContext(job_id, owner, is_course, action, job, job["fence"])

    def _renewer(self, run):
        """The fenced lease renewal; the guard wraps it and the final renew calls it directly."""
        job_id, owner, fence = run.job_id, run.owner, run.fence

        def heartbeat():
            self.jobs.renew_lease(job_id, owner, fence, self.lease_seconds)

        return heartbeat

    # -- inside the lease guard ----------------------------------------------

    def _produce(self, run, heartbeat):
        """Candidate, verification, chart and the final file: (final_ref, evaluation, publication).

        None means begin_calculation refused the call.
        """
        adapter = self.adapters.resolve(run.job["adapter_version"], run.job["projection_version"])
        loaded = self.storage.load_input(run.job["input_manifest_ref"], input_binding(run.job))
        projected = loaded.projected
        raw = self._obtain_candidate(run, adapter, loaded, heartbeat)
        if raw is _REFUSED:
            return None
        binding = call_binding(run.job)
        verified = self._verify(adapter, raw, projected, binding)
        heartbeat()
        publication = self._select_and_publish_chart(run, adapter, verified, loaded, binding, heartbeat)
        response_bytes, evaluation = self._assemble_result(verified, projected, publication)
        heartbeat()
        final_ref = self.storage.save_final(response_bytes, binding, run.fence, publication)
        return final_ref, evaluation, publication

    def _obtain_candidate(self, run, adapter, loaded, heartbeat):
        """A recovered stored candidate, or a new fenced calculation (``_REFUSED`` if not permitted)."""
        raw = None
        previous_call_id = None
        if run.action == "recover":
            if run.is_course:
                evidence = self.course_recovery.inspect(run.job_id, run.owner, run.fence)
                if evidence.action not in {"resume_candidate", "retry_local_call"}:
                    raise JourneyError("TEMPORARILY_UNAVAILABLE")
            binding = call_binding(run.job)
            planned = run.job["planned_candidate_ref"]
            raw = self.storage.load_calculation(planned, binding)
            if raw is None:
                if run.job["call_phase"] == "candidate_saved" or run.job.get("candidate_ref") is not None:
                    raise JourneyError("STORED_INPUT_INVALID")
                previous_call_id = run.job["call_id"]
            else:
                candidate_ref = {**planned, "sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw)}
                run.refresh(self.jobs.mark_calculation_saved(run.job_id, run.owner, run.fence, candidate_ref))
        if raw is None:
            return self._calculate(run, adapter, loaded, heartbeat, previous_call_id)
        return raw

    def _calculate(self, run, adapter, loaded, heartbeat, previous_call_id):
        from mock_journey.jobs import CALCULATION_RESTART_LIMIT, calculation_restarts

        if getattr(adapter, "can_calculate", True) is not True:
            # Historical candidate readers must never start a new
            # call using changed detection semantics.
            raise JourneyError("TEMPORARILY_UNAVAILABLE")
        if previous_call_id is not None and calculation_restarts(run.job) >= CALCULATION_RESTART_LIMIT:
            # Close after the lease guard stops; begin_calculation
            # would refuse this restart in its own CAS as well.
            raise _RestartLimitReached()
        heartbeat()
        binding = {**input_binding(run.job), "job_id": run.job_id, "call_id": str(uuid.uuid4())}
        planned = self.storage.planned_calculation(binding)
        permitted, job = self.jobs.begin_calculation(
            run.job_id, run.owner, run.fence, binding["call_id"], planned,
            previous_call_id=previous_call_id,
        )
        run.refresh(job)
        if not permitted:
            return _REFUSED
        # This runs the local calculator. Configuration/storage errors
        # preserve the durable job for a later lease; domain failures
        # are classified by the calculator and handled below.
        run.calculating = True
        raw = adapter.calculate(loaded, binding, heartbeat)
        run.calculating = False
        heartbeat()
        candidate_ref = self.storage.save_calculation(planned, raw, binding)
        run.refresh(self.jobs.mark_calculation_saved(run.job_id, run.owner, run.fence, candidate_ref))
        return raw

    @staticmethod
    def _verify(adapter, raw, projected, binding):
        try:
            verified = adapter.validate_response(raw, projected, binding)
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
            raise JourneyError("CALCULATOR_CONTRACT_MISMATCH") from None
        if not isinstance(verified, VerifiedCalculation):
            raise JourneyError("CALCULATOR_CONTRACT_MISMATCH")
        return verified

    def _select_and_publish_chart(self, run, adapter, verified, loaded, binding, heartbeat):
        """Pin one chart selection unless one is already pinned, then publish the pinned one."""
        current = self.jobs.get_job(run.job_id)
        if current["chart_snapshot"]["kind"] == "unset":
            if verified.chart_kind == "no_chart":
                selection = {"kind": "no_chart", **binding}
            else:
                chart = adapter.get_chart(verified, binding, heartbeat)
                if not isinstance(chart, VerifiedChart):
                    raise JourneyError("CALCULATOR_CONTRACT_MISMATCH")
                selection = self.storage.save_chart_candidate(chart.data, chart.source_sha256, binding, run.fence)
            self.jobs.pin_chart(run.job_id, run.owner, run.fence, selection)
        return self.storage.publish_selected_chart(
            run.job_id, loaded.raw_base, binding,
            lambda ident: self.jobs.get_job(ident)["chart_snapshot"],
        )

    def _assemble_result(self, verified, projected, publication):
        """The legacy response bytes (signed chart URL included) and the evaluation."""
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
        return response_bytes, evaluation

    # -- after the lease guard -----------------------------------------------

    def _final_renew(self, renew):
        # Quiesce the optional renewer and surface latent failures before
        # the final ownership CAS. It must not race a completed job.
        if self.lease_guard_factory is not None:
            final_renew = getattr(self.lease_guard_factory, "final_renew", None)
            if final_renew is None:
                renew()
            else:
                final_renew(renew)

    def _commit(self, run, final_ref, evaluation, publication):
        # completion_plan is passed only when configured (test doubles take no keyword).
        options = {} if self.completion_plan is None else {"completion_plan": self.completion_plan}
        finalized = self.jobs.finalize(run.job_id, run.owner, run.fence, final_ref, evaluation, publication, **options)
        record_event("calculation_completed", state="evaluated")
        try:
            progress = finalized["progress_application"]
            record_event("progress_application", reason=progress["reason"], applied=progress["applied"],
                         program_completed=finalized["evaluation"]["program_completed"])
        except Exception:
            pass  # Logging metadata never overturns the committed result.
        return True

    # -- classification ----------------------------------------------------------

    def _on_journey_error(self, run, error):
        from mock_journey.jobs import JobLeaseLost

        if run.is_course:
            try:
                proven = run.calculating and error.code in _LOCAL_FAILURE_CODES
                self.jobs.defer_course_recovery(run.job_id, run.owner, run.fence, error.code,
                    next_due_at=int(self.clock()) + self.retry_seconds, proven_local_error=proven)
                if proven:
                    evidence = self.course_recovery.inspect(run.job_id, run.owner, run.fence)
                    if evidence.action == "close_terminal":
                        self.jobs.close_course_terminal(run.job_id, run.owner, run.fence, evidence)
                        return True
            except (JobLeaseLost, JourneyError, CourseError):
                pass
            record_event("calculation_deferred", error_code=error.code)
            return False
        if error.code in _LOCAL_FAILURE_CODES:
            try:
                self.jobs.mark_failed(run.job_id, run.owner, run.fence, error.code)
                record_event("calculation_failed", error_code=error.code, state="failed")
                return True
            except (JobLeaseLost, JourneyError):
                return False
        record_event("calculation_deferred", error_code=error.code)
        return False

    def _on_unexpected_error(self, run, error):
        # Retry only after a new lease. Recovery first checks the stored
        # candidate; a missing candidate gets a new fenced execution path.
        from mock_journey.jobs import JobLeaseLost

        write_diagnostic("error", "request_failed", {"exception": error})
        if run.is_course:
            try:
                self.jobs.defer_course_recovery(run.job_id, run.owner, run.fence, "TEMPORARILY_UNAVAILABLE",
                                                next_due_at=int(self.clock()) + self.retry_seconds)
            except (JobLeaseLost, JourneyError, CourseError):
                pass
        record_event("calculation_deferred")
        return False

    def _close_restart_limit(self, run):
        """Close as CALCULATION_FAILED under the same lease; any refusal only defers."""
        from mock_journey.jobs import RESTART_LIMIT_BASIS, JobLeaseLost, calculation_restarts
        try:
            if run.is_course:
                # The seal re-verifies this fresh proof that a restart is the next step.
                evidence = self.course_recovery.inspect(run.job_id, run.owner, run.fence)
                if evidence.action != "retry_local_call":
                    raise JourneyError("INVALID_STATE")
                self.jobs.close_course_restart_limit(run.job_id, run.owner, run.fence, evidence)
            else:
                self.jobs.close_restart_limit(run.job_id, run.owner, run.fence)
        except (JobLeaseLost, JourneyError, CourseError) as error:
            record_event("calculation_deferred", error_code=getattr(error, "code", None))
            return False
        # The stored counter of the snapshot that reached the limit (_calculate read
        # the same row); begin_calculation never issues a restart past the limit,
        # so today it is exactly CALCULATION_RESTART_LIMIT.
        record_event("calculation_failed", error_code="CALCULATION_FAILED", state="failed",
                     failure_basis=RESTART_LIMIT_BASIS, calculation_restarts=calculation_restarts(run.job))
        return True


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

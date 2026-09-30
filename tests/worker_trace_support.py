"""Worker external-call-order characterization (S5-04). Never imported by runtime code.

``JourneyWorker.process`` talks to the outside only through ``jobs``,
``storage``, the resolved calculator adapter, the optional course recovery
service, the optional lease guard factory and the operational recorder. Two
harnesses record every one of those calls, in order, so the decomposition of
``_process`` into step methods can be checked against a baseline captured
before it (tests/fixtures/worker_call_order/):

1. Spy harness (``SPY_SCENARIOS``/``run_spy``): scripted doubles record each
   call with its exact positional/keyword shape. Faults are injected by call
   name and ordinal, so every branch of the error classification runs.
2. Memory harness (``MEMORY_SCENARIOS``/``run_memory``): the real
   repositories, storage and internal calculator on tests/memory_dynamodb.py
   and MemoryS3. The trace is MemoryDynamoDB's own request record (reads and
   writes) merged with S3 operations, adapter calls and operational records
   in the order they happened, for the ``worker.process`` calls only.

Run-dependent values are canonicalized by first appearance (``canonical``):
UUIDs become ``<uuid:N>``, 64/32 lowercase hex strings ``<h:N>``/``<hex:N>``,
``resume_nonce`` values ``<secret:N>``, legacy key-stem clocks
``CPR-ACTION-<wallclock>`` and key dates ``<date>``. Equal values keep equal
labels, so the baseline still fixes which owner, fence, call id or reference
flows into which call. Diagnostic ``*_ms`` durations become ``<ms>``.
Diagnostic stack traces keep file and function names but not line numbers,
and consecutive mock_journey/worker.py frames collapse to one entry: moving
code inside the worker changes those frames by design, while the frames below
it (the failing callee) stay fixed.
"""

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import re
from types import SimpleNamespace

from mock_journey.contracts import VerifiedCalculation, VerifiedChart
from mock_journey.course_errors import CourseError
from mock_journey.errors import JourneyError
from mock_journey.jobs import CALCULATION_RESTART_LIMIT, JobLeaseLost


# ---------------------------------------------------------------------------
# Canonical form
# ---------------------------------------------------------------------------

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_TOKENS = re.compile(
    rf"(?P<uuid>{_UUID})"
    r"|(?P<h>(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f]))"
    r"|(?P<hex>(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f]))"
    r"|(?P<stem>CPR-ACTION-[0-9]{10})"
    r"|(?P<date>(?<=/)[0-9]{4}-[0-9]{2}-[0-9]{2}(?=/))"
)
WORKER_FILE = "mock_journey/worker.py"
# Random per-attempt values that are not UUID/hex shaped (base64url nonces).
_SECRET_KEYS = frozenset({"resume_nonce"})


class _Labels:
    def __init__(self):
        self.labels, self.counts = {}, {}

    def _replace(self, match):
        kind = match.lastgroup
        if kind == "stem":
            return "CPR-ACTION-<wallclock>"
        if kind == "date":
            return "<date>"
        return self._label(kind, match.group(kind))

    def _label(self, kind, text):
        key = (kind, text)
        if key not in self.labels:
            self.counts[kind] = self.counts.get(kind, 0) + 1
            self.labels[key] = f"<{kind}:{self.counts[kind]}>"
        return self.labels[key]

    def _secret(self, value):
        if type(value) is str:
            return self._label("secret", value)
        if type(value) is dict and set(value) == {"S"} and type(value["S"]) is str:
            return {"S": self._label("secret", value["S"])}  # a DynamoDB-typed attribute
        return self.value(value)

    def value(self, value):
        if type(value) is str:
            return _TOKENS.sub(self._replace, value)
        if type(value) is dict:
            return {self.value(key): self._secret(item) if key in _SECRET_KEYS else self.value(item)
                    for key, item in value.items()}
        if type(value) in (list, tuple):
            return [self.value(item) for item in value]
        return value


def canonical(value):
    """Relabel run-dependent strings by first appearance (JSON traversal order)."""
    return _Labels().value(value)


def stack_shape(frames):
    """``path:line in function`` frames -> ``path in function``; worker frames collapse to one entry."""
    shaped = []
    for frame in frames:
        location, _, function = frame.partition(" in ")
        path = location.rsplit(":", 1)[0]
        if path == WORKER_FILE:
            if not shaped or shaped[-1] != WORKER_FILE:
                shaped.append(WORKER_FILE)
            continue
        shaped.append(f"{path} in {function}" if function else frame)
    return shaped


def log_fields(category, fields):
    """Diagnostic durations become ``<ms>`` (wall time); stack traces keep their shape only."""
    fields = dict(fields)
    if category == "diagnostic":
        if type(fields.get("stacktrace")) is list:
            fields["stacktrace"] = stack_shape(fields["stacktrace"])
        for key, value in fields.items():
            if key.endswith("_ms") and type(value) is int and value >= 0:
                fields[key] = "<ms>"
    return fields


def first_difference(expected, observed):
    """A short description of the first differing trace entry (for assertion messages)."""
    for index, (left, right) in enumerate(zip(expected, observed)):
        if left != right:
            return f"entry {index}: expected {json.dumps(left)[:600]} observed {json.dumps(right)[:600]}"
    return f"lengths differ: expected {len(expected)} observed {len(observed)}"


# ---------------------------------------------------------------------------
# 1. Spy harness
# ---------------------------------------------------------------------------

JOB_ID = "8d7f2a3c-1b4e-4f5a-9c6d-7e8f9a0b1c2d"
ATTEMPT_ID = "2f0e6c1a-7b3d-4e5f-8a9b-0c1d2e3f4a5b"
EPOCH = "5a4b3c2d-1e0f-4a1b-9c2d-3e4f5a6b7c8d"
OLD_CALL_ID = "0b1c2d3e-4f5a-4b6c-8d7e-9f0a1b2c3d4e"
RAW = b'{"spy":"raw candidate"}'
STORED_CANDIDATE = b'{"spy":"stored candidate"}'
CLOCK = 1_000
LEASE_SECONDS, RETRY_SECONDS = 60, 5
COMPLETION_PLAN = SimpleNamespace(name="spy-completion-plan")
CORE = {"cpr_score": {"total_score": {"overall": 90, "score_rescue_vent": None}},
        "metrics": {"VentilationSpeed": {"%_Good": 100}}, "action_count": {"comp": 60, "vent": 0},
        "training_stats": {"cycle_count": 3, "elapsed_seconds": 90}, "guide_prompts": ["spy coaching"],
        "type_preservation": [80, 80.0, None, False, "80"]}
CONDITION = {"mode": "training", "target": "adult", "training_type": "compression_only",
             "cpr_cycle_type": "302", "is_2rescuers": False, "guideline": "ARC2025"}
PAYLOAD = {
    "calculation_input": {"condition": CONDITION, "vp_event_list": []},
    "response_context": {"Custom": None},
    "document_context": {"document_source": "none", "document": None},
    "definition": {"condition": CONDITION, "calculation_profile": {}, "goal": {"kind": "compressions", "required": 60},
                   "catalog_version": "spy-catalog", "profile_version": "spy-profile",
                   "adapter_version": "spy-adapter-v1", "projection_version": "spy-projection-v1"},
}


def _error(spec):
    kind, *detail = spec
    if kind == "JourneyError":
        return JourneyError(*detail)
    if kind == "CourseError":
        return CourseError(*detail)
    if kind == "JobLeaseLost":
        return JobLeaseLost()
    return {"OSError": OSError, "ValueError": ValueError, "KeyError": KeyError, "TypeError": TypeError,
            "TimeoutError": TimeoutError}[kind]("PRIVATE-SPY-MARKER")


class _Evidence(SimpleNamespace):
    pass


def describe(value):
    """A JSON form of one call argument; opaque objects become fixed markers."""
    if value is None or type(value) in (bool, int, float, str):
        return value
    if type(value) is bytes:
        try:
            return {"utf8": value.decode("utf-8")}
        except UnicodeDecodeError:
            return {"sha256": hashlib.sha256(value).hexdigest(), "size": len(value)}
    if type(value) is dict:
        return {key: describe(item) for key, item in value.items()}
    if type(value) in (list, tuple):
        return [describe(item) for item in value]
    if value is COMPLETION_PLAN:
        return "<completion_plan>"
    if type(value) is _Evidence:
        return {"evidence": value.action, "serial": value.serial}
    if type(value) is VerifiedCalculation:
        return {"VerifiedCalculation": {"goal_kind": value.goal_kind, "observed": value.observed,
                                        "chart_kind": value.chart_kind, "chart_reference": value.chart_reference,
                                        "goal_status": value.goal_status,
                                        "core": describe(json.dumps(value.core_result).encode())}}
    if type(value) is VerifiedChart:
        return {"VerifiedChart": {"data": describe(value.data), "source_sha256": value.source_sha256}}
    if type(value) is SimpleNamespace and hasattr(value, "projected"):
        return "<loaded>"
    if type(value) is SimpleNamespace and hasattr(value, "payload"):
        return "<projected>"
    if callable(value):
        return "<callable>"
    return f"<{type(value).__name__}>"


class Trace:
    """One ordered record of every external call; faults are keyed by (call name, ordinal)."""

    def __init__(self, faults=None):
        self.entries = []
        self.faults = dict(faults or {})
        self.counts = {}

    def call(self, name, *args, **kwargs):
        entry = {"call": name, "args": [describe(arg) for arg in args]}
        if kwargs:
            entry["kwargs"] = {key: describe(value) for key, value in kwargs.items()}
        self.entries.append(entry)
        number = self.counts[name] = self.counts.get(name, 0) + 1
        spec = self.faults.get((name, number))
        if spec is not None:
            raise _error(spec)


class SpyJobs:
    """A scripted job row; not the repository's rules (those are tested on DynamoDB)."""

    def __init__(self, trace, config):
        self.trace, self.config = trace, config
        self.job = spy_job(config)

    def _call(self, name, args, kwargs):
        self.trace.call("jobs." + name, *args, **kwargs)

    def is_course_job(self, *args, **kwargs):
        self._call("is_course_job", args, kwargs)
        return self.config.get("course", False)

    def get_job(self, *args, **kwargs):
        self._call("get_job", args, kwargs)
        return deepcopy(self.job)

    def claim(self, *args, **kwargs):
        self._call("claim", args, kwargs)
        forced = self.config.get("claim")
        if forced is not None:
            return forced, deepcopy(self.job)
        action = "execute" if self.job["call_phase"] == "not_started" else "recover"
        self.job = {**self.job, "state": "running", "owner": args[1], "fence": self.job["fence"] + 1}
        return action, deepcopy(self.job)

    def renew_lease(self, *args, **kwargs):
        self._call("renew_lease", args, kwargs)

    def begin_calculation(self, *args, **kwargs):
        self._call("begin_calculation", args, kwargs)
        if not self.config.get("permitted", True):
            return False, deepcopy(self.job)
        _, _, _, call_id, planned = args
        updated = {**self.job, "call_id": call_id, "planned_candidate_ref": planned, "call_phase": "started",
                   "candidate_ref": None, "execution_fence": self.job["fence"]}
        if kwargs.get("previous_call_id") is not None:
            updated["calculation_restarts"] = self.job.get("calculation_restarts", 0) + 1
        self.job = updated
        return True, deepcopy(self.job)

    def mark_calculation_saved(self, *args, **kwargs):
        self._call("mark_calculation_saved", args, kwargs)
        self.job = {**self.job, "call_phase": "candidate_saved", "candidate_ref": args[3]}
        return deepcopy(self.job)

    def pin_chart(self, *args, **kwargs):
        self._call("pin_chart", args, kwargs)
        self.job = {**self.job, "chart_snapshot": {**args[3], "revision": self.job["chart_snapshot"]["revision"] + 1}}
        return deepcopy(self.job["chart_snapshot"])

    def finalize(self, *args, **kwargs):
        self._call("finalize", args, kwargs)
        if "finalized" in self.config:
            return deepcopy(self.config["finalized"])
        return {"evaluation": deepcopy(args[4]),
                "progress_application": {"applied": True, "applied_epoch": EPOCH, "reason": "APPLIED"}}

    def mark_failed(self, *args, **kwargs):
        self._call("mark_failed", args, kwargs)

    def defer_course_recovery(self, *args, **kwargs):
        self._call("defer_course_recovery", args, kwargs)

    def reopen_course_recovery(self, *args, **kwargs):
        self._call("reopen_course_recovery", args, kwargs)

    def close_course_terminal(self, *args, **kwargs):
        self._call("close_course_terminal", args, kwargs)

    def close_restart_limit(self, *args, **kwargs):
        self._call("close_restart_limit", args, kwargs)

    def close_course_restart_limit(self, *args, **kwargs):
        self._call("close_course_restart_limit", args, kwargs)


class SpyJobsWithoutCourseProbe(SpyJobs):
    is_course_job = None  # getattr(..., "is_course_job", None) is not callable: a legacy-only repository.


def spy_job(config):
    job = {
        "job_id": JOB_ID, "attempt_id": ATTEMPT_ID, "epoch": EPOCH, "input_digest": "d" * 64,
        "adapter_version": "spy-adapter-v1", "projection_version": "spy-projection-v1",
        "input_manifest_ref": {"bucket": "spy-bucket", "key": "input/manifest.request.json",
                               "sha256": "e" * 64, "size": 10},
        "state": "queued", "call_phase": "not_started", "call_id": None, "execution_fence": 0,
        "planned_candidate_ref": None, "candidate_ref": None, "chart_snapshot": {"kind": "unset", "revision": 0},
        "fence": 3, "owner": None, "lease_until": 0, "revision": 7,
    }
    if config.get("recover"):
        job.update(state="running", call_phase="started", call_id=OLD_CALL_ID, execution_fence=3,
                   planned_candidate_ref={"bucket": "spy-bucket", "key": "calc/old-call.json"})
    job.update(deepcopy(config.get("job", {})))
    return job


class SpyStorage:
    def __init__(self, trace, config):
        self.trace, self.config = trace, config

    def _call(self, name, args, kwargs):
        self.trace.call("storage." + name, *args, **kwargs)

    def load_input(self, *args, **kwargs):
        self._call("load_input", args, kwargs)
        payload = deepcopy(PAYLOAD)
        for path, value in self.config.get("payload", {}).items():
            target = payload
            *parents, last = path.split(".")
            for parent in parents:
                target = target[parent]
            if value is ...:
                del target[last]
            else:
                target[last] = value
        return SimpleNamespace(projected=SimpleNamespace(payload=payload), raw_base="spy/raw/base")

    def load_calculation(self, *args, **kwargs):
        self._call("load_calculation", args, kwargs)
        return self.config.get("candidate")

    def planned_calculation(self, *args, **kwargs):
        self._call("planned_calculation", args, kwargs)
        return {"bucket": "spy-bucket", "key": "calc/" + args[0]["call_id"] + ".json"}

    def save_calculation(self, *args, **kwargs):
        self._call("save_calculation", args, kwargs)
        planned, raw, _ = args
        return {**planned, "sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw)}

    def save_chart_candidate(self, *args, **kwargs):
        self._call("save_chart_candidate", args, kwargs)
        _, source_sha256, binding, _ = args
        return {"kind": "snapshot", "bucket": "spy-bucket", "key": "chart/candidate.json",
                "source_sha256": source_sha256, **binding}

    def publish_selected_chart(self, *args, **kwargs):
        self._call("publish_selected_chart", args, kwargs)
        job_id, _, binding, read_snapshot = args
        snapshot = read_snapshot(job_id)
        return {"kind": snapshot["kind"], "selection_revision": snapshot["revision"],
                "key": "chart/published.json" if snapshot["kind"] == "snapshot" else None, **binding}

    def sign_chart(self, *args, **kwargs):
        self._call("sign_chart", args, kwargs)
        return None if args[0]["key"] is None else "https://spy.invalid/" + args[0]["key"]

    def save_final(self, *args, **kwargs):
        self._call("save_final", args, kwargs)
        return {"bucket": "spy-bucket", "key": "final/result.json",
                "sha256": hashlib.sha256(args[0]).hexdigest(), "size": len(args[0])}


class SpyAdapter:
    def __init__(self, trace, config):
        self.trace, self.config = trace, config

    @property
    def can_calculate(self):
        self.trace.call("adapter.can_calculate")
        value = self.config.get("can_calculate", True)
        if value is ...:
            raise AttributeError("can_calculate")  # getattr(..., True) then uses its default.
        return value

    def calculate(self, *args, **kwargs):
        self.trace.call("adapter.calculate", *args, **kwargs)
        for _ in range(self.config.get("calculate_heartbeats", 1)):
            args[2]()
        self.trace.call("adapter.calculate.return")
        return RAW

    def validate_response(self, *args, **kwargs):
        self.trace.call("adapter.validate_response", *args, **kwargs)
        verified = self.config.get("verified", "ok")
        if verified == "junk":
            return {"not": "verified"}
        chart = self.config.get("chart_kind", "no_chart")
        return VerifiedCalculation(deepcopy(CORE), "compressions", self.config.get("observed", 60), chart,
                                   "spy-chart-reference" if chart == "snapshot" else None)

    def get_chart(self, *args, **kwargs):
        self.trace.call("adapter.get_chart", *args, **kwargs)
        args[2]()
        if self.config.get("chart", "ok") == "junk":
            return {"not": "a chart"}
        data = {"values": [1, 1.0, None]}
        return VerifiedChart(data, hashlib.sha256(json.dumps(data).encode()).hexdigest())


class SpyAdapters:
    def __init__(self, trace, config):
        self.trace, self.adapter = trace, SpyAdapter(trace, config)

    def resolve(self, *args, **kwargs):
        self.trace.call("adapters.resolve", *args, **kwargs)
        return self.adapter


class SpyRecovery:
    def __init__(self, trace, actions):
        self.trace, self.actions, self.serial = trace, list(actions), 0

    def inspect(self, *args, **kwargs):
        self.trace.call("recovery.inspect", *args, **kwargs)
        self.serial += 1
        return _Evidence(action=self.actions.pop(0), serial=self.serial)


class _SpyGuard:
    def __init__(self, factory, heartbeat):
        self.factory, self.heartbeat = factory, heartbeat

    def __enter__(self):
        self.factory.trace.call("guard.enter")
        if self.factory.mode == "not_callable":
            return "not-callable"
        trace, heartbeat = self.factory.trace, self.heartbeat

        def guarded():
            trace.call("guard.heartbeat")
            heartbeat()
        return guarded

    def __exit__(self, exc_type, exc, traceback):
        self.factory.trace.call("guard.exit", None if exc_type is None else exc_type.__name__)
        return self.factory.mode == "suppress"


class SpyGuardFactory:
    """A lease guard double: its own heartbeat wrapper, a scripted exit, no thread."""

    def __init__(self, trace, mode):
        self.trace, self.mode = trace, mode

    def __call__(self, *args, **kwargs):
        self.trace.call("guard.factory", *args, **kwargs)
        return _SpyGuard(self, args[0])


class SpyGuardFactoryWithFinalRenew(SpyGuardFactory):
    def final_renew(self, *args, **kwargs):
        self.trace.call("guard.final_renew", *args, **kwargs)
        args[0]()


class SpyOperations:
    def __init__(self, trace):
        self.trace = trace

    def record(self, category, event, fields):
        self.trace.entries.append({"log": [category, event, log_fields(category, fields)]})
        return True


def _restart(course=False, **extra):
    config = {"recover": True, "job": {"calculation_restarts": CALCULATION_RESTART_LIMIT}, **extra}
    if course:
        config.update(course=True, completion_plan=True)
    return config


def _course(**extra):
    return {"course": True, "completion_plan": True, "guard": "final_renew", **extra}


# Each scenario is scripted data; the baseline holds the recorded outcome.
SPY_SCENARIOS = {
    # -- legacy (not a course job) -------------------------------------------
    "legacy_execute_no_chart": {},
    "legacy_execute_no_course_probe": {"jobs": "no_course_probe"},
    "legacy_execute_snapshot_guard_final_renew": {"guard": "final_renew", "chart_kind": "snapshot"},
    "legacy_execute_snapshot_guard_plain": {"guard": "plain", "chart_kind": "snapshot"},
    "legacy_execute_completion_plan": {"completion_plan": True, "calculate_heartbeats": 3},
    "legacy_chart_already_pinned": {"chart_kind": "snapshot", "job": {"chart_snapshot": {"kind": "snapshot",
                                                                                            "revision": 2}}},
    "legacy_can_calculate_absent": {"can_calculate": ...},
    "legacy_claim_busy": {"claim": "busy"},
    "legacy_claim_done": {"claim": "done"},
    "legacy_claim_raises": {"faults": {("jobs.claim", 1): ("OSError",)}},
    "legacy_probe_raises": {"faults": {("jobs.is_course_job", 1): ("JourneyError", "TEMPORARILY_UNAVAILABLE")}},
    "legacy_recover_candidate": {"recover": True, "candidate": STORED_CANDIDATE, "guard": "final_renew"},
    "legacy_recover_restart": {"recover": True, "guard": "final_renew"},
    "legacy_recover_saved_without_candidate": {"recover": True, "job": {"call_phase": "candidate_saved"}},
    "legacy_recover_candidate_ref_without_candidate": {
        "recover": True, "job": {"candidate_ref": {"bucket": "spy-bucket", "key": "calc/old-call.json"}}},
    "legacy_restart_limit": _restart(),
    "legacy_restart_limit_guard": _restart(guard="final_renew"),
    "legacy_restart_limit_refused_lease": _restart(faults={("jobs.close_restart_limit", 1): ("JobLeaseLost",)}),
    "legacy_restart_limit_refused_state": _restart(
        faults={("jobs.close_restart_limit", 1): ("JourneyError", "INVALID_STATE")}),
    "legacy_restart_limit_candidate_resumes": _restart(candidate=STORED_CANDIDATE),
    "legacy_begin_refused": {"permitted": False, "guard": "final_renew"},
    "legacy_cannot_calculate": {"can_calculate": False},
    "legacy_resolve_unavailable": {"faults": {("adapters.resolve", 1): ("JourneyError", "TEMPORARILY_UNAVAILABLE")}},
    "legacy_load_input_invalid": {"faults": {("storage.load_input", 1): ("JourneyError", "STORED_INPUT_INVALID")}},
    "legacy_calculate_failed": {"faults": {("adapter.calculate", 1): ("JourneyError", "CALCULATION_FAILED")}},
    "legacy_calculate_timeout": {"guard": "final_renew", "faults": {("adapter.calculate", 1): ("TimeoutError",)}},
    "legacy_validate_value_error": {"faults": {("adapter.validate_response", 1): ("ValueError",)}},
    "legacy_validate_not_verified": {"verified": "junk"},
    "legacy_chart_not_verified": {"chart_kind": "snapshot", "chart": "junk"},
    "legacy_response_assembly_fails": {"payload": {"definition.condition": ...}},
    "legacy_goal_kind_mismatch": {"payload": {"definition.goal": {"kind": "cycles", "required": 3}}},
    "legacy_mark_failed_lease_lost": {"verified": "junk",
                                      "faults": {("jobs.mark_failed", 1): ("JobLeaseLost",)}},
    "legacy_mark_failed_refused": {"verified": "junk",
                                   "faults": {("jobs.mark_failed", 1): ("JourneyError", "INVALID_STATE")}},
    "legacy_heartbeat_lease_lost": {"guard": "final_renew", "faults": {("jobs.renew_lease", 2): ("JobLeaseLost",)}},
    "legacy_save_final_os_error": {"faults": {("storage.save_final", 1): ("OSError",)}},
    "legacy_publish_unavailable": {"faults": {("storage.publish_selected_chart", 1):
                                              ("JourneyError", "TEMPORARILY_UNAVAILABLE")}},
    "legacy_guard_factory_raises": {"guard": "final_renew", "faults": {("guard.factory", 1): ("OSError",)}},
    "legacy_guard_enter_lease_lost": {"guard": "final_renew", "faults": {("guard.enter", 1): ("JobLeaseLost",)}},
    "legacy_guard_not_callable": {"guard": "not_callable"},
    "legacy_guard_exit_lease_lost": {"guard": "final_renew", "faults": {("guard.exit", 1): ("JobLeaseLost",)}},
    "legacy_guard_exit_replaces_error": {"guard": "final_renew", "faults": {
        ("storage.save_final", 1): ("OSError",), ("guard.exit", 1): ("JobLeaseLost",)}},
    "legacy_guard_suppresses_error": {"guard": "suppress", "faults": {("storage.save_final", 1): ("OSError",)}},
    "legacy_guard_heartbeat_lease_lost": {"guard": "final_renew",
                                          "faults": {("guard.heartbeat", 1): ("JobLeaseLost",)}},
    "legacy_final_renew_lease_lost": {"guard": "final_renew", "faults": {("guard.final_renew", 1): ("JobLeaseLost",)}},
    "legacy_final_plain_renew_lease_lost": {"guard": "plain", "faults": {("jobs.renew_lease", 6): ("JobLeaseLost",)}},
    "legacy_finalize_unavailable": {"faults": {("jobs.finalize", 1): ("JourneyError", "TEMPORARILY_UNAVAILABLE")}},
    "legacy_finalize_lease_lost": {"faults": {("jobs.finalize", 1): ("JobLeaseLost",)}},
    "legacy_finalize_without_progress": {"finalized": {"evaluation": {"program_completed": True}}},
    "legacy_finalize_returns_none": {"finalized": None},
    "legacy_course_error_in_body": {"faults": {("storage.load_calculation", 1): ("CourseError", "NOT_FOUND")},
                                    "recover": True},
    # -- course jobs -----------------------------------------------------------
    "course_without_completion_plan": {"course": True},
    "course_without_recovery_service": {"course": True, "completion_plan": True, "recovery": False},
    "course_sealed": _course(job={"terminal_seal": {"basis": "calculation_restart_limit"}}),
    "course_failed_reopened": _course(job={"state": "failed"}, inspect=["resume_candidate"], chart_kind="snapshot"),
    "course_failed_not_resumable": _course(job={"state": "failed"}, inspect=["close_terminal"]),
    "course_failed_inspect_course_error": _course(job={"state": "failed"}, inspect=["resume_candidate"],
                                                  faults={("recovery.inspect", 1): ("CourseError", "NOT_FOUND")}),
    "course_failed_reopen_refused": _course(job={"state": "failed"}, inspect=["resume_candidate"], faults={
        ("jobs.reopen_course_recovery", 1): ("JourneyError", "INVALID_STATE")}),
    "course_failed_get_job_raises": _course(faults={("jobs.get_job", 1): ("OSError",)}),
    "course_execute_snapshot": _course(chart_kind="snapshot"),
    "course_execute_no_guard": _course(guard=None),
    "course_claim_busy": _course(claim="busy"),
    "course_recover_retry_call": _course(recover=True, inspect=["retry_local_call"]),
    "course_recover_candidate": _course(recover=True, inspect=["resume_candidate"], candidate=STORED_CANDIDATE),
    "course_recover_waits": _course(recover=True, inspect=["wait_integrity"]),
    "course_recover_inspect_course_error": _course(recover=True, inspect=["resume_candidate"], faults={
        ("recovery.inspect", 1): ("CourseError", "TEMPORARILY_UNAVAILABLE")}),
    "course_restart_limit": _restart(course=True, guard="final_renew",
                                     inspect=["retry_local_call", "retry_local_call"]),
    "course_restart_limit_not_retry": _restart(course=True, inspect=["retry_local_call", "wait_integrity"]),
    "course_restart_limit_refused": _restart(course=True, inspect=["retry_local_call", "retry_local_call"], faults={
        ("jobs.close_course_restart_limit", 1): ("CourseError", "TEMPORARILY_UNAVAILABLE")}),
    "course_proven_failure_closed": _course(inspect=["close_terminal"], faults={
        ("adapter.calculate", 1): ("JourneyError", "CALCULATION_FAILED")}),
    "course_proven_contract_mismatch_waits": _course(inspect=["wait_integrity"], faults={
        ("adapter.calculate", 1): ("JourneyError", "CALCULATOR_CONTRACT_MISMATCH")}),
    "course_proven_close_refused": _course(inspect=["close_terminal"], faults={
        ("adapter.calculate", 1): ("JourneyError", "STORED_INPUT_INVALID"),
        ("jobs.close_course_terminal", 1): ("JourneyError", "INVALID_STATE")}),
    "course_proven_defer_lease_lost": _course(inspect=[], faults={
        ("adapter.calculate", 1): ("JourneyError", "CALCULATION_FAILED"),
        ("jobs.defer_course_recovery", 1): ("JobLeaseLost",)}),
    "course_proven_inspect_course_error": _course(inspect=["close_terminal"], faults={
        ("adapter.calculate", 1): ("JourneyError", "CALCULATION_FAILED"),
        ("recovery.inspect", 1): ("CourseError", "TEMPORARILY_UNAVAILABLE")}),
    "course_unproven_after_calculation": _course(verified="junk"),
    "course_unproven_before_calculation": _course(faults={
        ("storage.load_input", 1): ("JourneyError", "STORED_INPUT_INVALID")}),
    "course_journey_unavailable": _course(faults={
        ("storage.planned_calculation", 1): ("JourneyError", "TEMPORARILY_UNAVAILABLE")}),
    "course_unexpected_error": _course(faults={("adapter.calculate", 1): ("TimeoutError",)}),
    "course_unexpected_defer_refused": _course(faults={
        ("storage.save_calculation", 1): ("OSError",),
        ("jobs.defer_course_recovery", 1): ("CourseError", "TEMPORARILY_UNAVAILABLE")}),
    "course_lease_lost": _course(faults={("guard.heartbeat", 2): ("JobLeaseLost",)}),
    "course_finalize_unavailable": _course(faults={("jobs.finalize", 1): ("JourneyError", "TEMPORARILY_UNAVAILABLE")}),
}


def run_spy(name):
    """Run one scripted scenario on the current JourneyWorker; return its canonical outcome."""
    from mock_journey.worker import JourneyWorker

    config = SPY_SCENARIOS[name]
    trace = Trace(config.get("faults"))
    jobs = (SpyJobsWithoutCourseProbe if config.get("jobs") == "no_course_probe" else SpyJobs)(trace, config)
    guard = {None: None, "final_renew": SpyGuardFactoryWithFinalRenew, "plain": SpyGuardFactory,
             "not_callable": SpyGuardFactoryWithFinalRenew, "suppress": SpyGuardFactoryWithFinalRenew}[
        config.get("guard")]
    worker = JourneyWorker(jobs, SpyStorage(trace, config), SpyAdapters(trace, config),
                           lease_seconds=LEASE_SECONDS, retry_seconds=RETRY_SECONDS, clock=lambda: CLOCK,
                           lease_guard_factory=None if guard is None else guard(trace, config["guard"]),
                           operations=SpyOperations(trace),
                           completion_plan=COMPLETION_PLAN if config.get("completion_plan") else None)
    if config.get("course") and config.get("completion_plan") and config.get("recovery", True):
        worker.course_recovery = SpyRecovery(trace, config.get("inspect", []))
    try:
        outcome = {"returned": worker.process(JOB_ID)}
    except Exception as error:  # noqa: BLE001 - the propagated type is part of the baseline.
        outcome = {"raised": type(error).__name__}
    return canonical({**outcome, "trace": trace.entries})


# ---------------------------------------------------------------------------
# 2. Memory harness (real repositories, storage and calculator)
# ---------------------------------------------------------------------------

from tests.calculator_doubles import ProcessKilled  # noqa: E402 (a killed process: nothing runs after the call)


class _Sink:
    """Merge S3/adapter/log records into MemoryDynamoDB's own request record by position."""

    def __init__(self):
        self.client = None
        self.others = []
        self.active = False

    def other(self, entry):
        if self.active:
            self.others.append((len(self.client.calls), entry))

    @contextmanager
    def window(self, runs, label):
        start = len(self.client.calls)
        self.others, self.active = [], True
        run = {"run": label}
        try:
            try:
                yield run
            finally:
                self.active = False
        except ProcessKilled:
            run["raised"] = "ProcessKilled"
        end = len(self.client.calls)
        pending = list(self.others)
        trace = []
        for index in range(start, end):
            while pending and pending[0][0] <= index:
                trace.append(pending.pop(0)[1])
            operation, request = self.client.calls[index]
            trace.append({"ddb": operation, "request": deepcopy(request)})
        trace.extend(entry for _, entry in pending)
        run["trace"] = trace
        runs.append(run)


def _tracing_s3(sink):
    from tests.mock_storage_support import MemoryS3

    class TracingS3(MemoryS3):
        def put_object(self, *, Bucket, Key, Body, Metadata=None, **kwargs):
            data = Body.encode() if type(Body) is str else Body
            sink.other({"s3": "put_object", "bucket": Bucket, "key": Key, "metadata": deepcopy(Metadata),
                        "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)})
            return super().put_object(Bucket=Bucket, Key=Key, Body=Body, Metadata=Metadata, **kwargs)

        def get_object(self, *, Bucket, Key):
            sink.other({"s3": "get_object", "bucket": Bucket, "key": Key,
                        "found": (Bucket, Key) in self.objects})
            return super().get_object(Bucket=Bucket, Key=Key)

        def generate_presigned_url(self, operation, *, Params, ExpiresIn):
            sink.other({"s3": "presign", "operation": operation, "params": deepcopy(Params), "expires": ExpiresIn})
            return super().generate_presigned_url(operation, Params=Params, ExpiresIn=ExpiresIn)

    return TracingS3()


def _tracing_events(sink):
    from tests.journey_support import EventLog

    class TracingEvents(EventLog):
        def recorder(self, role):
            inner = super().recorder(role)

            class _Recorder:
                def record(self, category, event, fields):
                    result = inner.record(category, event, fields)
                    sink.other({"log": [role, category, event, log_fields(category, fields)]})
                    return result
            return _Recorder()

    return TracingEvents()


def _tracing_calculator(sink, harness, version):
    from tests.calculator_doubles import TrackedCalculator
    from tests.journey_support import HARNESS_STAGE

    class TracingCalculator(TrackedCalculator):
        """The bundled calculator; ``mode`` scripts one interruption per call.

        The methods are overridden here rather than passed as TrackedCalculator
        hooks: the memory baseline fixes the diagnostic stack shape of an
        interrupted call as ``tests/worker_trace_support.py in calculate``
        directly below the worker frame.
        """

        def __init__(self):
            super().__init__(stage=HARNESS_STAGE, version=version)
            self.mode = None

        def calculate(self, loaded, binding, heartbeat):
            sink.other({"adapter": "calculate", "binding": deepcopy(binding)})
            if self.mode == "error":
                raise RuntimeError("PRIVATE-MEMORY-MARKER")
            if self.mode == "killed":
                raise ProcessKilled()
            if type(self.mode) is tuple:
                raise JourneyError(self.mode[1])
            if self.mode == "lease_expires":
                harness[0].advance(LEASE_SECONDS + 1)
            return super().calculate(loaded, binding, heartbeat)

        def validate_response(self, raw, projected, binding):
            sink.other({"adapter": "validate_response", "binding": deepcopy(binding),
                        "sha256": hashlib.sha256(raw).hexdigest()})
            return super().validate_response(raw, projected, binding)

        def get_chart(self, verified, binding, heartbeat):
            sink.other({"adapter": "get_chart", "binding": deepcopy(binding)})
            return super().get_chart(verified, binding, heartbeat)

    return TracingCalculator()


def _memory_harness(guard=None, legacy=False):
    from mock_journey.contracts import CURRENT_ADAPTER_VERSION, PENDING_GOAL_ADAPTER_VERSION
    from tests.journey_support import JourneyStore, V2Journey, with_registry_adapters

    sink = _Sink()
    store = JourneyStore.memory()
    sink.client = store.client
    objects = _tracing_s3(sink)
    holder = [None]
    # The legacy fixture job is bound to the retained pending-v3 adapter (D136, 3A);
    # course attempts use the current adapter. The other registry versions are
    # registered as plain bundled calculators (they take no call in these runs).
    adapter = _tracing_calculator(sink, holder, PENDING_GOAL_ADAPTER_VERSION if legacy else CURRENT_ADAPTER_VERSION)
    options = {}
    seeded = None
    if legacy:
        from tests.legacy_rows_support import seed_legacy_rows
        seeded = seed_legacy_rows(store, objects)
        options["start"] = seeded.meta["capture_clock"] + 60
    h = V2Journey(store, objects=objects, events=_tracing_events(sink), adapters=[adapter], **options)
    holder[0] = h
    if guard is not None:
        from mock_journey.assembly import build_worker
        if guard == "aws":
            from mock_journey.aws_lease import AwsLeaseGuardFactory
            factory = AwsLeaseGuardFactory(lease_seconds=LEASE_SECONDS, interval_seconds=19,
                                           renewal_timeout_seconds=5)
        else:
            from local_server.lease import LocalLeaseGuardFactory
            factory = LocalLeaseGuardFactory(lease_seconds=LEASE_SECONDS, interval_seconds=19,
                                             renewal_timeout_seconds=5)
        h.worker = build_worker(h.worker_settings, dynamodb_client=store.client, s3_client=objects,
                                legacy_bindings=h.bindings,
                                adapters=with_registry_adapters([adapter], stage=h.composition.stage),
                                required_bindings=h.execution.required_bindings, clock=h.clock,
                                operations=h._operations("worker"), lease_guard_factory=factory)
    return SimpleNamespace(h=h, sink=sink, adapter=adapter, seeded=seeded, runs=[])


def _process(env, label, job_id):
    with env.sink.window(env.runs, label) as run:
        run["returned"] = env.h.worker.process(job_id)


def _practice(env, program="mock-compression-only"):
    from tests.journey_support import dummy_course
    h = env.h
    course = dummy_course(program, "adult")
    session = h.login()
    started = h.start(session.token, course, course.practice_link_id)
    h.upload(session.token, started["attemptId"], started["condition"])
    h.advance(1)
    return session, course, h.job_id(started["attemptId"])


def _final(env):
    session, course, practice = _practice(env)
    h = env.h
    assert h.work(job_id=practice) is True
    final = h.start(session.token, course, course.final_link_id)
    h.upload(session.token, final["attemptId"], final["condition"])
    h.advance(1)
    return h.job_id(final["attemptId"])


def memory_course_practice():
    env = _memory_harness()
    _process(env, "practice", _practice(env)[2])
    return env.runs


def memory_course_practice_aws_guard():
    env = _memory_harness(guard="aws")
    _process(env, "practice", _practice(env)[2])
    return env.runs


def memory_course_practice_local_guard():
    env = _memory_harness(guard="local")
    _process(env, "practice", _practice(env)[2])
    return env.runs


def memory_course_ventilation_no_chart():
    env = _memory_harness()
    _process(env, "practice", _practice(env, "mock-ventilation-only")[2])
    return env.runs


def memory_legacy_queued():
    env = _memory_harness(legacy=True)
    _process(env, "queued", env.seeded.attempts["queued"]["job_id"])
    _process(env, "queued_again", env.seeded.attempts["queued"]["job_id"])
    return env.runs


def memory_legacy_queued_local_guard():
    env = _memory_harness(guard="local", legacy=True)
    _process(env, "queued", env.seeded.attempts["queued"]["job_id"])
    return env.runs


def memory_legacy_calculation_failed():
    env = _memory_harness(legacy=True)
    env.adapter.mode = ("journey", "CALCULATION_FAILED")
    _process(env, "queued", env.seeded.attempts["queued"]["job_id"])
    return env.runs


def memory_course_candidate_resume():
    env = _memory_harness()
    job_id = _practice(env)[2]
    storage = env.h.worker.storage
    original = storage.save_calculation

    def saved_then_killed(*args, **kwargs):
        original(*args, **kwargs)
        raise ProcessKilled()

    storage.save_calculation = saved_then_killed
    _process(env, "killed_after_save", job_id)
    del storage.save_calculation
    env.h.advance(LEASE_SECONDS + 1)
    _process(env, "resumed", job_id)
    return env.runs


def memory_course_lease_expires():
    env = _memory_harness()
    job_id = _practice(env)[2]
    env.adapter.mode = "lease_expires"
    _process(env, "lease_lost", job_id)
    return env.runs


def memory_course_proven_failure():
    env = _memory_harness()
    job_id = _final(env)
    env.adapter.mode = ("journey", "CALCULATION_FAILED")
    _process(env, "final_failed", job_id)
    env.adapter.mode = None
    env.h.advance(LEASE_SECONDS + 1)
    _process(env, "final_again", job_id)
    return env.runs


def _restart_runs(env, job_id, mode):
    env.adapter.mode = mode
    for number in range(CALCULATION_RESTART_LIMIT + 1):
        _process(env, f"interrupted_{number + 1}", job_id)
        env.h.advance(LEASE_SECONDS + 1)
    env.adapter.mode = None
    _process(env, "restart_limit", job_id)
    _process(env, "after_limit", job_id)


def memory_course_restart_limit_error():
    env = _memory_harness()
    _restart_runs(env, _final(env), "error")
    return env.runs


def memory_course_restart_limit_killed():
    env = _memory_harness()
    _restart_runs(env, _final(env), "killed")
    return env.runs


def memory_legacy_restart_limit():
    env = _memory_harness(legacy=True)
    _restart_runs(env, env.seeded.attempts["queued"]["job_id"], "error")
    return env.runs


MEMORY_SCENARIOS = {
    "course_practice": memory_course_practice,
    "course_practice_aws_guard": memory_course_practice_aws_guard,
    "course_practice_local_guard": memory_course_practice_local_guard,
    "course_ventilation_no_chart": memory_course_ventilation_no_chart,
    "legacy_queued": memory_legacy_queued,
    "legacy_queued_local_guard": memory_legacy_queued_local_guard,
    "legacy_calculation_failed": memory_legacy_calculation_failed,
    "course_candidate_resume": memory_course_candidate_resume,
    "course_lease_expires": memory_course_lease_expires,
    "course_proven_failure": memory_course_proven_failure,
    "course_restart_limit_error": memory_course_restart_limit_error,
    "course_restart_limit_killed": memory_course_restart_limit_killed,
    "legacy_restart_limit": memory_legacy_restart_limit,
}


def run_memory(name):
    """Run one memory scenario; return its canonical worker runs."""
    return canonical(MEMORY_SCENARIOS[name]())

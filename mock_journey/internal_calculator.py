"""Execute the bundled calculator with isolated raw, chart and count context.

This is an internal worker dependency, not an HTTP client. LoadedInput must
come from storage.load_input's manifest, binding and checksum verification;
the dataclass itself is not proof of persistence. Stored candidates are
validated after the storage layer verifies their checksum and job binding.
No default CPR completion interpretation or runtime registration is supplied.
"""

from copy import deepcopy
import hashlib
import re
import uuid

from mock_journey import typed
from mock_journey.contracts import (
    PENDING_GOAL_ADAPTER_VERSION,
    RETAINED_PENDING_GOAL_ADAPTER_VERSION,
    PENDING_GOAL_ADAPTER_VERSIONS,
    PENDING_GOAL_PROFILE_VERSION,
    VerifiedCalculation,
    VerifiedChart,
)
from mock_journey.errors import JourneyError
from mock_journey.projection import LoadedInput, typed_identity


_BINDING = frozenset(("attempt_id", "epoch", "input_digest", "adapter_version",
                      "projection_version", "job_id", "call_id"))
_SCHEMA = "arc-internal-calculation-v1"
_PENDING_SCHEMA = "arc-internal-calculation-v2"
_VERSION = re.compile(r"[A-Za-z0-9_.-]{1,128}\Z")
_STEM = re.compile(r"CPR-ACTION-([0-9]{10})-([0-9a-f-]{36})\Z")
_CORE_KEYS = frozenset(("cpr_score", "metrics", "aed_score", "training_stats",
                        "action_count", "guide_prompts", "chart_dataset_url"))


def _invalid():
    return JourneyError("CALCULATOR_CONTRACT_MISMATCH")


def _count(value):
    if type(value) is not int or value < 0:
        raise _invalid()
    return value


def _validate_core(core):
    """Check the current serializer's structure without changing any score."""
    if (type(core) is not dict or set(core) != _CORE_KEYS
            or any(type(core[key]) is not dict for key in (
                "cpr_score", "metrics", "aed_score", "training_stats", "action_count"))
            or type(core["guide_prompts"]) is not list
            or any(type(prompt) is not str for prompt in core["guide_prompts"])
            or core["chart_dataset_url"] is not None):
        raise _invalid()
    scores = core["cpr_score"]
    if (set(scores) != {"total_score", "part_scores"}
            or type(scores["total_score"]) is not dict
            or type(scores["part_scores"]) is not list
            or "overall" not in scores["total_score"]
            or type(scores["total_score"]["overall"]) not in (int, float, type(None))
            or set(core["action_count"]) != {"comp", "vent"}
            or set(core["training_stats"]) != {"cycle_count", "elapsed_seconds"}
            or type(core["training_stats"]["elapsed_seconds"]) not in (int, float)
            or set(core["aed_score"]) != {"overall", "part_scores"}
            or type(core["aed_score"]["overall"]) not in (int, float, type(None))
            or type(core["aed_score"]["part_scores"]) is not list):
        raise _invalid()
    _count(core["training_stats"]["cycle_count"])


class InternalCalculator:
    def __init__(self, *, version, projection_version, stage, cycle_goal_resolver=None,
                 allow_pending_cycle_goal=False):
        if (any(type(value) is not str or not _VERSION.fullmatch(value)
                for value in (version, projection_version, stage))
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", stage)
                or (cycle_goal_resolver is not None and not callable(cycle_goal_resolver))
                or type(allow_pending_cycle_goal) is not bool
                or allow_pending_cycle_goal != (version in PENDING_GOAL_ADAPTER_VERSIONS)
                or (allow_pending_cycle_goal and cycle_goal_resolver is not None)):
            raise ValueError("Invalid internal calculator configuration.")
        self.version, self.projection_version, self.stage = version, projection_version, stage
        self.cycle_goal_resolver = cycle_goal_resolver
        self.allow_pending_cycle_goal = allow_pending_cycle_goal
        self.candidate_schema = _PENDING_SCHEMA if allow_pending_cycle_goal else _SCHEMA

    @property
    def can_calculate(self):
        # The retained v2 candidate is still verifiable, but running the current
        # detector under its old name would silently change accepted-job meaning.
        return self.version != RETAINED_PENDING_GOAL_ADAPTER_VERSION

    def _validate_input(self, projected, binding):
        try:
            if type(binding) is not dict or set(binding) != _BINDING:
                raise _invalid()
            if any(type(value) is not str or not value for value in binding.values()):
                raise _invalid()
            for key in ("attempt_id", "job_id", "call_id"):
                if str(uuid.UUID(binding[key])) != binding[key]:
                    raise _invalid()
            definition = projected.payload["definition"]
            if (binding["input_digest"] != typed_identity(projected)
                    or binding["adapter_version"] != self.version
                    or binding["projection_version"] != self.projection_version
                    or definition["adapter_version"] != self.version
                    or definition["projection_version"] != self.projection_version
                    or typed.canonical_bytes(definition["condition"]) != typed.canonical_bytes(
                        projected.payload["calculation_input"]["condition"])):
                raise _invalid()
            kind = definition["goal"]["kind"]
            training = definition["condition"]["training_type"]
            if {"compression_only": "compressions", "ventilation_only": "ventilations",
                    "cpr": "cycles"}.get(training) != kind:
                raise _invalid()
            _count(definition["goal"]["required"])
            if self.allow_pending_cycle_goal and definition.get("profile_version") != PENDING_GOAL_PROFILE_VERSION:
                raise _invalid()
            return definition
        except JourneyError:
            raise
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError, UnicodeError):
            raise _invalid() from None

    def _raw_location(self, loaded):
        from util.uploader import date_prefix, org_prefix

        try:
            base = loaded.raw_base
            if (type(base) is not str or "\\" in base
                    or any(part in ("", ".", "..") for part in base.split("/"))):
                raise _invalid()
            parts = base.split("/")
            if len(parts) < 5:
                raise _invalid()
            stage, org, day, stem = parts[-4:]
            match = _STEM.fullmatch(stem)
            if not match or str(uuid.UUID(match[2])) != match[2]:
                raise _invalid()
            organization = loaded.projected.payload["response_context"].get("Organization")
            if (stage != self.stage or day != date_prefix(stem)
                    or org != org_prefix((organization or {}).get("org_id"))):
                raise _invalid()
            return stem, org
        except JourneyError:
            raise
        except (ValueError, TypeError, AttributeError, OverflowError, OSError):
            raise _invalid() from None

    def calculate(self, loaded, binding, heartbeat):
        if not self.can_calculate:
            raise JourneyError("TEMPORARILY_UNAVAILABLE")
        if type(loaded) is not LoadedInput or not callable(heartbeat):
            raise _invalid()
        projected = loaded.projected
        definition = self._validate_input(projected, binding)
        if (definition["goal"]["kind"] == "cycles" and self.cycle_goal_resolver is None
                and not self.allow_pending_cycle_goal):
            # Configuration is incomplete; do not consume this input as failed.
            raise JourneyError("TEMPORARILY_UNAVAILABLE")
        stem, org = self._raw_location(loaded)
        return self._calculate(loaded, binding, heartbeat, definition, stem, org)

    def _calculate(self, loaded, binding, heartbeat, definition, stem, org):
        # The concrete context API is shared with the core; no global uploader
        # monkeypatch or environment switch participates in a request.
        from main import run_calculator
        from services.calculation_context import CalculationExecutionContext, AcceptedRaw, CalculationContextError

        projected = loaded.projected
        charts, observations = [], []

        def chart_publisher(data):
            charts.append(typed.parse_json(typed.json_bytes(data)))
            return None  # The fenced worker publishes and signs the selected chart.

        context = CalculationExecutionContext(
            accepted_raw=AcceptedRaw(
                key_stem=stem, org=org,
                cpr_sha256=hashlib.sha256(projected.cpr_bytes).hexdigest(), cpr_size=len(projected.cpr_bytes),
                aed_sha256=hashlib.sha256(projected.aed_bytes).hexdigest(), aed_size=len(projected.aed_bytes),
            ),
            publish_chart=chart_publisher, observe=observations.append,
        )
        inputs = projected.payload["calculation_input"]
        response_context = projected.payload["response_context"]
        heartbeat()
        try:
            core = run_calculator(
                projected.cpr_bytes, projected.aed_bytes, deepcopy(inputs["condition"]),
                deepcopy(inputs["vp_event_list"]), usage=deepcopy(response_context.get("Usage")),
                stage=self.stage, organization=deepcopy(response_context.get("Organization")),
                execution_context=context,
            )
        except JourneyError:
            raise
        except CalculationContextError:
            raise _invalid() from None
        except (ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError, RecursionError):
            raise JourneyError("CALCULATION_FAILED") from None
        heartbeat()
        if len(charts) != 1 or len(observations) != 1:
            raise _invalid()
        evidence = observations[0]
        counts = {"comp": _count(evidence.comp_count), "vent": _count(evidence.vent_count)}
        kind = definition["goal"]["kind"]
        pending = self.allow_pending_cycle_goal and kind == "cycles"
        observed = (None if pending else counts["comp"] if kind == "compressions"
                    else counts["vent"] if kind == "ventilations"
                    else _count(self.cycle_goal_resolver(evidence, deepcopy(definition))))
        goal = {"kind": kind, "observed": observed}
        if self.allow_pending_cycle_goal:
            goal["status"] = "pending_policy" if pending else "evaluated"
        chart_bytes = typed.json_bytes(charts[0])
        candidate = {"schema": self.candidate_schema, "binding": deepcopy(binding), "core_result": core,
                     "goal": goal, "counts": counts,
                     "chart": {"data": charts[0], "sha256": hashlib.sha256(chart_bytes).hexdigest()}}
        raw = typed.json_bytes(candidate)
        self.validate_response(raw, projected, binding)
        return raw

    def validate_response(self, raw, projected, binding):
        definition = self._validate_input(projected, binding)
        if (definition["goal"]["kind"] == "cycles" and self.cycle_goal_resolver is None
                and not self.allow_pending_cycle_goal):
            raise JourneyError("TEMPORARILY_UNAVAILABLE")
        try:
            if type(raw) is not bytes:
                raise _invalid()
            candidate = typed.parse_json(raw)
            if (type(candidate) is not dict
                    or set(candidate) != {"schema", "binding", "core_result", "goal", "counts", "chart"}
                    or candidate["schema"] != self.candidate_schema
                    or typed.canonical_bytes(candidate["binding"]) != typed.canonical_bytes(binding)):
                raise _invalid()
            core, goal, counts, chart = (candidate[key] for key in ("core_result", "goal", "counts", "chart"))
            _validate_core(core)
            goal_fields = {"kind", "observed", "status"} if self.allow_pending_cycle_goal else {"kind", "observed"}
            if (type(goal) is not dict or set(goal) != goal_fields
                    or goal["kind"] != definition["goal"]["kind"]
                    or type(counts) is not dict or set(counts) != {"comp", "vent"}
                    or type(chart) is not dict or set(chart) != {"data", "sha256"}
                    or type(chart["data"]) is not dict):
                raise _invalid()
            for key in ("comp", "vent"):
                if _count(counts[key]) != _count(core["action_count"][key]):
                    raise _invalid()
            goal_status = None
            if self.allow_pending_cycle_goal:
                goal_status = "pending_policy" if goal["kind"] == "cycles" else "evaluated"
                if goal["status"] != goal_status:
                    raise _invalid()
            if goal_status == "pending_policy":
                if goal["observed"] is not None:
                    raise _invalid()
                observed = None
            else:
                observed = _count(goal["observed"])
            expected = {"compressions": counts["comp"], "ventilations": counts["vent"]}.get(goal["kind"])
            if expected is not None and observed != expected:
                raise _invalid()
            checksum = hashlib.sha256(typed.json_bytes(chart["data"])).hexdigest()
            if type(chart["sha256"]) is not str or chart["sha256"] != checksum:
                raise _invalid()
            return VerifiedCalculation(core, goal["kind"], observed, "snapshot",
                                       {**chart, "binding": deepcopy(binding)}, goal_status)
        except JourneyError:
            raise
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError, UnicodeError, OverflowError):
            raise _invalid() from None

    def get_chart(self, verified, binding, heartbeat):
        if type(verified) is not VerifiedCalculation or not callable(heartbeat):
            raise _invalid()
        try:
            chart = verified.chart_reference
            if (verified.chart_kind != "snapshot" or type(chart) is not dict
                    or set(chart) != {"data", "sha256", "binding"}
                    or typed.canonical_bytes(chart["binding"]) != typed.canonical_bytes(binding)
                    or hashlib.sha256(typed.json_bytes(chart["data"])).hexdigest() != chart["sha256"]):
                raise _invalid()
            heartbeat()
            return VerifiedChart(deepcopy(chart["data"]), chart["sha256"])
        except JourneyError:
            raise
        except (ValueError, TypeError, KeyError, RecursionError, UnicodeError):
            raise _invalid() from None

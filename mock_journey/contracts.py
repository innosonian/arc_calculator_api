"""Versioned internal calculation contracts and verified result evidence."""

from dataclasses import dataclass
from typing import Protocol, Callable
from types import MappingProxyType
import re

from mock_journey.errors import JourneyError
from mock_journey.projection import LoadedInput, ProjectedInput
from mock_journey.typed import canonical_bytes


# Adapter versions. Names are part of stored definitions/bindings; never rename.
#
# * arc-local-calculator-pending-v2 (RETAINED_PENDING_GOAL_ADAPTER_VERSION):
#   the first pending-goal adapter. Verify-only: it validates its stored
#   candidates and never runs a calculation (D25/D136).
# * arc-internal-detection-pending-v3 (PENDING_GOAL_ADAPTER_VERSION): the
#   current detection under the pending profile. Retained, but it still
#   calculates attempts that were started with its definition, finishing a
#   cycles goal as pending_policy (D136, 3A). It is not verify-only.
# * arc-internal-detection-v4 (CYCLE_GOAL_ADAPTER_VERSION): the current
#   adapter. Same detection; a cycles goal is evaluated by the D136 closed
#   cycle rule (mock_journey.cycle_goal) under the cycles profile.
RETAINED_PENDING_GOAL_ADAPTER_VERSION = "arc-local-calculator-pending-v2"
PENDING_GOAL_ADAPTER_VERSION = "arc-internal-detection-pending-v3"
PENDING_GOAL_ADAPTER_VERSIONS = frozenset({PENDING_GOAL_ADAPTER_VERSION, RETAINED_PENDING_GOAL_ADAPTER_VERSION})
PENDING_GOAL_PROFILE_VERSION = "tester-goal-pending-v2"
CYCLE_GOAL_ADAPTER_VERSION = "arc-internal-detection-v4"
CYCLE_GOAL_PROFILE_VERSION = "tester-goal-cycles-v1"
# Every adapter whose goal carries a status (the versioned goal shape).
VERSIONED_GOAL_ADAPTER_VERSIONS = PENDING_GOAL_ADAPTER_VERSIONS | {CYCLE_GOAL_ADAPTER_VERSION}
# Version registry (S6-10, D127, D136). New execution definitions select the
# current adapter. The retained tuple is the registration order of the old
# adapters the Worker must keep addressable for already accepted jobs; the
# AWS setting must equal it exactly. Retained is not the same as verify-only:
# only VERIFY_ONLY_ADAPTER_VERSIONS never start a calculation.
# execution_definitions re-exports PROJECTION_VERSION.
PROJECTION_VERSION = "arc-local-projection-v1"
CURRENT_ADAPTER_VERSION = CYCLE_GOAL_ADAPTER_VERSION
RETAINED_ADAPTER_VERSIONS = (RETAINED_PENDING_GOAL_ADAPTER_VERSION, PENDING_GOAL_ADAPTER_VERSION)
VERIFY_ONLY_ADAPTER_VERSIONS = frozenset({RETAINED_PENDING_GOAL_ADAPTER_VERSION})

# Input/call binding (S6-07): the arc-binding metadata digest of stored input,
# candidate and final objects is typed.digest over exactly these fields (the
# digest sorts keys; callers keep this field order in the dicts they build).
INPUT_BINDING_FIELDS = ("attempt_id", "epoch", "input_digest", "adapter_version", "projection_version")
CALL_BINDING_FIELDS = INPUT_BINDING_FIELDS + ("job_id", "call_id")


def input_binding(job):
    return {key: job[key] for key in INPUT_BINDING_FIELDS}


def call_binding(job):
    return {**input_binding(job), "job_id": job["job_id"], "call_id": job["call_id"]}


def is_versioned_adapter(version):
    """A pending-goal or cycle-goal adapter: its goal carries a status under a fixed profile."""
    return version in VERSIONED_GOAL_ADAPTER_VERSIONS


def is_versioned_goal(definition):
    """A versioned-adapter definition (see is_versioned_adapter)."""
    return is_versioned_adapter(definition.get("adapter_version"))


def expected_goal_status(kind, adapter_version):
    """The goal status the adapter reports for this goal kind; None when the goal has no status.

    A pending adapter keeps a cycles goal pending_policy (its stored results
    and in-flight attempts keep that meaning). The cycle-goal adapter reports
    every goal kind evaluated (D136).
    """
    if adapter_version in PENDING_GOAL_ADAPTER_VERSIONS:
        return "pending_policy" if kind == "cycles" else "evaluated"
    if adapter_version == CYCLE_GOAL_ADAPTER_VERSION:
        return "evaluated"
    return None


def expected_profile_version(adapter_version):
    """The profile a versioned adapter's definition must carry; None for other adapters."""
    if adapter_version in PENDING_GOAL_ADAPTER_VERSIONS:
        return PENDING_GOAL_PROFILE_VERSION
    if adapter_version == CYCLE_GOAL_ADAPTER_VERSION:
        return CYCLE_GOAL_PROFILE_VERSION
    return None


def is_retained_adapter(version):
    """Registered for already accepted jobs (D127). Not the same as verify-only."""
    return version in RETAINED_ADAPTER_VERSIONS


def is_verify_only_adapter(version):
    """Validates stored candidates only; never starts a calculation."""
    return version in VERIFY_ONLY_ADAPTER_VERSIONS


@dataclass(frozen=True)
class VerifiedCalculation:
    core_result: dict
    goal_kind: str
    observed: int | None
    chart_kind: str
    chart_reference: object = None
    goal_status: str | None = None

    def __post_init__(self):
        if (type(self.core_result) is not dict
                or self.goal_kind not in ("cycles", "compressions", "ventilations")
                or self.goal_status not in (None, "evaluated", "pending_policy")
                or (self.goal_status == "pending_policy" and (self.goal_kind != "cycles" or self.observed is not None))
                or (self.goal_status != "pending_policy" and (type(self.observed) is not int or self.observed < 0))
                or self.chart_kind not in ("snapshot", "no_chart")
                or (self.chart_kind == "no_chart" and self.chart_reference is not None)
                or (self.chart_kind == "snapshot" and self.chart_reference is None)):
            raise JourneyError("CALCULATOR_CONTRACT_MISMATCH")
        try:
            canonical_bytes(self.core_result)
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise JourneyError("CALCULATOR_CONTRACT_MISMATCH") from None


@dataclass(frozen=True)
class VerifiedChart:
    data: dict | list
    source_sha256: str

    def __post_init__(self):
        if type(self.data) not in (dict, list) or type(self.source_sha256) is not str or not re.fullmatch(r"[0-9a-f]{64}", self.source_sha256):
            raise JourneyError("CALCULATOR_CONTRACT_MISMATCH")
        try:
            canonical_bytes(self.data)
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise JourneyError("CALCULATOR_CONTRACT_MISMATCH") from None


class CalculatorAdapter(Protocol):
    """An explicitly registered internal calculator using verified stored input.

    Heartbeats guard ownership. A recovered candidate is validated without
    executing the calculator again; unresolved policy is never filled in here.
    """
    version: str
    projection_version: str

    def calculate(self, loaded: LoadedInput, binding: dict, heartbeat: Callable[[], None]) -> bytes: ...

    def validate_response(self, raw: bytes, projected: ProjectedInput, binding: dict) -> VerifiedCalculation: ...

    def get_chart(self, verified: VerifiedCalculation, binding: dict,
                  heartbeat: Callable[[], None]) -> VerifiedChart: ...


class CalculatorRegistry:
    """Keep old registered adapters addressable for already accepted jobs."""

    def __init__(self, adapters):
        registered = {}
        for adapter in adapters:
            key = (adapter.version, adapter.projection_version)
            if any(type(value) is not str or not value for value in key) or key in registered:
                raise ValueError("Invalid adapter registry.")
            registered[key] = adapter
        self._registered = MappingProxyType(registered)

    def resolve(self, version, projection_version):
        adapter = self._registered.get((version, projection_version))
        if adapter is None:
            # A missing retained adapter is an operating/configuration problem,
            # not proof that the accepted input is invalid. Preserve the job.
            raise JourneyError("TEMPORARILY_UNAVAILABLE")
        return adapter

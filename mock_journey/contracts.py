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
#   September detection under the pending profile. Its stored cycles goals
#   keep pending_policy (D136, 3A); it is now verify-only.
# * arc-internal-detection-v4 (CYCLE_GOAL_ADAPTER_VERSION): the first
#   cycle-goal adapter. Its stored goals use the D136 closed-cycle rule
#   (mock_journey.cycle_goal) under the cycles-v1 profile. Verify-only:
#   stored scores retain the D42 and D07/D08 meaning (D138, D139).
# * arc-internal-detection-v5 (EOF_VENT_ADAPTER_VERSION): the D138 adapter.
#   Verify-only under the cycles-v2 profile. Stored scores remain visible;
#   their evaluations enforce the ARC minimum quantity (D07) as a pass
#   condition (D139: decision "fail" with MINIMUM_QUANTITY_NOT_MET).
RETAINED_PENDING_GOAL_ADAPTER_VERSION = "arc-local-calculator-pending-v2"
PENDING_GOAL_ADAPTER_VERSION = "arc-internal-detection-pending-v3"
PENDING_GOAL_ADAPTER_VERSIONS = frozenset({PENDING_GOAL_ADAPTER_VERSION, RETAINED_PENDING_GOAL_ADAPTER_VERSION})
PENDING_GOAL_PROFILE_VERSION = "tester-goal-pending-v2"
CYCLE_GOAL_ADAPTER_VERSION = "arc-internal-detection-v4"
CYCLE_GOAL_PROFILE_VERSION = "tester-goal-cycles-v1"
EOF_VENT_ADAPTER_VERSION = "arc-internal-detection-v5"
EOF_VENT_PROFILE_VERSION = "tester-goal-cycles-v2"
ROUND2_ADAPTER_VERSION = "arc-internal-detection-v6"
ROUND2_PROFILE_VERSION = "tester-goal-cycles-v3"
GATE_REASON = "GATE_NOT_PASSED"
# D139 evaluation reason: a group of the session is below the ARC minimum
# quantity (D07). Fixed position: goal reason, this code, SCORE_NOT_PASS.
MINIMUM_QUANTITY_REASON = "MINIMUM_QUANTITY_NOT_MET"


@dataclass(frozen=True)
class AdapterFeatures:
    """The fixed meaning of one versioned adapter. A stored version never changes its row.

    goal_status: the status a ``cycles`` goal ends with ("pending_policy" or
        "evaluated"); compressions/ventilations goals are always "evaluated".
    profile: the profile_version a definition of this adapter must carry.
    candidate_schema: the schema name of the candidates this adapter writes.
    minimum_quantity_pass_gate: whether the evaluation itself enforces the ARC
        minimum quantity (D07) as a pass condition and reports
        MINIMUM_QUANTITY_NOT_MET (D139 True). False for the older adapters:
        their null policy already keeps such a session from passing, and
        their evaluations keep their stored shape.
    """
    goal_status: str
    profile: str
    candidate_schema: str
    minimum_quantity_pass_gate: bool


_PENDING_FEATURES = AdapterFeatures("pending_policy", PENDING_GOAL_PROFILE_VERSION,
                                    "arc-internal-calculation-v2", False)
ADAPTER_FEATURES = MappingProxyType({
    RETAINED_PENDING_GOAL_ADAPTER_VERSION: _PENDING_FEATURES,
    PENDING_GOAL_ADAPTER_VERSION: _PENDING_FEATURES,
    CYCLE_GOAL_ADAPTER_VERSION: AdapterFeatures("evaluated", CYCLE_GOAL_PROFILE_VERSION,
                                                "arc-internal-calculation-v3", False),
    EOF_VENT_ADAPTER_VERSION: AdapterFeatures("evaluated", EOF_VENT_PROFILE_VERSION,
                                              "arc-internal-calculation-v4", True),
    ROUND2_ADAPTER_VERSION: AdapterFeatures("evaluated", ROUND2_PROFILE_VERSION,
                                            "arc-internal-calculation-v5", True),
})
# Every adapter whose goal carries a status (the versioned goal shape).
VERSIONED_GOAL_ADAPTER_VERSIONS = frozenset(ADAPTER_FEATURES)
# Adapters that evaluate a cycles goal by the D136 closed-cycle rule.
CYCLE_RULE_ADAPTER_VERSIONS = frozenset(
    version for version, features in ADAPTER_FEATURES.items() if features.goal_status == "evaluated")
# Version registry (S6-10, D127, D136, D138). New execution definitions select
# the current adapter. The retained tuple is the registration order of the old
# adapters the Worker must keep addressable for already accepted jobs; the
# AWS setting must equal it exactly. All retained adapters validate stored
# candidates only and never start a calculation.
# execution_definitions re-exports PROJECTION_VERSION.
PROJECTION_VERSION = "arc-local-projection-v1"
CURRENT_ADAPTER_VERSION = ROUND2_ADAPTER_VERSION
CURRENT_PROFILE_VERSION = ADAPTER_FEATURES[CURRENT_ADAPTER_VERSION].profile
RETAINED_ADAPTER_VERSIONS = (RETAINED_PENDING_GOAL_ADAPTER_VERSION, PENDING_GOAL_ADAPTER_VERSION,
                             CYCLE_GOAL_ADAPTER_VERSION, EOF_VENT_ADAPTER_VERSION)
VERIFY_ONLY_ADAPTER_VERSIONS = frozenset(RETAINED_ADAPTER_VERSIONS)

# Input/call binding (S6-07): the arc-binding metadata digest of stored input,
# candidate and final objects is typed.digest over exactly these fields (the
# digest sorts keys; callers keep this field order in the dicts they build).
INPUT_BINDING_FIELDS = ("attempt_id", "epoch", "input_digest", "adapter_version", "projection_version")
CALL_BINDING_FIELDS = INPUT_BINDING_FIELDS + ("job_id", "call_id")


def input_binding(job):
    return {key: job[key] for key in INPUT_BINDING_FIELDS}


def call_binding(job):
    return {**input_binding(job), "job_id": job["job_id"], "call_id": job["call_id"]}


def adapter_features(version):
    """The registered features of a versioned adapter; None for any other version."""
    return ADAPTER_FEATURES.get(version)


def is_versioned_adapter(version):
    """A pending-goal or cycle-goal adapter: its goal carries a status under a fixed profile."""
    return version in ADAPTER_FEATURES


def is_versioned_goal(definition):
    """A versioned-adapter definition (see is_versioned_adapter)."""
    return is_versioned_adapter(definition.get("adapter_version"))


def uses_cycle_rule(version):
    """The adapter evaluates a cycles goal by the D136 closed-cycle rule (it needs the resolver)."""
    return version in CYCLE_RULE_ADAPTER_VERSIONS


def expected_goal_status(kind, adapter_version):
    """The goal status the adapter reports for this goal kind; None when the goal has no status.

    A pending adapter keeps a cycles goal pending_policy (its stored results
    and in-flight attempts keep that meaning). A cycle-rule adapter reports
    every goal kind evaluated (D136).
    """
    features = ADAPTER_FEATURES.get(adapter_version)
    if features is None:
        return None
    return features.goal_status if kind == "cycles" else "evaluated"


def expected_profile_version(adapter_version):
    """The profile a versioned adapter's definition must carry; None for other adapters."""
    features = ADAPTER_FEATURES.get(adapter_version)
    return None if features is None else features.profile


def calculation_options(adapter_version):
    """Collect ARC gate evidence only for the current calculation adapter."""
    from services.calculation_context import CalculationOptions
    return CalculationOptions(collect_gates=uses_round2_gates(adapter_version))


def uses_round2_gates(adapter_version):
    return adapter_version == ROUND2_ADAPTER_VERSION


def gates_pass_on_minimum_quantity(adapter_version):
    """Whether this adapter's evaluation enforces the ARC minimum quantity as a pass condition (D139)."""
    features = ADAPTER_FEATURES.get(adapter_version)
    return features is not None and features.minimum_quantity_pass_gate


@dataclass(frozen=True)
class _MinimumQuantityPolicy:
    active: bool


def minimum_quantity_policy(condition, comp_count, vent_count):
    """The ARC minimum-quantity pass condition (D07) for the whole session.

    ARC2020/ARC2025 CPR requires 90 adult/child or 45 infant compressions
    and 6 ventilations. ``active`` means at least one group is below its
    minimum. A condition the calculator cannot configure is a contract error.
    """
    from services.config import Config
    try:
        config = Config(condition).calculation_config
        if not config.is_cpr() or config.condition.get("guideline") not in ("ARC2020", "ARC2025"):
            return _MinimumQuantityPolicy(False)
        minimum_comp_count = 45 if config.is_infant() else 90
        comp_below_minimum = comp_count < minimum_comp_count
        vent_below_minimum = vent_count < 6
        return _MinimumQuantityPolicy(comp_below_minimum or vent_below_minimum)
    except (KeyError, TypeError, ValueError, AttributeError):
        raise JourneyError("CALCULATOR_CONTRACT_MISMATCH") from None


def is_retained_adapter(version):
    """Registered to validate stored candidates of already accepted jobs (D127)."""
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
    gates: list[dict] | None = None
    learner_counts: dict | None = None

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

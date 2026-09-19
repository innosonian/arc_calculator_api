"""Versioned internal calculation contracts and verified result evidence."""

from dataclasses import dataclass
from typing import Protocol, Callable
from types import MappingProxyType
import re

from mock_journey.errors import JourneyError
from mock_journey.projection import LoadedInput, ProjectedInput
from mock_journey.typed import canonical_bytes


RETAINED_PENDING_GOAL_ADAPTER_VERSION = "arc-local-calculator-pending-v2"
PENDING_GOAL_ADAPTER_VERSION = "arc-internal-detection-pending-v3"
PENDING_GOAL_ADAPTER_VERSIONS = frozenset({PENDING_GOAL_ADAPTER_VERSION, RETAINED_PENDING_GOAL_ADAPTER_VERSION})
PENDING_GOAL_PROFILE_VERSION = "tester-goal-pending-v2"


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

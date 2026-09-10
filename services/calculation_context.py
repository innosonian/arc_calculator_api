"""Request-owned hooks for a previously accepted internal calculation.

The storage/execution layer verifies the complete accepted input manifest before
constructing this object. The receipt here verifies raw byte identity and the
existing storage names only; it does not authenticate a manifest or a user.
Default calculator calls do not use this module's hooks.
"""

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import hmac
import re
from threading import Lock

from config.constants import (
    CALC_CASE_CPR,
    CALC_CASE_DID_NOT_RESCUE_VENT,
    CALC_CASE_NOT_CALC,
    CALC_CASE_ONLY_COMP,
    CALC_CASE_ONLY_VENT,
)
from config.enums import ActorType


class CalculationContextError(RuntimeError):
    """A failed internal execution contract, without request/callback contents."""


_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_STEM = re.compile(r"CPR-ACTION-[0-9]{10}-" + _UUID)
_ORG = re.compile(_UUID)
_SHA256 = re.compile(r"[0-9a-f]{64}")
_CALC_CASES = frozenset((CALC_CASE_CPR, CALC_CASE_DID_NOT_RESCUE_VENT,
                        CALC_CASE_NOT_CALC, CALC_CASE_ONLY_COMP, CALC_CASE_ONLY_VENT))
_ACTOR_TYPES = frozenset(actor.value for actor in ActorType)


@dataclass(frozen=True, slots=True)
class AcceptedRaw:
    cpr_sha256: str
    cpr_size: int
    aed_sha256: str
    aed_size: int
    key_stem: str
    org: str

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        for digest in (self.cpr_sha256, self.aed_sha256):
            if type(digest) is not str or _SHA256.fullmatch(digest) is None:
                raise CalculationContextError("Invalid accepted raw receipt")
        if (type(self.cpr_size) is not int or self.cpr_size <= 0
                or type(self.aed_size) is not int or self.aed_size < 0):
            raise CalculationContextError("Invalid accepted raw receipt")
        if type(self.key_stem) is not str or _STEM.fullmatch(self.key_stem) is None:
            raise CalculationContextError("Invalid accepted raw receipt")
        if (type(self.org) is not str
                or (self.org != "_no_org" and _ORG.fullmatch(self.org) is None)):
            raise CalculationContextError("Invalid accepted raw receipt")


@dataclass(frozen=True, slots=True)
class CycleEvidence:
    """Existing cycle grouping facts; these do not assert a complete cycle."""

    part_number: int
    cycle_number: int
    calc_case: str
    actor_type: str
    compression_action_count: int
    ventilation_action_count: int


@dataclass(frozen=True, slots=True)
class CalculationEvidence:
    # Same whole-session action counts as the existing public action_count.
    comp_count: int
    vent_count: int
    cycle_group_count: int
    cycles: tuple[CycleEvidence, ...]


def _count(value: object) -> int:
    if type(value) is not int or value < 0:
        raise CalculationContextError("Invalid calculation evidence")
    return value


def _evidence(result: dict) -> CalculationEvidence:
    """Read calculated facts without scoring methods or mutable object aliases."""
    cycles = []
    for part in result["part_with_scores"]:
        part_number = _count(part["part_num"])
        for scored in part["result"]["cycle_with_score_list"]:
            if _count(scored.part_num) != part_number:
                raise CalculationContextError("Invalid calculation evidence")
            cycle = scored.cycle
            calc_case = scored.calc_case
            actor_type = cycle.actor_type.value
            if (type(calc_case) is not str or calc_case not in _CALC_CASES
                    or type(actor_type) is not str or actor_type not in _ACTOR_TYPES):
                raise CalculationContextError("Invalid calculation evidence")
            cycles.append(CycleEvidence(
                part_number=part_number,
                cycle_number=_count(cycle.cycle_num),
                calc_case=calc_case,
                actor_type=actor_type,
                compression_action_count=_count(cycle.get_comp_count()),
                ventilation_action_count=_count(cycle.get_vent_count()),
            ))
    return CalculationEvidence(_count(result["comp_count"]), _count(result["vent_count"]),
                               len(cycles), tuple(cycles))


class CalculationExecutionContext:
    """Single-use hooks; construct one per invocation, never a global capture.

    Callback failures abort this context path. They do not fall back to the
    diagnostic uploader. A publisher may capture a chart and return None while
    the execution layer commits and publishes its selected result later.
    """

    def __init__(self, *, accepted_raw: AcceptedRaw,
                 publish_chart: Callable[[dict], str | None],
                 observe: Callable[[CalculationEvidence], None]) -> None:
        if (type(accepted_raw) is not AcceptedRaw
                or not callable(publish_chart) or not callable(observe)):
            raise CalculationContextError("Invalid calculation execution context")
        accepted_raw.validate()
        self._accepted_raw = accepted_raw
        self._publisher = publish_chart
        self._observer = observe
        self._lock = Lock()
        self._state = "new"

    def _claim(self, expected: str, next_state: str) -> None:
        with self._lock:
            if self._state != expected:
                raise CalculationContextError("Invalid calculation context lifecycle")
            self._state = next_state

    def begin(self, cpr_bytes: bytes, aed_bytes: bytes) -> AcceptedRaw:
        self._claim("new", "raw_claimed")
        receipt = self._accepted_raw
        receipt.validate()
        if type(cpr_bytes) is not bytes or type(aed_bytes) is not bytes:
            raise CalculationContextError("Accepted raw input mismatch")
        if (len(cpr_bytes) != receipt.cpr_size or len(aed_bytes) != receipt.aed_size
                or not hmac.compare_digest(hashlib.sha256(cpr_bytes).hexdigest(), receipt.cpr_sha256)
                or not hmac.compare_digest(hashlib.sha256(aed_bytes).hexdigest(), receipt.aed_sha256)):
            raise CalculationContextError("Accepted raw input mismatch")
        self._claim("raw_claimed", "raw_verified")
        return receipt

    def observe_calculation(self, result: dict) -> None:
        self._claim("raw_verified", "observing")
        try:
            snapshot = _evidence(result)
            if self._observer(snapshot) is not None:
                raise CalculationContextError("Invalid calculation observer result")
        except Exception:
            raise CalculationContextError("Calculation evidence capture failed") from None
        self._claim("observing", "observed")

    def publish_chart(self, chart_data: dict) -> str | None:
        self._claim("observed", "publishing")
        try:
            if type(chart_data) is not dict:
                raise CalculationContextError("Invalid calculation chart")
            result = self._publisher(deepcopy(chart_data))
            if result is not None and type(result) is not str:
                raise CalculationContextError("Invalid calculation chart publisher result")
        except Exception:
            raise CalculationContextError("Calculation chart publication failed") from None
        self._claim("publishing", "finished")
        return result

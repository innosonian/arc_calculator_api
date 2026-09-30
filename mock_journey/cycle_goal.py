"""The CPR completion cycle rule (D136): observed cycles of a cycles goal.

An observed cycle is a cycle the calculator classified as ``cpr`` (compressions
followed by ventilations, closed): the last unfinished group and the
compression-only / ventilation-only / not-calculated groups are excluded. The
count follows the device's ``cycle_cnt`` grouping; compression/ventilation
counts inside a cycle are judged by the score, not here. Cycles of every actor
count (a virtual partner's cycles too); AED actions do not take part.
"""

from config.constants import CALC_CASE_CPR
from mock_journey.errors import JourneyError


def closed_cycle_count(evidence, definition):
    """CalculatorAdapter cycle_goal_resolver: the number of ``cpr`` cycles in the evidence."""
    from services.calculation_context import CalculationEvidence, CycleEvidence

    if (type(evidence) is not CalculationEvidence or type(evidence.cycles) is not tuple
            or any(type(cycle) is not CycleEvidence for cycle in evidence.cycles)
            or type(definition) is not dict or type(definition.get("goal")) is not dict
            or definition["goal"].get("kind") != "cycles"):
        raise JourneyError("CALCULATOR_CONTRACT_MISMATCH")
    return sum(1 for cycle in evidence.cycles if cycle.calc_case == CALC_CASE_CPR)

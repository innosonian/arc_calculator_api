"""Additive Round 2 checklist over the calculator's existing observations."""

from bisect import bisect_right
from itertools import accumulate
from math import isfinite

from config.constants import (
    ACTION_TYPE_COMP, ACTION_TYPE_VENT, AED_EVENT_ANALYZING, AED_EVENT_ARRIVED,
    AED_EVENT_BEGIN_CPR, AED_EVENT_NO_SHOCK_ADVISE, AED_EVENT_PADS_ON,
    AED_EVENT_POWER_ON, AED_EVENT_SHOCK_DELIVERED, AED_SEPARATE_EVENTS, CALC_CASE_CPR,
)
from config.enums import Actor, ActorType


# (threshold, unit, compare as a maximum). Repetitions uses the definition's
# goal. The arrival timer has no approved limit yet.
GATE_RULES = {
    "repetitions": (None, "count", False),
    "compression_depth_in_range_pct": (70, "%", False),
    "compression_rate_in_range_pct": (70, "%", False),
    "recoil_pct": (80, "%", False),
    "hand_position_pct": (80, "%", False),
    "compressions_per_cycle_tolerance": (2, "count", True),
    "ventilation_volume_in_range_pct": (70, "%", False),
    # BLS ventilation-only: action-based breaths/min against the
    # cprGuideline.ventilationRateMin/Max band, expressed as a 0-100 score.
    "ventilation_interval_score": (70, "score", False),
    "aed_pads_attached": (1, "count", False),
    "aed_no_contact_during_analysis": (0, "count", True),
    "aed_shock_delivered": (1, "count", False),
    "aed_arrival_to_power_on": (None, "seconds", True),
}

_PERCENT_METRICS = {
    "compression_depth_in_range_pct": "CompressionDepth",
    "compression_rate_in_range_pct": "CompressionRate",
    "recoil_pct": "Recoil",
    "hand_position_pct": "HandPosition",
    "ventilation_volume_in_range_pct": "VentilationVolume",
}

_CONDITIONAL_AED_GATES = frozenset(("aed_shock_delivered", "aed_arrival_to_power_on"))


def collect_action_evidence(actions, aed_events):
    """Count whole-session learner actions and inspect the raw AED event stream."""
    learner = {"comp": 0, "vent": 0}
    compressions = []
    for action in actions:
        kind = action["action_type"]
        if kind in (ACTION_TYPE_COMP, ACTION_TYPE_VENT) and action["actor"] != Actor.VIRTUAL_PARTNER:
            learner[kind] += 1
        if kind == ACTION_TYPE_COMP:
            compressions.append((action["first_timestamp"], action["last_timestamp"]))

    windows = []
    analyzing = arrived = power_on = None
    incomplete_analysis = False
    pads = shock = no_shock = False
    for entry in aed_events:
        event, timestamp = entry["event"], entry["timestamp"]
        if event == AED_EVENT_ARRIVED and arrived is None:
            arrived = timestamp
        elif event == AED_EVENT_POWER_ON and arrived is not None and power_on is None and timestamp >= arrived:
            power_on = timestamp
        elif event == AED_EVENT_PADS_ON:
            pads = True
        elif event == AED_EVENT_ANALYZING and analyzing is None:
            analyzing = timestamp
        elif event in AED_SEPARATE_EVENTS:
            shock |= event == AED_EVENT_SHOCK_DELIVERED
            no_shock |= event == AED_EVENT_NO_SHOCK_ADVISE
            if analyzing is not None and timestamp >= analyzing:
                windows.append((analyzing, timestamp))
            incomplete_analysis |= analyzing is not None and timestamp < analyzing
            analyzing = None
        elif event == AED_EVENT_BEGIN_CPR:
            incomplete_analysis |= analyzing is not None
            analyzing = None

    contacts = None
    if windows and analyzing is None and not incomplete_analysis:
        windows.sort()
        starts = [start for start, _ in windows]
        ends = list(accumulate((end for _, end in windows), max))
        contacts = 0
        for first, last in compressions:
            index = bisect_right(starts, last)
            if index and first <= ends[index - 1]:
                contacts += 1

    return {
        "learner_counts": learner,
        "pads_attached": int(pads),
        "analysis_contacts": contacts,
        "shock_delivered": int(shock),
        "no_shock_only": no_shock and not shock,
        "arrived": arrived is not None,
        "arrival_seconds": None if power_on is None else (power_on - arrived) / 1000,
    }


def _applicability(gate_id, definition):
    training = definition["condition"]["training_type"]
    if gate_id.startswith("aed_"):
        return definition.get("requiresAed", False)
    if gate_id == "ventilation_interval_score":
        return training == "ventilation_only" and definition.get("courseType") == "BLS"
    if gate_id == "ventilation_volume_in_range_pct":
        return training != "compression_only"
    if gate_id == "compressions_per_cycle_tolerance":
        return training == "cpr"
    if gate_id in _PERCENT_METRICS:
        return training != "ventilation_only"
    return True


def _gate(gate_id, value, applicable, definition):
    threshold, unit, maximum = GATE_RULES[gate_id]
    if gate_id == "repetitions":
        threshold = definition["goal"]["required"]
    if not applicable:
        return {"id": gate_id, "applicable": False, "value": None, "threshold": None, "unit": unit, "passed": None}
    threshold = definition.get("assessment", {}).get("gates", {}).get(gate_id, threshold)
    passed = None
    if threshold is not None:
        passed = value is not None and (value <= threshold if maximum else value >= threshold)
    return {"id": gate_id, "applicable": True, "value": value, "threshold": threshold, "unit": unit, "passed": passed}


def evaluate_gates(definition, observed, metrics, evidence):
    condition = definition["condition"]
    target = 15 if condition["cpr_cycle_type"] == "152" else 30
    learner_compression_actors = (ActorType.ONLY_REAL_PERSON.value, ActorType.VIRTUAL_PARTNER_VENT.value)
    cycle_error = max((
        abs(cycle.compression_action_count - target)
        for cycle in evidence.cycles
        if cycle.calc_case == CALC_CASE_CPR
        and (not condition["is_2rescuers"] or cycle.actor_type in learner_compression_actors)
    ), default=None)
    facts = evidence.gate_evidence
    values = {
        "repetitions": observed,
        "compressions_per_cycle_tolerance": cycle_error,
        "ventilation_interval_score": facts["ventilation_interval_score"],
        "aed_pads_attached": facts["pads_attached"],
        "aed_no_contact_during_analysis": facts["analysis_contacts"],
        "aed_shock_delivered": facts["shock_delivered"],
        "aed_arrival_to_power_on": facts["arrival_seconds"],
    }
    for gate_id, metric in _PERCENT_METRICS.items():
        distribution = metrics.get(metric)
        values[gate_id] = distribution.get("%_Good") if type(distribution) is dict else None
    return [
        _gate(gate_id, values[gate_id], _applicability(gate_id, definition)
              and (gate_id != "aed_shock_delivered" or not facts["no_shock_only"])
              and (gate_id != "aed_arrival_to_power_on" or facts["arrived"]), definition)
        for gate_id in GATE_RULES
    ]


def valid_gate_results(gates, definition, observed):
    """Validate the versioned checklist in durable candidate and evaluation records."""
    if type(gates) is not list or len(gates) != len(GATE_RULES):
        return False
    for gate_id, row in zip(GATE_RULES, gates):
        if type(row) is not dict or type(row.get("applicable")) is not bool:
            return False
        applicable = row["applicable"]
        configured = _applicability(gate_id, definition)
        if applicable != configured and (applicable or gate_id not in _CONDITIONAL_AED_GATES):
            return False
        value = row.get("value")
        if value is not None and not (
            type(value) in (int, float) and value >= 0 and (type(value) is int or isfinite(value))
        ):
            return False
        if gate_id == "repetitions" and (type(value) is not int or value != observed):
            return False
        if row.get("passed") is not None and type(row["passed"]) is not bool:
            return False
        if row.get("threshold") is not None and type(row["threshold"]) not in (int, float):
            return False
        if row != _gate(gate_id, value, applicable, definition):
            return False
    return True

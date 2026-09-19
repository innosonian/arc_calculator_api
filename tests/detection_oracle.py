"""Test-only oracle for D38-D46, separate from the production state machine.

Events are found by searching prefixes for the first qualifying pair, rather
than replaying the detector's mutable peak/confirmation/lock variables. Time
union is integrated over elementary endpoint intervals. Production detector,
action preparation, cycle marking and timeline helpers are never oracle inputs.

The unchanged scoring/serialization/coaching functions are deliberately reused:
their historical source hashes are checked separately, and fixed arithmetic
tests cover their inputs and weighted results. This is a detection-change oracle,
not an independent reimplementation of every historical scoring formula.
"""

from contextlib import ExitStack
from copy import deepcopy
from itertools import chain
from unittest.mock import patch
import hashlib


def event_trace(packets, target, training):
    if not packets:
        return []
    volumes = [max(p["ventilation_volume"]) for p in packets]
    depths = [max(p["compression_depth"]) for p in packets]
    rising = [False] + [b > a for a, b in zip(depths, depths[1:])]
    onset = [False] + [rising[i] and not rising[i - 1] for i in range(1, len(packets))]
    result = []
    if training != "ventilation_only":
        changes = [i for i in range(1, len(packets))
                   if packets[i]["compression_count"] > 0
                   and packets[i]["compression_count"] != packets[i - 1]["compression_count"]]
        for start, stop in zip([0] + changes, changes):
            result.append({"kind": "comp", "at": stop, "start": start, "stop": stop,
                           "rate": packets[stop]["compression_rate"]})
    if training != "compression_only":
        drop = 5 if target == "infant" else 10
        search_from = 0
        rearmed = False
        while search_from < len(volumes):
            start = next((i for i in range(search_from, len(volumes))
                          if volumes[i] > 0 and (not rearmed or volumes[i] > volumes[i - 1])), None)
            if start is None:
                break
            # Both low packets must lie after the peak. Computing the prefix
            # maximum separately makes an intervening recovery/new peak reset
            # the pair without a cumulative confirmation counter.
            # A positive excursion too small to meet the approved drop ends
            # when it returns to zero; it cannot lend its onset to a later one.
            running_peak = volumes[start]
            closure = None
            for i in range(start + 1, len(volumes)):
                running_peak = max(running_peak, volumes[i])
                if volumes[i] == 0 and running_peak < drop:
                    closure = i
                    break
                if running_peak >= drop:
                    break
            peak = volumes[start]
            confirmation = None
            for end in range(start + 2, closure + 1 if closure is not None else len(volumes)):
                peak = max(peak, volumes[end - 2])
                if min(peak - volumes[end - 1], peak - volumes[end]) >= drop:
                    confirmation = end
                    break
            if confirmation is None:
                if closure is not None:
                    search_from, rearmed = closure + 1, True
                    continue
                break
            peak_index = max(range(start, confirmation - 1), key=volumes.__getitem__)
            result.append({"kind": "vent", "at": confirmation, "start": start,
                           "stop": confirmation + 1, "peak": volumes[peak_index],
                           "peak_at": peak_index, "confirmations": [confirmation - 1, confirmation]})
            rearm_at = next((i for i in range(confirmation, len(volumes))
                             if volumes[i] == 0 or onset[i]), None)
            if rearm_at is None:
                break
            search_from, rearmed = rearm_at + 1, True
    # A completed ventilation phase with no new depth signal must not lend
    # the preceding compression peak to the next counted compression. If a
    # new rise spans that phase, retain its baseline and complete evidence.
    ventilation_ends = [event["at"] for event in result if event["kind"] == "vent"]
    for event in result:
        if event["kind"] != "comp":
            continue
        previous_count = event["start"]
        for boundary in ventilation_ends:
            if not previous_count <= boundary < event["at"]:
                continue
            new_rise = next((i for i in range(previous_count + 1, boundary + 1) if onset[i]), None)
            if new_rise is not None:
                event["start"] = max(event["start"], new_rise - 1)
            elif depths[boundary] == 0:
                event["start"] = boundary
    return sorted(result, key=lambda event: (event["at"], event["kind"] != "comp"))


def expected_actions(packets, target, training):
    from config.enums import Actor

    events = event_trace(packets, target, training)
    answer = []
    for event in events:
        indices = list(range(event["start"], event["stop"]))
        source = [packets[i] for i in indices]
        kind = event["kind"]
        boundary = max([other["at"] for other in events if other["at"] < event["start"]], default=0)
        begin = packets[boundary]["timestamp"] if kind == "vent" else source[0]["timestamp"]
        end = source[-1]["timestamp"]
        if kind == "vent":
            # D45's overlap example measures this breath from its own start.
            # Mere endpoint contact preserves the historical cadence anchor.
            overlaps = any(other["kind"] == "comp" and
                           max(source[0]["timestamp"], packets[other["start"]]["timestamp"]) <
                           min(end, packets[other["stop"] - 1]["timestamp"])
                           for other in events)
            if overlaps:
                begin = source[0]["timestamp"]
        samples = [p for p in source if (max(p["compression_depth"]) > 10 if kind == "comp"
                                        else max(p["ventilation_volume"]) > 0)] or source
        virtual = sum(p["actor"] == Actor.VIRTUAL_PARTNER for p in samples)
        actor = Actor.VIRTUAL_PARTNER if 2 * virtual > len(samples) else Actor.REAL_PERSON
        speed = 0
        if kind == "vent" and target == "infant":
            speed = packets[event["peak_at"]]["timestamp"] - source[0]["timestamp"]
        duration = end - begin
        answer.append({
            "action_type": kind,
            "compression_depth": list(chain.from_iterable(p["compression_depth"] for p in source)),
            "compression_rate": [event["rate"]] if kind == "comp" else [p["compression_rate"] for p in source],
            "compression_count": [p["compression_count"] for p in source],
            "hand_position": [p["hand_position"] for p in source],
            "ventilation_volume": list(chain.from_iterable(p["ventilation_volume"] for p in source)),
            "ventilation_count": [p["ventilation_count"] for p in source],
            "ventilation_speed": speed, "first_timestamp": begin, "last_timestamp": end,
            "is_aed_overlapped": [p["is_aed_overlapped"] for p in source], "actor": actor,
            "total_action_ms": duration, "handsoff_ms": max(0, duration - (1000 if kind == "comp" else 0)),
            "_event_packet": event["at"], "_detected_timestamp": packets[event["at"]]["timestamp"],
            "_source_packet_indices": indices, "_source_timestamps": [p["timestamp"] for p in source],
            "_elapsed_interval": (begin, end), "_rate_duration_ms": duration,
        })
    return answer


def expected_cycles(_marker, parts):
    for part in parts:
        actions = part["action_list"]
        groups = []
        for action in actions:
            if not groups or groups[-1][0]["_event_packet"] != action["_event_packet"]:
                groups.append([])
            groups[-1].append(action)
        cuts = [0] + [i for i in range(1, len(groups))
                      if any(a["action_type"] == "vent" for a in groups[i - 1])
                      and any(a["action_type"] == "comp" for a in groups[i])]
        for number, (left, right) in enumerate(zip(cuts, cuts[1:] + [len(groups)]), start=1):
            if left == right:
                continue
            selected = list(chain.from_iterable(groups[left:right]))
            lower = (groups[left - 1][-1]["_detected_timestamp"] if left else
                     min(a["first_timestamp"] for a in groups[0]))
            upper = groups[right - 1][-1]["_detected_timestamp"] if right < len(groups) else None
            for position, action in enumerate(selected):
                action["cycle_cnt"] = number
                action["compression_count"] = sum(a["action_type"] == "comp" for a in selected[:position + 1])
                action["ventilation_count"] = sum(a["action_type"] == "vent" for a in selected[:position + 1])
                action["_timeline_bounds"] = (lower, upper)
    return parts


def expected_totals(actions):
    if not actions:
        return 0, 0
    intervals, credits = [], 0
    for action in actions:
        start, end = action["_elapsed_interval"]
        if end < start:
            return (sum(a["total_action_ms"] for a in actions), sum(a["handsoff_ms"] for a in actions))
        lower, upper = action.get("_timeline_bounds", (start, None))
        start = max(start, lower)
        end = min(end, upper) if upper is not None else end
        if end > start:
            intervals.append((start, end))
            if action["action_type"] == "comp":
                credits += min(1000, end - start)
    endpoints = sorted(set(chain.from_iterable(intervals)))
    duration = sum(right - left for left, right in zip(endpoints, endpoints[1:])
                   if any(start <= left and right <= end for start, end in intervals))
    return duration, max(duration - credits, 0)


def run_expected(cpr, aed, condition, vp_events=()):
    """Run fixed scoring with independent events, signal arrays, cycles and time."""
    from main import run_calculator
    from data_handlers.action_data import ActionDataPrepare
    from transformers.counter import CountMarker
    from services.calculation_context import AcceptedRaw, CalculationExecutionContext

    charts = []
    context = CalculationExecutionContext(
        accepted_raw=AcceptedRaw(hashlib.sha256(cpr).hexdigest(), len(cpr), hashlib.sha256(aed).hexdigest(), len(aed),
                                 "CPR-ACTION-1700000000-12345678-1234-4234-9234-123456789abc", "_no_org"),
        publish_chart=lambda chart: charts.append(deepcopy(chart)), observe=lambda _evidence: None,
    )
    with ExitStack() as stack:
        stack.enter_context(patch.object(ActionDataPrepare, "get_action_list",
                                        lambda self, packets: expected_actions(packets, condition["target"], condition["training_type"])))
        stack.enter_context(patch.object(CountMarker, "make_count", expected_cycles))
        stack.enter_context(patch("calculators.cycle_evaluator.timeline_totals", expected_totals))
        stack.enter_context(patch("calculators.metric_evaluator.timeline_totals", expected_totals))
        result = run_calculator(cpr, aed, deepcopy(condition), deepcopy(list(vp_events)), stage="test",
                                execution_context=context)
    assert len(charts) == 1
    return result, charts[0]

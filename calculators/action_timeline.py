"""Shared elapsed-time accounting for independently detected action evidence."""

from config.constants import ACTION_TYPE_COMP, HANDSOFF_DEADTIME_MS


def timeline_totals(actions: list[dict]) -> tuple[int, int]:
    """Return elapsed and hands-off without changing individual rate durations.

    Legacy action-library callers without event metadata retain their previous
    scalar contract. Generated actions have observed intervals and disjoint
    cycle ownership bounds, while complete signal arrays remain unchanged.
    """
    if not actions:
        return 0, 0
    if any("_elapsed_interval" not in action for action in actions):
        return (sum(action["total_action_ms"] for action in actions),
                sum(action["handsoff_ms"] for action in actions))

    intervals = []
    compression_credit = 0
    for action in actions:
        start, end = action["_elapsed_interval"]
        # Preserve the existing timestamp-error behavior; this change does not
        # reorder, interpolate, or repair malformed packet timestamps.
        if end < start:
            return (sum(item["total_action_ms"] for item in actions),
                    sum(item["handsoff_ms"] for item in actions))
        lower, upper = action.get("_timeline_bounds", (start, None))
        start = max(start, lower)
        end = min(end, upper) if upper is not None else end
        duration = max(end - start, 0)
        if duration:
            intervals.append((start, end))
        if action["action_type"] == ACTION_TYPE_COMP:
            compression_credit += min(duration, HANDSOFF_DEADTIME_MS)

    elapsed = 0
    previous_end = None
    # Sorting interval endpoints only computes a union; original packet order,
    # event order, counters and source samples are never sorted or deduplicated.
    for start, end in sorted(intervals):
        uncovered_start = max(start, previous_end) if previous_end is not None else start
        elapsed += max(end - uncovered_start, 0)
        previous_end = max(previous_end, end) if previous_end is not None else end
    return elapsed, max(elapsed - compression_credit, 0)

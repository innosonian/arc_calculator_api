# 원본 hstm_v2 transformers/counter.py (V1 패리티 보존)
from config.constants import ACTION_TYPE_VENT, ACTION_TYPE_COMP
from itertools import groupby


class CountMarker:
    """
    mark count - cycle, comp, vent
    """

    def make_count(self, partial_actions: list[dict]) -> list[dict]:
        for partial_action in partial_actions:
            comp_cnt = 0
            vent_cnt = 0
            cycle_cnt = 1
            pre_action = ACTION_TYPE_COMP
            cycle_actions = []
            previous_boundary = None
            cycle_start = None
            indexed_actions = list(enumerate(partial_action["action_list"]))
            for _, indexed_group in groupby(indexed_actions, key=lambda item: (
                ("packet", item[1]["_event_packet"]) if "_event_packet" in item[1] else ("action", item[0])
            )):
                group = [action for _, action in indexed_group]
                contains_comp = any(action["action_type"] == ACTION_TYPE_COMP for action in group)
                if pre_action == ACTION_TYPE_VENT and contains_comp:
                    if previous_boundary is not None:
                        for previous in cycle_actions:
                            if "_elapsed_interval" in previous:
                                previous["_timeline_bounds"] = (cycle_start, previous_boundary)
                        cycle_start = previous_boundary
                    cycle_actions = []
                    cycle_cnt += 1
                    comp_cnt = 0
                    vent_cnt = 0
                if cycle_start is None and "_elapsed_interval" in group[0]:
                    cycle_start = min(action["_elapsed_interval"][0] for action in group)
                # Same-packet events share the cycle chosen once for the group.
                # Stable comp/vent presentation cannot itself create a cycle.
                for action in group:
                    if action["action_type"] == ACTION_TYPE_COMP:
                        comp_cnt += 1
                    else:
                        vent_cnt += 1
                    # Preserve the existing packet-list -> cycle-ordinal types.
                    action["cycle_cnt"] = cycle_cnt
                    action["compression_count"] = comp_cnt
                    action["ventilation_count"] = vent_cnt
                    if "_elapsed_interval" in action:
                        action["_timeline_bounds"] = (cycle_start, None)
                    cycle_actions.append(action)
                pre_action = ACTION_TYPE_VENT if any(action["action_type"] == ACTION_TYPE_VENT for action in group) else ACTION_TYPE_COMP
                previous_boundary = group[-1].get("_detected_timestamp")

        return partial_actions

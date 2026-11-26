from config.constants import ACTION_TYPE_VENT, ACTION_TYPE_COMP


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
            for action in partial_action["action_list"]:
                if action["action_type"] == ACTION_TYPE_COMP:
                    comp_cnt += 1
                else:
                    vent_cnt += 1

                if pre_action == ACTION_TYPE_VENT and action["action_type"] == ACTION_TYPE_COMP:
                    cycle_cnt += 1
                    comp_cnt = 1
                    vent_cnt = 0

                action["cycle_cnt"] = cycle_cnt
                action["compression_count"] = comp_cnt
                action["ventilation_count"] = vent_cnt
                if action["action_type"] == ACTION_TYPE_COMP and comp_cnt == 1:
                    action["prev_peak_sample"] = None  # AED로 새 파트가 된 첫 압박도 속도 미관측이다.

                pre_action = action["action_type"]

        return partial_actions

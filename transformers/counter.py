# 원본 hstm_v2 transformers/counter.py (V1 패리티 보존)
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

                # F-6: compression_count 이중성 — 병합 단계의 패킷별 리스트를 사이클 내 순번 int로
                #      덮어쓴다. whole_action_list와 dict를 공유하므로 파트에서 제외된(AED 겹침)
                #      액션은 리스트로 남고, chart_data가 두 타입을 흡수 — 기기 스펙 확인 대기.
                action["cycle_cnt"] = cycle_cnt
                action["compression_count"] = comp_cnt
                action["ventilation_count"] = vent_cnt

                pre_action = action["action_type"]

        return partial_actions

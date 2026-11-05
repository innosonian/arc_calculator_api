from config.constants import (
    EVENT_ID_START_COMP,
    EVENT_ID_END_COMP,
    EVENT_ID_START_VENT,
    EVENT_ID_END_VENT,
    EVENT_ID_START_AED,
    EVENT_ID_END_AED,
)
from config.enums import Actor
from services.config import Config
from services.http.schemas import VPEvent


class ActionDataInjector:
    def __init__(self, config: Config, vp_comp_count: int = 30, vp_vent_count: int = 2, rate_per_minute: int = 120):
        self.config = config
        self.vp_comp_count = vp_comp_count
        self.vp_vent_count = vp_vent_count
        self.rate_per_minute = rate_per_minute
        self.required_time_per_vent_ms = 2000

    def mark_virtual_partner_rtdata(self, action_list: list[dict], vp_event_list: list[VPEvent]) -> list[dict]:
        if not vp_event_list:
            return action_list

        # 1. 합친다.
        combined_list = action_list + vp_event_list
        # 2. 정렬 한다.
        sorted_list = sorted(combined_list, key=lambda d: d.get("last_timestamp") or d.get("timestamp"))

        # 3. 상태 변수 및 결과 리스트 초기화
        is_inside_vp = False
        result = []

        # 4. 정렬된 리스트를 순회
        for item in sorted_list:
            # 딕셔너리에 'type' 키가 있는지 확인하여 vp_action 구분
            item_type = item.get("event")

            if item_type in [EVENT_ID_START_COMP, EVENT_ID_START_VENT, EVENT_ID_START_AED]:
                # 4-1. START를 만나면 상태를 True로 바꾸고 임시 변수에 저장
                is_inside_vp = True
                continue
            elif item_type in [EVENT_ID_END_COMP, EVENT_ID_END_VENT, EVENT_ID_END_AED]:
                # 4-2. END 타입이면, False로 바꾸고 임시 변수에 저장
                is_inside_vp = False
                continue
            # 4-3. is_inside_vp가 True면 가상 파트너 표시를 한다.
            if is_inside_vp:
                item["actor"] = Actor.VIRTUAL_PARTNER

            result.append(item)

        return result

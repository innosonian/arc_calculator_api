# 원본 hstm_v2 data_handlers/vp_action.py (V1 패리티 보존)
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
    # 스펙 §4.5(I-1): 원본 생성자의 미사용 파라미터 4종(vp_comp_count=30, vp_vent_count=2,
    # rate_per_minute=120, required_time_per_vent_ms=2000)은 저장만 되고 소비처 0건(grep 재확인)이라 미이식.
    # 호출부(services/preparers.py)는 원래 config만 전달하므로 정합.
    def __init__(self, config: Config):
        self.config = config

    def mark_virtual_partner_rtdata(self, action_list: list[dict], vp_event_list: list[VPEvent]) -> list[dict]:
        # 주의: 파라미터 이름은 action_list이지만 실제 입력은 rtdata(패킷) 리스트다(원본 그대로).
        if not vp_event_list:
            return action_list

        # 1. 합친다.
        combined_list = action_list + vp_event_list
        # 2. 정렬 한다.
        # F-7: rtdata에는 last_timestamp가 없어 항상 timestamp 폴백. 동일 timestamp에서
        #      패킷이 VP 이벤트보다 앞에 오는 stable sort 경계 — 기기 스펙 확인 대기.
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

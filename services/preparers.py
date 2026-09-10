# 이식 출처: 원본 hstm_v2 services/preparers.py (67줄).
# 원본 대비 차이: make_prepared_data(소비처 grep 0건 dead 코드 — 스펙 §4.5)와
# 그 전용 import(PartDivider)만 미이식. 나머지 동작 동일.
from config.constants import ACTION_TYPE_COMP, ACTION_TYPE_VENT
from data_handlers.action_data import ActionDataPrepare
from data_handlers.data_parser import DataParser
from data_handlers.vp_action import ActionDataInjector
from services.config import Config
from services.http.schemas import ParsedData, VPEvent
from transformers.counter import CountMarker


def parse_data(cpr_byte_data: bytes, aed_byte_data: bytes, config: Config) -> ParsedData:
    parser = DataParser()
    rtdata_list = parser.parse_cpr_bytes(cpr_byte_data, config)
    aed_data_list = parser.parse_aed_bytes(aed_byte_data)

    return ParsedData(rtdata_list=rtdata_list, aed_data_list=aed_data_list)


def prepare_data(
    action_list: list[dict],
    aed_part_list: list[dict],
    config: Config,
) -> dict:
    # comp_count/vent_count는 세션 전체 액션 수 — 응답 action_count와 스펙 §5.3 null 정책의
    # 최소횟수 게이트(원본 lambda_handler._is_not_enough_attempt의 카운트 소스)가 이 값을 쓴다.
    comp_count = len([1 for a in action_list if a["action_type"] == ACTION_TYPE_COMP])
    vent_count = len([1 for a in action_list if a["action_type"] == ACTION_TYPE_VENT])

    # divide action as part
    # 실제로 파트별 액션 묶음이 됨
    partial_actions = config.divider.divide_action_part(action_list=action_list)

    return {
        "prepared_cpr_data": CountMarker().make_count(partial_actions["partial_action_list"]),  # 사이클 마킹
        "prepared_aed_data": aed_part_list,
        "whole_cpr_action_list": partial_actions["whole_action_list"],
        "comp_count": comp_count,
        "vent_count": vent_count,
    }


def make_pre_action_list(
    config: Config,
    parsed_data: dict,
    vp_event_list: list[VPEvent],
) -> (list[dict], list[dict]):
    # aed
    aed_part_list = config.divider.divide_aed_part(parsed_data["aed_data_list"])

    # rtdata중 aed와 겹치는 것이 있는 것은 표시를 남김
    marked_rtdata_list = config.divider.mark_aed_overlapped_rtdata(parsed_data["rtdata_list"], aed_part_list)

    # rtdata중 virtual partner가 진행한 패킷들에 표시를 남김
    vp_marked_rtdata_list = ActionDataInjector(config).mark_virtual_partner_rtdata(marked_rtdata_list, vp_event_list)

    # action 데이터 만듦
    action_list = ActionDataPrepare(config).get_action_list(vp_marked_rtdata_list)

    return action_list, aed_part_list

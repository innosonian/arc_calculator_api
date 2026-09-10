# 원본 hstm_v2 transformers/part_divider.py (V1 패리티 보존)
# 스펙 §4.5(I-1): 원본 생성자의 calculation_config 파라미터는 저장만 되고 클래스 내 소비처
# 0건(grep 재확인)이라 미이식. 호출부는 PartDivider()로 인스턴스화한다(원본 services/config.py:19 참조).
from config.constants import AED_SEPARATE_EVENTS, AED_EVENT_BEGIN_CPR


class PartDivider:
    def mark_aed_overlapped_rtdata(self, rtdata_list: list[dict], aed_part_list: list[dict]) -> list[dict]:
        aed_part_timestamp_list = [
            (a["aed_part_data"][0]["timestamp"], a["aed_part_data"][-1]["timestamp"]) for a in aed_part_list
        ]

        for rtdata in rtdata_list:
            for a_ts in aed_part_timestamp_list:
                if a_ts[0] < rtdata["timestamp"] < a_ts[1]:
                    rtdata["is_aed_overlapped"] = True

            if "is_aed_overlapped" not in rtdata:
                rtdata["is_aed_overlapped"] = False

        return rtdata_list

    def divide_action_part(self, action_list: list[dict]) -> dict:
        # TODO: 기준 결정 필요함
        rtdata_part_consume_types = ["action_under_aed", "partial_overlapped", "action_cover_aed"]
        part_list = []
        whole_action_list = []

        part_num = 1
        partial_data = []
        for action in action_list:
            overlap_type = self._get_overlap_type(action["is_aed_overlapped"])
            action["part_num"] = part_num
            action["overlap_type"] = overlap_type

            whole_action_list.append(action)

            if overlap_type is None:
                partial_data.append(action)
            elif overlap_type in rtdata_part_consume_types:
                if partial_data:
                    part_num += 1
                    part_list.append(partial_data)

                partial_data = []

        if partial_data:
            part_list.append(partial_data)

        partial_action_list = []
        part_num = 1
        for part in part_list:
            partial_action_list.append({"part_num": part_num, "action_list": part})
            part_num += 1

        return {
            "partial_action_list": partial_action_list,
            "whole_action_list": whole_action_list,
        }

    def _get_overlap_type(self, is_aed_overlapped: list[bool]) -> str | None:
        """
        action_under_aed: 액션 전체가 aed 파트 아래에 있음
        partial_overlapped: 앞 혹은 뒷부분 일부가 aed 파트와 겹침
        action_cover_aed: 액션 안에 aed 파트가 포함된
        None: 액션과 aed 파트가 겹치지 않음
        """
        length = len(is_aed_overlapped)
        if length == is_aed_overlapped.count(True):
            return "action_under_aed"
        elif is_aed_overlapped[0] != is_aed_overlapped[-1]:
            return "partial_overlapped"
        elif is_aed_overlapped[0] is False and is_aed_overlapped[-1] is False and is_aed_overlapped.count(True) >= 1:
            return "action_cover_aed"

        return None

    def divide_aed_part(self, aed_data_list: list[dict]) -> list[dict]:
        # F-8: separate 이벤트(16/21) 이후 BEGIN_CPR(19)까지의 이벤트와, 마지막 separate 이벤트
        #      없이 끝난 잔여 이벤트는 파트가 되지 않고 폐기된다 — 기기 스펙 확인 대기.
        aed_part_list = []
        aed_part_data = []
        part_cnt = 0

        is_dump_event = False
        for aed_data in aed_data_list:
            if aed_data["event"] == AED_EVENT_BEGIN_CPR:
                is_dump_event = False
                continue

            if is_dump_event:
                continue

            aed_part_data.append(aed_data)
            if aed_data["event"] in AED_SEPARATE_EVENTS:
                part_cnt += 1
                aed_part_list.append(
                    {
                        "part_cnt": part_cnt,
                        "aed_part_data": aed_part_data,
                    }
                )
                is_dump_event = True
                aed_part_data = []

        return aed_part_list

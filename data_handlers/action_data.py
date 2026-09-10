# 원본 hstm_v2 data_handlers/action_data.py (V1 패리티 보존)
from config.constants import (
    HANDSOFF_DEADTIME_MS,
    ACTION_TYPE_COMP,
    ACTION_TYPE_VENT,
    ACTION_TYPE_LAST,
    PACKET_MS,
    MINIMUM_VENT_VOLUME,
    MINIMUM_COMP_DEPTH,
    VENT_TAIL_MIN_PACKETS,
)
from config.enums import Actor
from services.config import Config


class ActionDataPrepare:
    def __init__(self, config: Config):
        self.config = config

    def get_action_list(self, rtdata_list: list[dict]) -> list[dict]:
        action_rtdata_list = self.generate_action_rtdata_list(rtdata_list)
        pre_action_list = self.make_pre_action_list(action_rtdata_list)
        return self.make_action_list(pre_action_list)

    def generate_action_rtdata_list(self, rtdata_list: list) -> list[dict]:
        # 원본 hstm_v2 action_data.py:24-99
        action_list = []
        if not rtdata_list:
            return action_list

        # 첫 패킷 값을 기준선으로 삼으면, 스트림이 첫 압박 카운트가 오른 직후부터
        # 시작할 때 그 압박이 액션으로 만들어지지 않는다. 0에서 시작해 복원한다.
        last_comp_cnt = 0
        last_vent_vol = 0

        buffer = []
        ventilation_speed = 0
        for i, rtdata in enumerate(rtdata_list):
            is_comp_cnt_changed = rtdata["compression_count"] != last_comp_cnt and rtdata["compression_count"] > 0
            vent_vol = max(rtdata["ventilation_volume"])
            vent_vol_min = min(rtdata["ventilation_volume"])

            # F-10: 환기 시작/신호 판정 하한 MINIMUM_VENT_VOLUME(=10) — 기기 스펙 확인 대기.
            is_vent_started = vent_vol_min < MINIMUM_VENT_VOLUME

            if vent_vol < MINIMUM_VENT_VOLUME:
                vent_vol = 0

            is_vent_cnt_changed = vent_vol < last_vent_vol and is_vent_started

            if is_comp_cnt_changed or is_vent_cnt_changed:
                action_type = ACTION_TYPE_VENT if is_vent_cnt_changed else ACTION_TYPE_COMP

                # ventilation speed 새로 구하기
                ventilation_speed = self._calc_ventilation_speed(buffer, action_type)

                last_comp_cnt = rtdata["compression_count"]

                if self._is_valid_action(action_type):
                    action_list.append(
                        {
                            "action_type": action_type,
                            # 스트림 첫 패킷에서 바로 확정되는 경우 버퍼가 비어 있다.
                            "rtdata_list": buffer or [rtdata],
                            "ventilation_speed": ventilation_speed,
                        }
                    )

                buffer = []
                last_vent_vol = 0
            else:
                last_vent_vol = vent_vol

            buffer.append(rtdata)

        # 하강 전에 스트림이 끊긴 마지막 호흡을 액션으로 확정한다.
        # 직전 액션 이후 관측이 짧으면 새 호흡이 아니라 직전 호흡의 잔여 흔들림이다.
        if (
            last_vent_vol >= MINIMUM_VENT_VOLUME
            and len(buffer) >= VENT_TAIL_MIN_PACKETS
            and self._is_valid_action(ACTION_TYPE_VENT)
        ):
            action_list.append(
                {
                    "action_type": ACTION_TYPE_VENT,
                    "rtdata_list": buffer,
                    "ventilation_speed": self._calc_ventilation_speed(buffer, ACTION_TYPE_VENT),
                }
            )
            # 뒤따르는 LAST 액션이 빈 리스트를 받지 않도록 마지막 패킷 하나를 남긴다.
            buffer = [rtdata_list[-1]]

        # add last action for get last chest compression action rate
        action_list.append(
            {
                "action_type": ACTION_TYPE_LAST,
                "rtdata_list": buffer,
                "ventilation_speed": ventilation_speed,
            }
        )

        return action_list

    def _is_valid_action(self, action_type: str) -> bool:
        return all(
            [
                not (self.config.calculation_config.is_vent_only() and action_type == ACTION_TYPE_COMP),
                not (self.config.calculation_config.is_cco() and action_type == ACTION_TYPE_VENT),
            ]
        )

    def _is_ventilation(self, action_type: str) -> bool:
        return True if action_type == ACTION_TYPE_VENT else False

    # TODO :: 반드시 리팩토링이 필요
    def _calc_ventilation_speed(self, buffer: list[dict], action_type: str) -> int:
        if not self.config.calculation_config.is_infant() or not self._is_ventilation(action_type):
            return 0
        start_ventilation = None
        end_ventilation = None
        max_ventilation_volume = 0
        for rtdata in buffer:
            ventilation_volume = max(rtdata["ventilation_volume"])
            if not ventilation_volume:
                continue

            if start_ventilation is None:
                start_ventilation = rtdata
                end_ventilation = rtdata
                max_ventilation_volume = ventilation_volume

            if max_ventilation_volume < ventilation_volume:
                max_ventilation_volume = ventilation_volume
                end_ventilation = rtdata

        if start_ventilation is None or end_ventilation is None:
            # F-3: 기기 raw vent speed 단위 ×PACKET_MS(50ms) 환산 폴백 — 기기 스펙 확인 대기.
            return buffer[0]["ventilation_speed"] * PACKET_MS

        return end_ventilation["timestamp"] - start_ventilation["timestamp"]

    def make_pre_action_list(self, action_rtdata_list: list[dict]) -> list[dict]:
        pre_action_list = []
        for action_rtdata in action_rtdata_list:
            merged_data = {
                "compression_depth": [],
                "compression_rate": [],
                "compression_count": [],
                "hand_position": [],
                "ventilation_volume": [],
                # "ventilation_speed": [],
                "ventilation_count": [],
                "first_timestamp": 0,
                "last_timestamp": 0,
                "is_aed_overlapped": [],
                "packet_signal": [],
            }
            for rtdata in action_rtdata["rtdata_list"]:
                merged_data = self._merge_data(merged_data, rtdata)

            merged_data["action_type"] = action_rtdata["action_type"]
            merged_data["ventilation_speed"] = action_rtdata["ventilation_speed"]
            # 앱이 vp_event로 VP 구간을 정확히 전달하므로, 액션 actor를 "마지막 패킷"이나
            # "버퍼 전체 다수"가 아니라 "실제 압박/환기가 일어난(신호 있는) 패킷"의 VP 멤버십으로
            # 확정한다. 버퍼가 VP 경계에 걸쳐 idle/전환 패킷이 섞여도 실제 동작 주체로 귀속된다.
            merged_data["actor"] = self._primary_actor(merged_data.pop("packet_signal"), merged_data["action_type"])
            pre_action_list.append(merged_data)

        return pre_action_list

    def _merge_data(self, merged_data: dict, rtdata: dict) -> dict:
        merged_data["compression_depth"].extend(rtdata["compression_depth"])
        merged_data["compression_rate"].append(rtdata["compression_rate"])
        merged_data["compression_count"].append(rtdata["compression_count"])
        merged_data["hand_position"].append(rtdata["hand_position"])
        merged_data["ventilation_volume"].extend(rtdata["ventilation_volume"])
        # merged_data["ventilation_speed"].append(rtdata["ventilation_speed"])
        merged_data["ventilation_count"].append(rtdata["ventilation_count"])
        merged_data["last_timestamp"] = rtdata["timestamp"]
        merged_data["is_aed_overlapped"].append(rtdata["is_aed_overlapped"])
        merged_data["packet_signal"].append(
            (rtdata["actor"], max(rtdata["compression_depth"]), max(rtdata["ventilation_volume"]))
        )

        if merged_data["first_timestamp"] == 0:
            merged_data["first_timestamp"] = rtdata["timestamp"]

        return merged_data

    def _primary_actor(self, packet_signal: list, action_type: str) -> Actor:
        # 액션 종류에 따라 "신호가 있는(실제 압박/환기가 일어난) 패킷"만 골라 그 패킷들의
        # VP 멤버십 다수로 actor를 정한다. 신호 패킷이 없으면 전체 패킷으로 폴백.
        # F-7: 신호 판정 경계 비대칭(comp는 depth > MINIMUM_COMP_DEPTH strict,
        #      vent는 vol >= MINIMUM_VENT_VOLUME inclusive) — 기기 스펙 확인 대기.
        if action_type == ACTION_TYPE_COMP:
            actors = [actor for actor, depth, _ in packet_signal if depth > MINIMUM_COMP_DEPTH]
        elif action_type == ACTION_TYPE_VENT:
            actors = [actor for actor, _, vol in packet_signal if vol >= MINIMUM_VENT_VOLUME]
        else:
            actors = []
        if not actors:
            actors = [actor for actor, _, _ in packet_signal]
        if not actors:
            return Actor.REAL_PERSON
        vp = sum(1 for actor in actors if actor == Actor.VIRTUAL_PARTNER)
        return Actor.VIRTUAL_PARTNER if vp > len(actors) - vp else Actor.REAL_PERSON

    def make_action_list(self, pre_action_list: list[dict]) -> list[dict]:
        # F-2: compression_rate 한 칸 시프트(각 액션의 rate를 다음 액션의 값으로 교체.
        #      LAST 센티널 덕분에 실제 마지막 comp도 rate를 받는다) — 기기 스펙 확인 대기.
        for i, pre_action in enumerate(pre_action_list):
            try:
                next_action = pre_action_list[i + 1]
            except IndexError:
                continue

            pre_action["compression_rate"] = next_action["compression_rate"]

        # remove last action data
        pre_action_list = self._remove_last_action_from(pre_action_list)

        action_list = self._calc_handsoff_ms(pre_action_list)

        return action_list

    def _remove_last_action_from(self, pre_action_list: list[dict]) -> list[dict]:
        if not pre_action_list:
            return pre_action_list

        if pre_action_list[-1]["action_type"] == ACTION_TYPE_LAST:
            pre_action_list.pop()

        return pre_action_list

    def _calc_handsoff_ms(self, pre_action_list: list[dict]) -> list[dict]:
        # F-1: comp 액션에만 HANDSOFF_DEADTIME_MS(1000ms) 공제, vent 액션은 전체 시간이
        #      handsoff — 기기 스펙 확인 대기.
        for pre_action in pre_action_list:
            total_action_ms = self._get_total_action_ms(pre_action)
            handsoff_ms = total_action_ms - (
                HANDSOFF_DEADTIME_MS if pre_action["action_type"] == ACTION_TYPE_COMP else 0
            )

            pre_action["total_action_ms"] = total_action_ms
            pre_action["handsoff_ms"] = handsoff_ms if handsoff_ms > 0 else 0

        return pre_action_list

    def _get_total_action_ms(self, pre_action: dict) -> int:
        return pre_action["last_timestamp"] - pre_action["first_timestamp"]

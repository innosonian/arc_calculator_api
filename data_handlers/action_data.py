from calculators.waveform import CompressionSegmenter, compression_events
from config.constants import (
    HANDSOFF_DEADTIME_MS,
    ACTION_TYPE_COMP,
    ACTION_TYPE_AED,
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
        action_rtdata_list = self._split_aed_tail(self.generate_action_rtdata_list(rtdata_list))
        pre_action_list = self.make_pre_action_list(action_rtdata_list)
        return self.make_action_list(pre_action_list)

    def _split_aed_tail(self, action_rtdata_list: list[dict]) -> list[dict]:
        # AED 창 뒤 첫 압박의 하강 구간만 복원하고 앞부분은 기존 파트 경계로 소비한다.
        # shortcut: 압박만 처리한다. 환기 액션의 AED 꼬리는 별도 결정 뒤 확장한다.
        result = []
        for action in action_rtdata_list:
            records = action["rtdata_list"]
            flags = [record["is_aed_overlapped"] for record in records]
            if action["action_type"] != ACTION_TYPE_COMP or True not in flags or flags[-1]:
                result.append(action)
                continue
            tail_at = len(flags) - flags[::-1].index(True)
            pmax = [max(record["compression_depth"]) for record in records[tail_at:]]
            peak = pmax.index(max(pmax))
            if pmax[peak] < MINIMUM_COMP_DEPTH:
                result.append(action)
                continue
            start = peak
            while start > 0 and MINIMUM_COMP_DEPTH <= pmax[start - 1] < pmax[start]:
                start -= 1
            cut = tail_at + start
            result.append({**action, "action_type": ACTION_TYPE_AED, "rtdata_list": records[:cut]})
            result.append({**action, "rtdata_list": records[cut:]})
        return result

    def generate_action_rtdata_list(self, rtdata_list: list) -> list[dict]:
        action_list = []
        if not rtdata_list:
            return action_list

        segmenter = CompressionSegmenter()
        last_vent_vol = 0

        buffer = []
        ventilation_speed = 0
        for i, rtdata in enumerate(rtdata_list):
            rtdata["record_index"] = i
            peak_record = segmenter.feed(i, max(rtdata["compression_depth"]))
            vent_vol = max(rtdata["ventilation_volume"])
            vent_vol_min = min(rtdata["ventilation_volume"])

            is_vent_started = vent_vol_min < MINIMUM_VENT_VOLUME

            if vent_vol < MINIMUM_VENT_VOLUME:
                vent_vol = 0

            is_vent_cnt_changed = vent_vol < last_vent_vol and is_vent_started

            if peak_record is not None or is_vent_cnt_changed:
                comp_actor_signal = None
                if is_vent_cnt_changed:
                    ventilation_speed = self._calc_ventilation_speed(buffer, ACTION_TYPE_VENT)
                if is_vent_cnt_changed and self._is_valid_action(ACTION_TYPE_VENT):
                    if peak_record is not None and self._is_valid_action(ACTION_TYPE_COMP):
                        comp_actor_signal = [
                            (record["actor"], max(record["compression_depth"]), max(record["ventilation_volume"]))
                            for record in buffer
                        ]
                    action_list.append(
                        {
                            "action_type": ACTION_TYPE_VENT,
                            "rtdata_list": buffer or [rtdata],
                            "ventilation_speed": ventilation_speed,
                        }
                    )
                    # 환기가 시간 창을 소유하므로 동시 확정 압박은 [i,i) 창을 쓴다.
                    buffer = []

                if peak_record is not None and self._is_valid_action(ACTION_TYPE_COMP):
                    compression_action = {
                        "action_type": ACTION_TYPE_COMP, "rtdata_list": buffer,
                        "ventilation_speed": 0, "peak_record": peak_record, "count_record": i,
                    }
                    if comp_actor_signal is not None:
                        # 시간·HP 창은 비워도 기존 신호 창의 actor 다수결은 유지한다.
                        compression_action["actor_signal"] = comp_actor_signal
                    action_list.append(compression_action)
                buffer = []
                last_vent_vol = 0
            else:
                last_vent_vol = vent_vol

            # 확정 Record는 다음 버퍼의 선두다: [직전 count_record, 이번 count_record).
            buffer.append(rtdata)

        peak_record = segmenter.finish()
        if peak_record is not None and self._is_valid_action(ACTION_TYPE_COMP):
            action_list.append({
                "action_type": ACTION_TYPE_COMP, "rtdata_list": buffer[:-1],
                "ventilation_speed": 0, "peak_record": peak_record, "count_record": len(rtdata_list) - 1,
            })

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
        elif peak_record is not None and self._is_valid_action(ACTION_TYPE_COMP):
            buffer = [rtdata_list[-1]]

        # LAST는 마지막 압박의 손위치 이완 표본을 보존한다.
        action_list.append(
            {
                "action_type": ACTION_TYPE_LAST,
                "rtdata_list": buffer or [rtdata_list[-1]],
                "ventilation_speed": ventilation_speed,
            }
        )

        # 분절은 위에서 한 번만 수행한다. recoil은 actor/사이클 경계 밖의 다음 정점까지 관측한다.
        events = compression_events([record["compression_depth"] for record in rtdata_list],
                                    segmenter.peak_packet_indexes)
        by_peak = {event.peak_packet_index: event for event in events}
        for action in action_list:
            if action["action_type"] == ACTION_TYPE_COMP:
                event = by_peak[action["peak_record"]]
                action.update(peak_offset=event.peak_sample_offset, depth_raw=event.peak_depth,
                              recoil_raw=event.recoil_depth,
                              peak_timestamp=rtdata_list[event.peak_packet_index]["timestamp"])
                if not action["rtdata_list"]:
                    action["empty_window_record"] = rtdata_list[action["count_record"]]

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
            return buffer[0]["ventilation_speed"] * PACKET_MS

        return end_ventilation["timestamp"] - start_ventilation["timestamp"]

    def make_pre_action_list(self, action_rtdata_list: list[dict]) -> list[dict]:
        pre_action_list = []
        for action_rtdata in action_rtdata_list:
            merged_data = {
                "compression_depth": [],
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
            for key in ("peak_record", "peak_offset", "count_record", "depth_raw", "recoil_raw", "peak_timestamp"):
                if key in action_rtdata:
                    merged_data[key] = action_rtdata[key]
            records = action_rtdata["rtdata_list"]
            if not records and "empty_window_record" in action_rtdata:
                # 환기와 동시 확정되거나 마지막 환기 경계 직후 finish된 압박은 시간 0 창이다.
                boundary = action_rtdata["empty_window_record"]
                merged_data.update(action_start=action_rtdata["count_record"],
                                   action_end=action_rtdata["count_record"],
                                   first_timestamp=boundary["timestamp"], last_timestamp=boundary["timestamp"],
                                   is_aed_overlapped=[boundary["is_aed_overlapped"]],
                                   signal_record=action_rtdata["peak_record"])
            if records and "record_index" in records[0]:
                merged_data["action_start"] = records[0]["record_index"]
                merged_data["action_end"] = records[-1]["record_index"] + 1
                signal = "ventilation_volume" if action_rtdata["action_type"] == ACTION_TYPE_VENT else "compression_depth"
                if action_rtdata["action_type"] == ACTION_TYPE_COMP:
                    # 직전 압박의 이완 꼬리가 현재 정점보다 깊을 수 있어 최저점 이후에서 찾는다.
                    records = records[min(range(len(records)), key=lambda i: max(records[i]["compression_depth"])):]
                merged_data["signal_record"] = max(records, key=lambda record: max(record[signal]))["record_index"]
            # 앱이 vp_event로 VP 구간을 정확히 전달하므로, 액션 actor를 "마지막 패킷"이나
            # "버퍼 전체 다수"가 아니라 "실제 압박/환기가 일어난(신호 있는) 패킷"의 VP 멤버십으로
            # 확정한다. 버퍼가 VP 경계에 걸쳐 idle/전환 패킷이 섞여도 실제 동작 주체로 귀속된다.
            merged_data["actor"] = self._primary_actor(
                action_rtdata.get("actor_signal", merged_data.pop("packet_signal")), merged_data["action_type"]
            )
            pre_action_list.append(merged_data)

        return pre_action_list

    def _merge_data(self, merged_data: dict, rtdata: dict) -> dict:
        merged_data["compression_depth"].extend(rtdata["compression_depth"])
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
        prev_peak_sample = None
        for i, pre_action in enumerate(pre_action_list):
            next_hand_position = []
            try:
                next_action = pre_action_list[i + 1]
            except IndexError:
                next_action = None

            if next_action is not None:
                next_hand_position = next_action["hand_position"]

            # 압박 위치 표본을 이번 압박의 하강 구간과 이완 구간으로 재구성한다. 다음 액션의
            # 원본 버퍼를 읽으므로 앞에서부터 순서대로 처리해야 한다.
            if pre_action["action_type"] == ACTION_TYPE_COMP:
                pre_action["prev_peak_sample"] = prev_peak_sample
                prev_peak_sample = pre_action["peak_record"] * 10 + pre_action["peak_offset"]
                pre_action["hand_position"] = self._own_hand_position(pre_action["hand_position"], next_hand_position)
            else:
                prev_peak_sample = None

        # remove last action data
        pre_action_list = self._remove_last_action_from(pre_action_list)

        action_list = self._calc_handsoff_ms(pre_action_list)

        return action_list

    @staticmethod
    def _own_hand_position(own: list[int], following: list[int]) -> list[int]:
        """압박 액션의 압박 위치 표본을 이번 압박의 하강 구간과 이완 구간으로 재구성한다.

        정점 대비 30 raw 하강 Record는 다음 버퍼의 선두다. 그래서
        압박 N의 나머지 이완 구간(확정 Record부터 코드가 0이 되기 전까지)은 액션 N+1 버퍼에 포함된다.
        마네킨은 좌우 코드(4/8)를 이 이완 구간 1~2패킷에만 보내는 경우가 있어, 자기 버퍼만 사용하면
        판정이 누락되거나 직전 압박의 판정이 섞인다.

        - 자기 버퍼에서는 마지막 0 뒤의 구간(이번 압박의 하강 구간)을 사용한다. 버퍼가 0으로 끝나면
          이완이 버퍼 안에서 끝난 것이므로 끝의 0을 제외하고 그 앞의 마지막 압박 구간을 사용한다.
        - 다음 버퍼에서는 첫 0 앞의 구간(이번 압박의 이완 구간)을 사용한다. 자기 버퍼의 마지막 코드와
          다음 버퍼의 첫 코드가 모두 0이 아니어서 압박이 경계를 넘어 이어질 때만 사용한다. 다음 버퍼에
          0이 없으면 이완 구간과 다음 하강 구간을 구분할 수 없으므로 사용하지 않는다.
        - 다음 액션이 환기 액션이거나 세션 마지막 버퍼여도 같은 규칙을 적용한다.
        """
        end = len(own)
        while end and own[end - 1] == 0:
            end -= 1
        pressed = own[:end]
        if 0 in pressed:
            pressed = pressed[len(pressed) - pressed[::-1].index(0):]

        crosses_boundary = bool(own) and own[-1] != 0 and bool(following) and following[0] != 0
        release = following[: following.index(0)] if crosses_boundary and 0 in following else []

        return pressed + release

    def _remove_last_action_from(self, pre_action_list: list[dict]) -> list[dict]:
        if not pre_action_list:
            return pre_action_list

        if pre_action_list[-1]["action_type"] == ACTION_TYPE_LAST:
            pre_action_list.pop()

        return pre_action_list

    def _calc_handsoff_ms(self, pre_action_list: list[dict]) -> list[dict]:
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

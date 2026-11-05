from config.borders import trapezium_get_point, fall_linear_get_point, dart_get_point, BaseBorder
from config.constants import ACTION_TYPE_COMP, ACTION_TYPE_VENT
from models.action import ActionWithScore
from services.config import Config
from util.custom_math import custom_round


class ActionEvaluator:
    def __init__(self, config: Config):
        self.config = config

    def evaluate_action(self, action_list: list[dict]) -> list[ActionWithScore]:
        return [
            self._evaluate(action, self.config.border, action["actor"])
            for action in action_list
            if action["action_type"]
        ]

    def _evaluate(self, action: dict, border: BaseBorder, actor: str) -> ActionWithScore | None:
        if action["action_type"] == ACTION_TYPE_COMP:
            return self._evaluate_comp(action, border, actor)
        elif action["action_type"] == ACTION_TYPE_VENT:
            return self._evaluate_vent(action, border, actor)

        return None

    def _evaluate_comp(self, action: dict, border: BaseBorder, actor: str) -> ActionWithScore:
        # make digit
        depth_digit = custom_round(action.get("depth_raw", max(action["compression_depth"], default=0)) / 2)
        recoil_digit = custom_round(action.get("recoil_raw", min(action["compression_depth"], default=0)) / 2)

        prev_peak_sample = action.get("prev_peak_sample")
        comp_rate_digit = 0
        if prev_peak_sample is not None:
            peak_sample = action["peak_record"] * 10 + action["peak_offset"]
            comp_rate_digit = custom_round(60000 / ((peak_sample - prev_peak_sample) * 5))

        if self.config.condition.get("target") == "infant":
            hand_position_spot = self.get_finger_position_spot(action["hand_position"])
        else:
            hand_position_spot = self.get_hand_position_spot(action["hand_position"])

        # calc score
        depth_score = trapezium_get_point(border, "comp_depth", depth_digit)
        recoil_score = fall_linear_get_point(border, "recoil", recoil_digit)
        comp_rate_score = trapezium_get_point(border, "comp_rate", comp_rate_digit)
        hand_position_score = dart_get_point(border, "hand_position", hand_position_spot)

        hand_position_score.update({"value": hand_position_spot})
        depth_score.update({"value": depth_digit})
        recoil_score.update({"value": recoil_digit})
        comp_rate_score.update({"value": comp_rate_digit})

        # make result
        evaluation = {
            "comp_depth": depth_score,
            "comp_rate": comp_rate_score,
            "recoil": recoil_score,
            "hand_position": hand_position_score,
        }
        return ActionWithScore(action_type=ACTION_TYPE_COMP, action_data=action, score=evaluation, actor=actor)

    def _evaluate_vent(self, action: dict, border: BaseBorder, actor: str) -> ActionWithScore:
        total_action_ms = action["total_action_ms"]
        # make digit
        vent_vol_digit = int(max(action["ventilation_volume"], default=0))

        try:
            vent_speed_digit = action["ventilation_speed"]
        except KeyError:
            vent_speed_digit = 0

        # calc score
        vent_vol_score = trapezium_get_point(border, "vent_vol", vent_vol_digit)
        vent_speed_score = trapezium_get_point(border, "vent_speed", vent_speed_digit)
        if total_action_ms == 0:
            vent_rate_digit = 0
            vent_rate_score = {"grade": 0, "criterion": "low"}
        else:
            vent_rate_digit = (60 * 1000) / total_action_ms if total_action_ms > 0 else 0
            vent_rate_score = trapezium_get_point(border, self._get_vent_rate_key(), vent_rate_digit)

        vent_vol_score.update({"value": vent_vol_digit})
        vent_speed_score.update({"value": vent_speed_digit})
        vent_rate_score.update({"value": vent_rate_digit})

        # make result
        evaluation = {
            "vent_vol": vent_vol_score,
            "vent_rate": vent_rate_score,
        }
        if self.config.calculation_config.is_infant():
            evaluation["vent_speed"] = vent_speed_score

        return ActionWithScore(action_type=ACTION_TYPE_VENT, action_data=action, score=evaluation, actor=actor)

    def _get_vent_rate_key(self) -> str:
        if self.config.calculation_config.is_cpr():
            return "vent_rate_1p"

        return "vent_only_rate"

    def get_hand_position_spot(self, hand_position_list: list[int]) -> str:
        # 표본은 이번 압박의 하강 구간과 이완 구간이다(ActionDataPrepare._own_hand_position 참고).
        # 좌우 코드가 이완 구간에만 포함되는 경우가 있으므로 표본 전체를 대상으로 판정한다.
        # 복부 코드(2)가 하나라도 있으면 down, 좌우 코드(4/8)가 있으면 많은 쪽(동수는 right), 그 외는 center다.
        if 2 in hand_position_list:
            return "down"

        num_of_left = hand_position_list.count(4)
        num_of_right = hand_position_list.count(8)
        if num_of_left or num_of_right:
            return "left" if num_of_left > num_of_right else "right"

        return "center"

    def get_finger_position_spot(self, hand_position_list: list[int]) -> str:
        # infant 마네킨은 압박 위치 코드의 상위 비트를 플래그로 설정해 보내는 경우가 있다. 하위 4비트가
        # 성인과 같은 위치 코드이므로(16은 판정 없음, 17은 정위치, 18은 복부) 하위 4비트만 남겨 성인 판정
        # 규칙을 적용한다. 16처럼 하위 4비트가 0인 코드는 이 판정 단계에서 비압박으로 처리된다. 앱(pos & 0x0f)과
        # V1(상위 4비트 무시)도 같은 방식이다. 표본 재구성(ActionDataPrepare._own_hand_position)은 마스크 전
        # 원본 코드로 0을 찾아 구간을 나누므로, 16은 재구성 단계에서는 압박 구간에 포함된다.
        # infant 마네킨은 좌우 코드(4/8)를 보내지 않지만 규칙은 성인과 같게 유지한다.
        return self.get_hand_position_spot([h & 0xF for h in hand_position_list])

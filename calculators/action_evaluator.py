# [provenance] 원본 hstm_v2 calculators/action_evaluator.py:1-146 동일 이식 (V1 패리티 보존).
# 계산식·경계 비교·반올림·처리 순서 일체 무변경. 주석만 추가(provenance·기기 스펙 확인 대기).
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
        depth_digit = custom_round(max(action["compression_depth"]) / 2)
        recoil_digit = custom_round(min(action["compression_depth"]) / 2)

        try:
            comp_rate_list = action["compression_rate"]
            comp_rate_digit = comp_rate_list[0] if len(comp_rate_list) != 0 else 0
        except KeyError:
            comp_rate_digit = 0

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
        # Rate retains the full event interval even when aggregate elapsed time
        # overlaps a compression and is counted only once in the timeline.
        total_action_ms = action.get("_rate_duration_ms", action["total_action_ms"])
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

    # [기기 스펙 확인 대기] hand_position 값 도메인(F-5): 단일 플래그 {0, 2, 4, 8}만 처리하며
    # 비트 조합(예: 6, 12)이나 1은 카운트되지 않는다(무시). infant 쪽은 & 0xF 비트마스크를 쓰는 것과
    # 대조적 — 성인 hand_position이 항상 단일 플래그라는 프로토콜 보장은 기기 스펙 확인 대기.
    # 원본 hstm_v2 action_evaluator.py:103-130 동작 그대로 보존.
    # 주의: 아래 reverse()는 입력 리스트(원본 action dict)를 제자리 변형하는 부작용이 있다 — V1 패리티 보존.
    def get_hand_position_spot(self, hand_position_list: list[int]) -> str:
        num_of_down = 0
        num_of_left = 0
        num_of_right = 0

        hand_position_list.reverse()
        for h in hand_position_list:
            if h == 0:
                break

            match h:
                case 2:
                    num_of_down += 1
                case 4:
                    num_of_left += 1
                case 8:
                    num_of_right += 1

        if num_of_down:
            return "down"

        if num_of_left or num_of_right:
            if num_of_left > num_of_right:
                return "left"
            else:
                return "right"

        return "center"

    # [기기 스펙 확인 대기] infant 손가락 위치 판정(F-5): h & 0xF != 1 이면 오위치로 집계하는
    # 비트마스크 판정 — 값 도메인·비트 의미는 기기 스펙 확인 대기. center/down 2단계 판정.
    # 원본 hstm_v2 action_evaluator.py:132-146 동작 그대로 보존(reverse 부작용 포함).
    def get_finger_position_spot(self, hand_position_list: list[int]) -> str:
        num_of_wrong_position = 0

        hand_position_list.reverse()
        for h in hand_position_list:
            if h == 0:
                break

            if h & 0xF != 1:
                num_of_wrong_position += 1

        if num_of_wrong_position:
            return "down"

        return "center"

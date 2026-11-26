from calculators import hand_position
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

        # HP_V2 (D142); the board (D143) arrives as an upload part; main.run_calculator sets it on the config.
        hand_position_spot = hand_position.VALUES.get(hand_position.classify(
            action["hand_position"], action.get("hand_position_records", ()), action.get("peak_record"),
            getattr(self.config, "hand_position_board", hand_position.UNKNOWN_BOARD),
            self.config.condition.get("target") == "infant",
        ))

        # calc score
        depth_score = trapezium_get_point(border, "comp_depth", depth_digit)
        recoil_score = fall_linear_get_point(border, "recoil", recoil_digit)
        comp_rate_score = trapezium_get_point(border, "comp_rate", comp_rate_digit)

        depth_score.update({"value": depth_digit})
        recoil_score.update({"value": recoil_digit})
        comp_rate_score.update({"value": comp_rate_digit})

        # make result
        evaluation = {
            "comp_depth": depth_score,
            "comp_rate": comp_rate_score,
            "recoil": recoil_score,
        }
        # A compression without a hand-position sample is not scored: the key is omitted (D144).
        if hand_position_spot is not None:
            hand_position_score = dart_get_point(
                border, "hand_position", hand_position.SCORE_ROWS.get(hand_position_spot, hand_position_spot))
            hand_position_score.update({"value": hand_position_spot})
            evaluation["hand_position"] = hand_position_score
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

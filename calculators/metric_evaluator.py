from calculators.cycle_evaluator import CycleWithScore
from calculators.action_timeline import timeline_totals
from config.calculation_config import BaseCalculationConfig
from config.constants import ACTION_TYPE_COMP, ACTION_TYPE_VENT
from config.enums import Actor
from util.custom_math import custom_round


class MetricEvaluator:
    RATIO = 100

    def __init__(self, calculation_config: BaseCalculationConfig):
        self.calculation_config = calculation_config

        self.metric = {
            "CompressionDepth": {
                "%_TooShallow": 0,
                "%_Good": 0,
                "%_TooDeep": 0,
                "n_TooShallow": 0,
                "n_Good": 0,
                "n_TooDeep": 0,
            },
            "Recoil": {
                "%_Incomplete": 0,
                "%_Good": 0,
                "n_Incomplete": 0,
                "n_Good": 0,
            },
            "CompressionRate": {
                "%_TooSlow": 0,
                "%_Good": 0,
                "%_TooFast": 0,
                "n_TooSlow": 0,
                "n_Good": 0,
                "n_TooFast": 0,
            },
            "ScoreOfCCF": 0,
            "HandPosition": {
                "%_IncorrectLR": 0,
                "%_Good": 0,
                "%_IncorrectStomach": 0,
                "n_IncorrectLR": 0,
                "n_Good": 0,
                "n_IncorrectStomach": 0,
            },
            "CompressionNo": {
                "%_TooFew": 0,
                "%_Good": 0,
                "%_TooMany": 0,
                "n_TooFew": 0,
                "n_Good": 0,
                "n_TooMany": 0,
            },
            "CompressionCount": {
                "%_TooFew": 0,
                "%_Good": 0,
                "%_TooMany": 0,
                "n_TooFew": 0,
                "n_Good": 0,
                "n_TooMany": 0,
            },
            "VentilationVolume": {
                "%_TooLittle": 0,
                "%_Good": 0,
                "%_TooMuch": 0,
                "n_TooLittle": 0,
                "n_Good": 0,
                "n_TooMuch": 0,
            },
            "VentilationRate": {
                "%_InFrequently": 0,
                "%_Good": 0,
                "%_TooFrequently": 0,
                "n_InFrequently": 0,
                "n_Good": 0,
                "n_TooFrequently": 0,
            },
            "VentilationCount": {
                "%_TooMany": 0,
                "%_Good": 0,
                "%_TooFew": 0,
                "n_TooMany": 0,
                "n_Good": 0,
                "n_TooFew": 0,
            },
            "VentilationSpeed": {
                "%_TooSlow": 0,
                "%_Good": 0,
                "%_TooFast": 0,
                "n_TooSlow": 0,
                "n_Good": 0,
                "n_TooFast": 0,
            },
            "CompressionActionNumber": 0,
            "CompressionCycleNumber": 0,
            "VentilationCycleNumber": 0,
            "VentilationActionNumber": 0,
            "TotalEventTime": 0,
            "TotalHandsOffTime": 0,
            "CCF": {"%_CCF": 0, "score": 0, "sum_score": 0, "ccf_count": 0, "sum_ccf": 0},
            "AvgCompressionRate": 0,
            "AvgCompressionDepth": 0,
            "AvgVentilationVolume": 0,
            "AvgVentilationSpeed": 0,
            "HandsOffTimeSec": 0,
        }

        # ResultSummary 실측 요약용 누적기. 판정 분포(n_/%_)와 별개로 원시값 평균을 만든다.
        self._sum_comp_rate = 0
        self._comp_rate_count = 0
        self._sum_comp_depth = 0
        self._comp_depth_count = 0
        self._sum_vent_vol = 0
        self._sum_vent_speed = 0
        self._sum_cycle_handsoff_ms = 0
        self._handsoff_cycle_count = 0

    def evaluate(self, part_with_score_list):
        for part in part_with_score_list:
            self.collect_metrics(part["result"]["action_with_score_list"])
            self.collect_cycle_metrics(part["result"]["cycle_with_score_list"])

        self.calculate_metrics()

        return self.metric

    def collect_cycle_metrics(self, cycle_with_score_list: list[CycleWithScore]) -> None:
        for cycle_with_score in cycle_with_score_list:
            self._collect_comp_event_metrics(cycle_with_score)

            comp_no_score = cycle_with_score.evaluate_result_comp_no
            vent_cnt_score = cycle_with_score.evaluate_result_vent_cnt

            if self.calculation_config.is_cpr():
                if comp_no_score["criterion"] == "good":
                    self.metric["CompressionNo"]["n_Good"] += 1
                    self.metric["CompressionCount"]["n_Good"] += 1
                elif comp_no_score["criterion"] == "low":
                    self.metric["CompressionNo"]["n_TooFew"] += 1
                    self.metric["CompressionCount"]["n_TooFew"] += 1
                elif comp_no_score["criterion"] == "high":
                    self.metric["CompressionNo"]["n_TooMany"] += 1
                    self.metric["CompressionCount"]["n_TooMany"] += 1

            if vent_cnt_score["criterion"] == "good":
                self.metric["VentilationCount"]["n_Good"] += 1
            elif vent_cnt_score["criterion"] == "too_few":
                self.metric["VentilationCount"]["n_TooFew"] += 1
            elif vent_cnt_score["criterion"] == "too_many":
                self.metric["VentilationCount"]["n_TooMany"] += 1

            # CPR의 VentilationRate 분포는 사이클 단위 분당 환기율 판정으로 집계한다(V1과 동일).
            # 이 판정은 CPR 본 사이클에서만 세팅되고, ventilation only는 액션 단위(_collect_vent_metrics)로 쌓는다.
            vent_rate_criterion = cycle_with_score.evaluate_result_vent_rate["criterion"]
            if vent_rate_criterion == "good":
                self.metric["VentilationRate"]["n_Good"] += 1
            elif vent_rate_criterion == "low":
                self.metric["VentilationRate"]["n_InFrequently"] += 1
            elif vent_rate_criterion == "high":
                self.metric["VentilationRate"]["n_TooFrequently"] += 1

            if cycle_with_score.can_calc_comp():
                self.metric["CompressionCycleNumber"] += 1
            if cycle_with_score.can_calc_vent() and not cycle_with_score.did_rescue_vent:
                self.metric["VentilationCycleNumber"] += 1

            if cycle_with_score.ccf is not None:
                self.metric["CCF"]["sum_score"] += cycle_with_score.score_ccf
                self.metric["CCF"]["sum_ccf"] += cycle_with_score.ccf
                self.metric["CCF"]["ccf_count"] += 1

            # hands-off 사이클 평균용. rescue vent 사이클은 본 사이클이 아니므로 제외(V1의 pre-cycle 제외와 정렬).
            if not cycle_with_score.did_rescue_vent:
                self._sum_cycle_handsoff_ms += cycle_with_score.buffer_assigned_handsoff
                self._handsoff_cycle_count += 1

    def collect_metrics(self, action_with_score_list):
        for action in action_with_score_list:
            self._collect_comp_metrics(action)
            self._collect_vent_metrics(action)
        elapsed, handsoff = timeline_totals([action.action_data for action in action_with_score_list])
        self.metric["TotalEventTime"] += elapsed
        self.metric["TotalHandsOffTime"] += handsoff

    def _collect_comp_event_metrics(self, cycle_with_score: CycleWithScore) -> None:
        # depth/recoil 지표는 액션이 아니라 파형 이벤트 판정으로 집계한다
        # (액션 윈도우의 경계 어긋남으로 생기는 가짜 shallow 등을 지표에서도 제거).
        for event_score in cycle_with_score.comp_event_scores or []:
            depth = event_score["comp_depth"]["criterion"]
            recoil = event_score["recoil"]["criterion"]

            depth_value = event_score["comp_depth"].get("value")
            if depth_value is not None:
                self._sum_comp_depth += depth_value
                self._comp_depth_count += 1

            if depth == "good":
                self.metric["CompressionDepth"]["n_Good"] += 1
            elif depth == "low":
                self.metric["CompressionDepth"]["n_TooShallow"] += 1
            elif depth == "high":
                self.metric["CompressionDepth"]["n_TooDeep"] += 1

            if recoil == "good":
                self.metric["Recoil"]["n_Good"] += 1
            elif recoil == "bad":
                self.metric["Recoil"]["n_Incomplete"] += 1

    def _collect_comp_metrics(self, action):
        # depth/recoil은 액션이 아니라 사이클의 파형 이벤트 판정으로 집계한다(_collect_comp_event_metrics).
        if action.action_data["action_type"] == ACTION_TYPE_COMP and action.actor == Actor.REAL_PERSON:
            comp_rate = action.score["comp_rate"]["criterion"]
            hand_position = action.score["hand_position"]["criterion"]

            if action.action_data["compression_count"] > 1:
                if comp_rate == "good":
                    self.metric["CompressionRate"]["n_Good"] += 1
                elif comp_rate == "low":
                    self.metric["CompressionRate"]["n_TooSlow"] += 1
                elif comp_rate == "high":
                    self.metric["CompressionRate"]["n_TooFast"] += 1

                rate_value = action.score["comp_rate"].get("value") or 0
                if rate_value:
                    self._sum_comp_rate += rate_value
                    self._comp_rate_count += 1

            if hand_position == "good":
                self.metric["HandPosition"]["n_Good"] += 1
            elif hand_position == "side":
                self.metric["HandPosition"]["n_IncorrectLR"] += 1
            elif hand_position == "down":
                self.metric["HandPosition"]["n_IncorrectStomach"] += 1

            self.metric["CompressionActionNumber"] += 1

    def _collect_vent_metrics(self, action):
        if action.action_data["action_type"] == ACTION_TYPE_VENT and action.actor == Actor.REAL_PERSON:
            volume = action.score["vent_vol"]["criterion"]

            if volume == "good":
                self.metric["VentilationVolume"]["n_Good"] += 1
            elif volume == "low":
                self.metric["VentilationVolume"]["n_TooLittle"] += 1
            elif volume == "high":
                self.metric["VentilationVolume"]["n_TooMuch"] += 1

            self._sum_vent_vol += action.score["vent_vol"].get("value") or 0
            self._sum_vent_speed += action.action_data.get("ventilation_speed") or 0

            if self.calculation_config.is_infant():
                vent_speed = action.score["vent_speed"]["criterion"]
                if vent_speed == "good":
                    self.metric["VentilationSpeed"]["n_Good"] += 1
                elif vent_speed == "low":
                    self.metric["VentilationSpeed"]["n_TooFast"] += 1
                elif vent_speed == "high":
                    self.metric["VentilationSpeed"]["n_TooSlow"] += 1
            else:
                self.metric["VentilationSpeed"] = None

            self.metric["VentilationActionNumber"] += 1

            if self.calculation_config.is_vent_only() and action.action_data["ventilation_count"] > 1:
                rate = action.score["vent_rate"]["criterion"]
                if rate == "good":
                    self.metric["VentilationRate"]["n_Good"] += 1
                elif rate == "low":
                    self.metric["VentilationRate"]["n_InFrequently"] += 1
                elif rate == "high":
                    self.metric["VentilationRate"]["n_TooFrequently"] += 1

    def _collect_time_metrics(self, action):
        self.metric["TotalEventTime"] += action.action_data["total_action_ms"]
        self.metric["TotalHandsOffTime"] += action.action_data["handsoff_ms"]

    def calculate_metrics(self):
        self._calculate_comp_metrics()
        self._calculate_vent_metrics()
        self._calculate_ccf_metrics()
        self._calculate_measured_summary()

    def _calculate_comp_metrics(self):
        if self.metric["CompressionActionNumber"] != 0:
            compression_depth_action_count = self._sum_n_count(self.metric["CompressionDepth"])
            if compression_depth_action_count > 0:
                self.metric["CompressionDepth"]["%_TooShallow"] = custom_round(
                    (self.metric["CompressionDepth"]["n_TooShallow"] / compression_depth_action_count) * 100
                )
                self.metric["CompressionDepth"]["%_Good"] = custom_round(
                    (self.metric["CompressionDepth"]["n_Good"] / compression_depth_action_count) * 100
                )
                self.metric["CompressionDepth"]["%_TooDeep"] = custom_round(
                    (self.metric["CompressionDepth"]["n_TooDeep"] / compression_depth_action_count) * 100
                )

            compression_recoil_action_count = self._sum_n_count(self.metric["Recoil"])
            if compression_recoil_action_count > 0:
                self.metric["Recoil"]["%_Incomplete"] = custom_round(
                    (self.metric["Recoil"]["n_Incomplete"] / compression_recoil_action_count) * 100
                )
                self.metric["Recoil"]["%_Good"] = custom_round(
                    (self.metric["Recoil"]["n_Good"] / compression_recoil_action_count) * 100
                )

            compression_rate_action_count = self._sum_n_count(self.metric["CompressionRate"])
            if compression_rate_action_count > 0:
                self.metric["CompressionRate"]["%_TooSlow"] = custom_round(
                    (self.metric["CompressionRate"]["n_TooSlow"] / compression_rate_action_count) * 100
                )
                self.metric["CompressionRate"]["%_Good"] = custom_round(
                    (self.metric["CompressionRate"]["n_Good"] / compression_rate_action_count) * 100
                )
                self.metric["CompressionRate"]["%_TooFast"] = custom_round(
                    (self.metric["CompressionRate"]["n_TooFast"] / compression_rate_action_count) * 100
                )

            hand_position_action_count = self._sum_n_count(self.metric["HandPosition"])
            if hand_position_action_count > 0:
                self.metric["HandPosition"]["%_IncorrectLR"] = custom_round(
                    (self.metric["HandPosition"]["n_IncorrectLR"] / hand_position_action_count) * 100
                )
                self.metric["HandPosition"]["%_Good"] = custom_round(
                    (self.metric["HandPosition"]["n_Good"] / hand_position_action_count) * 100
                )
                self.metric["HandPosition"]["%_IncorrectStomach"] = custom_round(
                    (self.metric["HandPosition"]["n_IncorrectStomach"] / hand_position_action_count) * 100
                )

        if self.metric["CompressionCycleNumber"] != 0 and self.calculation_config.condition["training_type"] == "cpr":
            self.metric["CompressionCount"]["%_TooFew"] = custom_round(
                (self.metric["CompressionCount"]["n_TooFew"] / self.metric["CompressionCycleNumber"]) * 100
            )
            self.metric["CompressionCount"]["%_Good"] = custom_round(
                (self.metric["CompressionCount"]["n_Good"] / self.metric["CompressionCycleNumber"]) * 100
            )
            self.metric["CompressionCount"]["%_TooMany"] = custom_round(
                (self.metric["CompressionCount"]["n_TooMany"] / self.metric["CompressionCycleNumber"]) * 100
            )

            self.metric["CompressionNo"]["%_TooFew"] = custom_round(
                (self.metric["CompressionNo"]["n_TooFew"] / self.metric["CompressionCycleNumber"]) * 100
            )
            self.metric["CompressionNo"]["%_Good"] = custom_round(
                (self.metric["CompressionNo"]["n_Good"] / self.metric["CompressionCycleNumber"]) * 100
            )
            self.metric["CompressionNo"]["%_TooMany"] = custom_round(
                (self.metric["CompressionNo"]["n_TooMany"] / self.metric["CompressionCycleNumber"]) * 100
            )

            self.metric["CompressionDepth"] = self._ajd_ratio_to_100(self.metric["CompressionDepth"])
            self.metric["Recoil"] = self._ajd_ratio_to_100(self.metric["Recoil"])
            self.metric["CompressionRate"] = self._ajd_ratio_to_100(self.metric["CompressionRate"])
            self.metric["HandPosition"] = self._ajd_ratio_to_100(self.metric["HandPosition"])
            self.metric["CompressionNo"] = self._ajd_ratio_to_100(self.metric["CompressionNo"])
            self.metric["CompressionCount"] = self._ajd_ratio_to_100(self.metric["CompressionCount"])

    def _calculate_vent_metrics(self):
        # VentilationRate 분포는 CPR에선 사이클 단위, ventilation only에선 액션 단위로 쌓이므로
        # 환기 액션 수 게이트와 무관하게 자체 분포 합으로 비율을 계산한다
        # (환기를 하지 않은 CPR 세션도 사이클마다 InFrequently로 집계된다. V1과 동일).
        ventilation_rate_count = self._sum_n_count(self.metric["VentilationRate"])
        if ventilation_rate_count > 0:
            self.metric["VentilationRate"]["%_InFrequently"] = custom_round(
                (self.metric["VentilationRate"]["n_InFrequently"] / ventilation_rate_count) * 100
            )
            self.metric["VentilationRate"]["%_Good"] = custom_round(
                (self.metric["VentilationRate"]["n_Good"] / ventilation_rate_count) * 100
            )
            self.metric["VentilationRate"]["%_TooFrequently"] = custom_round(
                (self.metric["VentilationRate"]["n_TooFrequently"] / ventilation_rate_count) * 100
            )
            self.metric["VentilationRate"] = self._ajd_ratio_to_100(self.metric["VentilationRate"])

        if self.metric["VentilationActionNumber"] != 0:
            ventilation_volume_action_count = self._sum_n_count(self.metric["VentilationVolume"])
            if ventilation_volume_action_count > 0:
                self.metric["VentilationVolume"]["%_TooLittle"] = custom_round(
                    (self.metric["VentilationVolume"]["n_TooLittle"] / ventilation_volume_action_count) * 100
                )
                self.metric["VentilationVolume"]["%_Good"] = custom_round(
                    (self.metric["VentilationVolume"]["n_Good"] / ventilation_volume_action_count) * 100
                )
                self.metric["VentilationVolume"]["%_TooMuch"] = custom_round(
                    (self.metric["VentilationVolume"]["n_TooMuch"] / ventilation_volume_action_count) * 100
                )

            if self.metric["VentilationCycleNumber"] > 0:
                self.metric["VentilationCount"]["%_TooMany"] = custom_round(
                    (self.metric["VentilationCount"]["n_TooMany"] / self.metric["VentilationCycleNumber"]) * 100
                )
                self.metric["VentilationCount"]["%_Good"] = custom_round(
                    (self.metric["VentilationCount"]["n_Good"] / self.metric["VentilationCycleNumber"]) * 100
                )
                self.metric["VentilationCount"]["%_TooFew"] = custom_round(
                    (self.metric["VentilationCount"]["n_TooFew"] / self.metric["VentilationCycleNumber"]) * 100
                )

            self.metric["VentilationVolume"] = self._ajd_ratio_to_100(self.metric["VentilationVolume"])
            self.metric["VentilationCount"] = self._ajd_ratio_to_100(self.metric["VentilationCount"])

            if self.calculation_config.is_infant():
                ventilation_speed_action_count = self._sum_n_count(self.metric["VentilationSpeed"])
                if ventilation_speed_action_count > 0:
                    self.metric["VentilationSpeed"]["%_TooSlow"] = custom_round(
                        (self.metric["VentilationSpeed"]["n_TooSlow"] / ventilation_speed_action_count) * 100
                    )
                    self.metric["VentilationSpeed"]["%_Good"] = custom_round(
                        (self.metric["VentilationSpeed"]["n_Good"] / ventilation_speed_action_count) * 100
                    )
                    self.metric["VentilationSpeed"]["%_TooFast"] = custom_round(
                        (self.metric["VentilationSpeed"]["n_TooFast"] / ventilation_speed_action_count) * 100
                    )

                self.metric["VentilationSpeed"] = self._ajd_ratio_to_100(self.metric["VentilationSpeed"])
            else:
                self.metric["VentilationSpeed"] = None

    def _calculate_ccf_metrics(self):
        if self.metric["CCF"]["ccf_count"] > 0:
            self.metric["ScoreOfCCF"] = custom_round(self.metric["CCF"]["sum_score"] / self.metric["CCF"]["ccf_count"])
            self.metric["CCF"]["%_CCF"] = custom_round(self.metric["CCF"]["sum_ccf"] / self.metric["CCF"]["ccf_count"])
            self.metric["CCF"]["score"] = self.metric["ScoreOfCCF"]

    def _calculate_measured_summary(self):
        # ResultSummary는 채점 점수가 아니라 실측 평균을 요구한다(V1 계산기와 동일 의미):
        # 압박 속도 cpm, 깊이 mm, 환기량 ml, 환기 속도 ms, hands-off 초.
        if self._comp_rate_count:
            self.metric["AvgCompressionRate"] = custom_round(self._sum_comp_rate / self._comp_rate_count)
        if self._comp_depth_count:
            self.metric["AvgCompressionDepth"] = custom_round(self._sum_comp_depth / self._comp_depth_count)
        if self.metric["VentilationActionNumber"]:
            self.metric["AvgVentilationVolume"] = custom_round(
                self._sum_vent_vol / self.metric["VentilationActionNumber"]
            )
            self.metric["AvgVentilationSpeed"] = custom_round(
                self._sum_vent_speed / self.metric["VentilationActionNumber"]
            )
        self.metric["HandsOffTimeSec"] = self._calc_handsoff_seconds()

    def _calc_handsoff_seconds(self) -> int:
        # V1과 동일: CPR은 사이클 평균 초, compression only는 세션 총합 초. 환기 전용은 0.
        if self.calculation_config.is_cpr():
            if not self._handsoff_cycle_count:
                return 0
            return custom_round(self._sum_cycle_handsoff_ms / self._handsoff_cycle_count / 1000)
        if self.calculation_config.is_cco():
            return int(self.metric["TotalHandsOffTime"] / 1000)
        return 0

    def _ajd_ratio_to_100(self, values: dict) -> dict:
        sum_ratio = sum([v for k, v in values.items() if k.startswith("%")])

        if sum_ratio == self.RATIO:
            return values

        need_ajd_ratio = self.RATIO - sum_ratio
        if need_ajd_ratio > 3:
            return values

        values["%_Good"] += need_ajd_ratio

        return values

    def _sum_n_count(self, action_data: dict) -> int:
        result = 0
        for k, v in action_data.items():
            if "n_" in k:
                result += v
        return result

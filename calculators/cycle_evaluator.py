from calculators.waveform import COMP_PEAK_AMP, CompressionEvent, count_depth_peaks, drop_tap_events, segment_compressions
from calculators.action_timeline import timeline_totals
from config.borders import fall_linear_get_point, rise_linear_get_point, trapezium_get_point, BaseBorder, dart_get_point
from config.calculation_config import BaseCalculationConfig
from config.constants import (
    HANDSOFF_BUFFER_AFTER_AED_FOR_LAY,
    CYCLE_COMPLETE_CONDITION_COMP_COUNT,
    ACTION_TYPE_COMP,
    ACTION_TYPE_VENT,
    CALC_CASE_NOT_CALC,
    CALC_CASE_CPR,
    CALC_CASE_ONLY_COMP,
    CALC_CASE_ONLY_VENT,
    CALC_CASE_DID_NOT_RESCUE_VENT,
    MINIMUM_COMP_DEPTH,
)
from config.enums import ActorType, Actor
from config.score_weight import ScoreWeight
from models.action import ActionWithScore
from util.custom_math import custom_round


class NullPolicy:
    """CPR session minimums; the reference paths handle single-skill modes.

    Adult/child compression minimum: 90; infant: 45; ventilation: 6.
    Count source remains the complete session action counts from prepare_data.
    Compression rate, CCF, and measured coaching signals are outside the null groups.

    This exception applies only to ARC2020/ARC2025 CPR. Other guidelines,
    including ERC initial rescue ventilation, keep the reference calculation.
    """

    MINIMUM_SESSION_COMP_COUNT = 90
    MINIMUM_SESSION_COMP_COUNT_INFANT = 45
    MINIMUM_SESSION_VENT_COUNT = 6

    def __init__(self, chest_null: bool = False, vent_null: bool = False):
        self.chest_null = chest_null
        self.vent_null = vent_null

    @property
    def both_null(self) -> bool:
        return self.chest_null and self.vent_null

    @property
    def active(self) -> bool:
        return self.chest_null or self.vent_null

    @classmethod
    def inactive(cls) -> "NullPolicy":
        return cls()

    @classmethod
    def create(cls, calculation_config: BaseCalculationConfig, comp_count: int, vent_count: int) -> "NullPolicy":
        if (
            not calculation_config.is_cpr()
            or calculation_config.condition.get("guideline") not in ("ARC2020", "ARC2025")
        ):
            return cls.inactive()
        minimum_comp_count = (
            cls.MINIMUM_SESSION_COMP_COUNT_INFANT
            if calculation_config.is_infant()
            else cls.MINIMUM_SESSION_COMP_COUNT
        )
        return cls(
            chest_null=comp_count < minimum_comp_count,
            vent_null=vent_count < cls.MINIMUM_SESSION_VENT_COUNT,
        )


class Cycle:
    def __init__(self, actions: list[ActionWithScore], cycle_num: int, is_last_cycle: bool = False):
        self.actions = actions
        self.cycle_num = cycle_num
        self.is_last_cycle = is_last_cycle
        self.actor_type = self._get_actor_type()

    def handsoff(self) -> int:
        if any("_elapsed_interval" not in a.action_data for a in self.actions):
            return sum(a.action_data["handsoff_ms"] for a in self.actions)
        return timeline_totals([a.action_data for a in self.actions])[1]

    def total_action_ms(self) -> int:
        # Historical library callers may supply only the field being queried.
        # Reading elapsed time must not newly require a hands-off measurement.
        if any("_elapsed_interval" not in a.action_data for a in self.actions):
            return sum(a.action_data["total_action_ms"] for a in self.actions)
        return timeline_totals([a.action_data for a in self.actions])[0]

    def get_comp_count(self) -> int:
        return len([1 for a in self.actions if a.action_type == ACTION_TYPE_COMP])

    def get_vent_count(self) -> int:
        return len([1 for a in self.actions if a.action_type == ACTION_TYPE_VENT])

    def get_calc_case(self) -> str:
        vent_count = self.get_vent_count()

        if self.get_comp_count() == 0 and vent_count == 0:
            return CALC_CASE_NOT_CALC

        if self.get_comp_count() == 0 and vent_count >= 0:
            return CALC_CASE_ONLY_VENT

        if not self.is_last_cycle:
            return CALC_CASE_CPR

        if vent_count > 0:
            return CALC_CASE_CPR

        if self.get_comp_count() < CYCLE_COMPLETE_CONDITION_COMP_COUNT:
            return CALC_CASE_NOT_CALC

        if vent_count == 0:
            return CALC_CASE_ONLY_COMP

        return CALC_CASE_CPR

    def _get_actor_type(self) -> ActorType:
        # 사이클의 첫·마지막 액션만 보지 않고, 압박과 환기를 각각 주로 수행한 쪽
        # (실제 사용자 / 가상 파트너)으로 사이클 유형을 판정한다. 첫·마지막만 보면
        # 전환 사이클에서 선행 depth=0 VP 압박 등으로 사이클 전체가 ONLY_VP(0점)로
        # 잘못 분류된다.
        if not self.actions:
            return ActorType.ONLY_REAL_PERSON

        # 실제 사용자가 수행한 동작이 하나도 없으면(전부 가상 파트너) ONLY_VP.
        if not any(a.actor == Actor.REAL_PERSON for a in self.actions):
            return ActorType.ONLY_VIRTUAL_PARTNER

        vp_comp = self._primary_actor(ACTION_TYPE_COMP) == Actor.VIRTUAL_PARTNER
        vp_vent = self._primary_actor(ACTION_TYPE_VENT) == Actor.VIRTUAL_PARTNER

        if vp_comp and vp_vent:
            return ActorType.ONLY_VIRTUAL_PARTNER
        if vp_comp:
            return ActorType.VIRTUAL_PARTNER_COMP
        if vp_vent:
            return ActorType.VIRTUAL_PARTNER_VENT
        return ActorType.ONLY_REAL_PERSON

    def _primary_actor(self, action_type: str) -> Actor | None:
        # 해당 동작을 더 많이 수행한 쪽. 수가 같으면 실제 사용자로 본다(부당한 0점 방지).
        actors = [a.actor for a in self.actions if a.action_type == action_type]
        if not actors:
            return None
        vp = sum(1 for actor in actors if actor == Actor.VIRTUAL_PARTNER)
        return Actor.VIRTUAL_PARTNER if vp > len(actors) - vp else Actor.REAL_PERSON


class CycleMaker:
    @staticmethod
    def make_cycles(action_with_score: list[ActionWithScore]) -> list[Cycle]:
        cycles = []
        cycle_num = 1
        same_cycle_num_actions = []
        for action in action_with_score:
            if cycle_num == action.action_data["cycle_cnt"]:
                same_cycle_num_actions.append(action)
            else:
                cycles.append(Cycle(same_cycle_num_actions, cycle_num))
                cycle_num += 1
                same_cycle_num_actions = [action]

        if same_cycle_num_actions:
            cycles.append(Cycle(same_cycle_num_actions, cycle_num, is_last_cycle=True))

        return cycles


class CycleWithScore:
    def __init__(
        self,
        cycle: Cycle,
        part_num: int,
        score_weight: ScoreWeight,
        calculation_config: BaseCalculationConfig,
        null_policy: NullPolicy | None = None,
    ):
        self.cycle = cycle
        self.part_num = part_num
        self.score_weight = score_weight
        self.calculation_config = calculation_config
        self.null_policy = null_policy or NullPolicy.inactive()

        self.score_comp_depth = None
        self.score_comp_rate = None
        self.score_comp_no = None
        self.score_comp_count = None
        self.score_recoil = None
        self.score_hand_position = None
        self.score_vent_vol = None
        self.score_vent_count = None
        self.score_vent_rate = None
        # 코칭 판단 전용 환기 속도 신호. HSTM/총점용 score_vent_rate(V1 패리티 유지)와 분리.
        # CPR: 환기가 있는 사이클의 사이클 단위 분당 환기율 판정(_calc_cycle_vent_rate) grade.
        # vent-only: 환기 2회 이상일 때 기존 참평균. 측정 불가 사이클은 None(코칭 안 함).
        self.score_vent_rate_measured = None
        self.score_vent_speed = None
        self.score_ccf = None
        self.score_rescue_vent = None
        self.ccf = None
        self.total_action_ms = self.cycle.total_action_ms()
        self.evaluate_result_comp_no = {"grade": 0, "criterion": ""}
        self.evaluate_result_vent_cnt = {"grade": 0, "criterion": ""}
        self.evaluate_result_vent_rate = {"grade": 0, "criterion": ""}
        self.evaluate_result_rescue_vent = {"grade": 0, "criterion": ""}
        self.vent_cnt = 0
        self.calc_case = self.cycle.get_calc_case()
        self.is_rescue_vent_cycle = self._is_for_rescue_vent()
        # 파형 이벤트 단위 depth/recoil 판정(2인구조). 지표(metric) 집계도 이걸 쓴다.
        self.comp_event_scores = None

        # Rescue Vent를 해야되는데 하지 않았을 경우 rescue vent 항목 점수들 0점 INNO-1138
        if self.did_rescue_vent is False:
            self.calc_case = CALC_CASE_DID_NOT_RESCUE_VENT
            self.score_vent_vol = 0
            self.score_vent_count = 0
            self.score_rescue_vent = 0
            if self.calculation_config.is_infant():
                self.score_vent_speed = 0

    @property
    def is_first_cycle(self) -> bool:
        return self.cycle.cycle_num == 1 and self.part_num == 1

    @property
    def did_rescue_vent(self) -> bool | None:
        """
        true: did rescue vent
        false: did not rescue vent
        None: not rescue vent cycle
        """
        if not self.calculation_config.need_rescue_vent() or not self.is_first_cycle:
            return

        return (
            self.calculation_config.need_rescue_vent() and self.is_first_cycle and self.calc_case == CALC_CASE_ONLY_VENT
        )

    def make_score(self, border: BaseBorder):
        if self.calc_case == CALC_CASE_NOT_CALC or self.calc_case == CALC_CASE_DID_NOT_RESCUE_VENT:
            return self

        if self.did_rescue_vent:
            self.evaluate_result_rescue_vent = self._calc_rescue_vent(border)
            self.score_rescue_vent = self.evaluate_result_rescue_vent["grade"]
            # return self

        if self.can_calc_comp():
            self._embed_comp_score(border)
            # TODO: 당장의 계산을 위한 대응
            if self.calculation_config.condition["training_type"] == "cpr":
                self.evaluate_result_comp_no = self._calc_comp_no_score(border, self.calc_case)
                self.score_comp_no = self.evaluate_result_comp_no["grade"]
                self.score_comp_count = self.score_comp_no

        if self.can_calc_vent():
            self._embed_vent_score()
            # TODO: 당장의 계산을 위한 대응
            if self.calculation_config.condition["training_type"] == "cpr" and not self.did_rescue_vent:
                self.evaluate_result_vent_cnt = self._calc_vent_cnt_score(border, self.vent_cnt)
                self.score_vent_count = self.evaluate_result_vent_cnt["grade"]
                self.evaluate_result_vent_rate = self._calc_cycle_vent_rate(border)
                # 코칭 전용 속도 신호: 환기가 실제로 있었던 사이클만 사이클 단위 분당 환기율
                # 판정을 쓴다(HSTM criteria 분포와 동일 판정). 환기 0회 사이클은 횟수(count)
                # 결함이므로 속도는 측정 불가(None)로 남겨 코칭 대상에서 제외한다.
                if self.vent_cnt:
                    self.score_vent_rate_measured = self.evaluate_result_vent_rate["grade"]

        self.score_ccf = self.calc_ccf_score(border)
        self._apply_score_null_policy()

        return self

    def _is_for_rescue_vent(self) -> bool:
        return all(
            [
                self.calculation_config.need_rescue_vent(),
                self.cycle.cycle_num == 1,
                self.part_num == 1,
            ]
        )

    def _calc_rescue_vent(self, border: BaseBorder) -> dict:
        comp_count = len(
            [action for action in self.cycle.actions if action.action_data["action_type"] == ACTION_TYPE_COMP]
        )
        vent_count = len(
            [action for action in self.cycle.actions if action.action_data["action_type"] == ACTION_TYPE_VENT]
        )
        # rescue vent가 없으면 첫 사이클을 0점처리 함
        if comp_count > 0:
            vent_count = 0

        return trapezium_get_point(border, "init_rescue_cnt", vent_count)

    def _calc_vent_cnt_score(self, border: BaseBorder, vent_cnt: int) -> dict:
        if vent_cnt > 4:
            vent_cnt = 0

        return dart_get_point(border, "vent_cnt", str(vent_cnt))

    def _calc_cycle_vent_rate(self, border: BaseBorder) -> dict:
        # CPR 사이클의 분당 환기율(60초 환산) 판정. HSTM 문서의 VentilationRate criteria
        # 분포 집계용이며, 점수(score_vent_rate, 액션 기반)에는 영향을 주지 않는다.
        if not self.total_action_ms:
            return {"grade": 0, "criterion": "low"}

        vent_rate_digit = (60 * 1000) * self.vent_cnt / self.total_action_ms
        return trapezium_get_point(border, "vent_rate_1p", vent_rate_digit)

    def _calc_comp_no_score(self, border: BaseBorder, calc_case: str) -> dict:
        comp_count = self._get_comp_cnt()
        # TODO: 30-2 훈련으로 상정되어 있으므로, 그렇지 않은 경우에 대한 대응이 가능해야 함
        if calc_case == "only_comp" and comp_count < 30:
            return {"grade": 100, "criterion": "good"}

        comp_no_score = trapezium_get_point(border, self._get_comp_cnt_key(), comp_count)
        return comp_no_score

    def _get_comp_cnt_key(self) -> str:
        if self.calculation_config.condition["cpr_cycle_type"] == "152":
            return "comp_cnt_152"

        return "comp_cnt_302"

    def _get_comp_cnt(self) -> int:
        # 마네킨 compression_count는 사이클 경계에 마커(예: 30)를 찍어 가짜 압박을
        # 한 번 더 만든다(15회를 16회로). 깊이 파형의 peak(상승→하강)를 세면 경계 마커·하강edge
        # 아티팩트에 영향받지 않고 실제 압박 수가 나온다. (검출·사이클 구조는 그대로 두고 카운트만 보정)
        return self._count_compression_peaks()

    def _count_compression_peaks(self) -> int:
        # 분절(segment_compressions)과 같은 파형·같은 zigzag를 쓰므로
        # 카운트 == 이벤트 수가 구조적으로 보장된다.
        return len(self._compression_events())

    def _compression_events(self) -> list[CompressionEvent]:
        # 사이클의 압박 깊이 샘플을 패킷(10샘플) 단위로 묶어 압박 이벤트로 분절한다.
        # 분절은 패킷별 max 파형 기준(인트라-패킷 진동 무시), 값은 샘플 단위.
        # 마네킨 카운트 증가 수와 크게 어긋나면 탭(짧은 누름) 이벤트를 걸러낸다(drop_tap_events).
        packets = []
        counts = []
        for action in self.cycle.actions:
            if action.action_data["action_type"] != ACTION_TYPE_COMP:
                continue
            depths = action.action_data["compression_depth"]
            for i in range(0, len(depths), 10):
                packets.append(depths[i : i + 10])
            action_counts = action.action_data["compression_count"]
            counts.extend(action_counts if isinstance(action_counts, list) else [action_counts])
        events = segment_compressions(packets)
        increments = sum(1 for i in range(1, len(counts)) if counts[i] > counts[i - 1])
        return drop_tap_events(packets, events, increments)

    def _real_comp_actions(self) -> list[ActionWithScore]:
        # 압박 품질(rate/hand) 평균에 쓸 "실제 압박" 액션만 추린다.
        # 마네킨 경계 마커는 깊이≈0인 가짜 압박 액션을 만들어, 그대로 평균내면
        # 낮은 점수 + 늘어난 분모로 품질을 이중으로 끌어내린다. count(_count_compression_peaks)와
        # 같은 취지로 가짜 압박을 빼 실제 압박만으로 평균낸다.
        comp_actions = [a for a in self.cycle.actions if a.action_data["action_type"] == ACTION_TYPE_COMP]
        real = [a for a in comp_actions if max(a.action_data["compression_depth"]) >= MINIMUM_COMP_DEPTH]
        return real or comp_actions  # 전부 걸러지는 이상 케이스에선 원본 유지(분모 0 방지)

    def _embed_comp_score(self, border: BaseBorder | None = None) -> None:
        sum_comp_rate_score = 0
        sum_hand_position_score = 0

        # count와 동일하게 가짜 압박(깊이≈0 경계 마커)을 제외한 실제 압박만으로 평균낸다.
        comp_actions = self._real_comp_actions()
        total_action_count = len(comp_actions)
        if not total_action_count:
            return

        # rate/hand는 (아직) 액션 기반으로 평균낸다. depth/recoil은 아래에서 파형 이벤트 기반.
        for i, action in enumerate(comp_actions):
            sum_hand_position_score += action.score["hand_position"]["grade"]
            if i != 0:
                sum_comp_rate_score += action.score["comp_rate"]["grade"]

        self.score_hand_position = custom_round(sum_hand_position_score / total_action_count)
        if total_action_count > 1:
            self.score_comp_rate = custom_round(sum_comp_rate_score / (total_action_count - 1))
        else:
            self.score_comp_rate = 0

        self._embed_comp_depth_recoil_score(comp_actions, border)
        return

    def _embed_comp_depth_recoil_score(self, comp_actions: list[ActionWithScore], border: BaseBorder | None) -> None:
        # 액션 윈도우는 실제 압박과 경계가 어긋나(직전 압박의 하강 꼬리가 별도 윈도우로
        # 잘리는 등) 엉뚱한 max/min으로 depth/recoil이 채점된다. 카운트와 같은 파형
        # 분절의 정점/잔여 깊이로 채점해 분모(이벤트 수 = 카운트)와 값을 정렬한다.
        if border is not None:
            events = self._compression_events()
            if events:
                self.comp_event_scores = [self._comp_event_score(event, border) for event in events]
                self.score_comp_depth = custom_round(
                    sum(s["comp_depth"]["grade"] for s in self.comp_event_scores) / len(self.comp_event_scores)
                )
                self.score_recoil = custom_round(
                    sum(s["recoil"]["grade"] for s in self.comp_event_scores) / len(self.comp_event_scores)
                )
                return
            # 이벤트가 없으면(전부 진폭 미달인 이상 케이스) 액션 기반으로 후퇴하되,
            # 지표 집계가 이 사이클을 잃지 않도록 이벤트 점수를 액션 점수로 채운다.
            self.comp_event_scores = [
                {"comp_depth": action.score["comp_depth"], "recoil": action.score["recoil"]} for action in comp_actions
            ]

        self.score_comp_depth = custom_round(
            sum(action.score["comp_depth"]["grade"] for action in comp_actions) / len(comp_actions)
        )
        self.score_recoil = custom_round(
            sum(action.score["recoil"]["grade"] for action in comp_actions) / len(comp_actions)
        )

    @staticmethod
    def _comp_event_score(event: CompressionEvent, border: BaseBorder) -> dict:
        # 실측 평균 깊이(ResultSummary) 집계용으로 판정 외에 mm 값도 남긴다
        # (액션 점수의 "value"와 같은 계약. 액션 폴백 경로는 이미 value를 가진다).
        depth_digit = custom_round(event.peak_depth / 2)
        comp_depth = trapezium_get_point(border, "comp_depth", depth_digit)
        comp_depth.update({"value": depth_digit})
        return {
            "comp_depth": comp_depth,
            "recoil": fall_linear_get_point(border, "recoil", custom_round(event.recoil_depth / 2)),
        }

    def _embed_vent_score(self) -> None:
        sum_vent_vol_score = 0
        sum_vent_speed_score = 0
        sum_vent_rate_score = 0

        total_action_count = self.cycle.get_vent_count()
        if not total_action_count:
            return

        vent_actions = [
            action for action in self.cycle.actions if action.action_data["action_type"] == ACTION_TYPE_VENT
        ]
        for i, action in enumerate(vent_actions):
            sum_vent_vol_score += action.score["vent_vol"]["grade"]

            if self.calculation_config.is_infant():
                sum_vent_speed_score += action.score["vent_speed"]["grade"]
            if self.is_rescue_vent_cycle:
                continue
            if self.calc_case == CALC_CASE_ONLY_VENT and self.cycle.cycle_num == 1 and i == 0:
                continue
            sum_vent_rate_score += action.score["vent_rate"]["grade"]

        self.score_vent_vol = custom_round(sum_vent_vol_score / total_action_count)
        # HSTM/총점용(V1 패리티) — 기존 계산 그대로(한 글자도 바꾸지 않음).
        if total_action_count > 1:
            self.score_vent_rate = custom_round(sum_vent_rate_score / (total_action_count - 1))
        else:
            self.score_vent_rate = 0
        # 코칭 전용(measured): vent-only 등 비CPR은 모든 환기가 유효하므로 참평균을 그대로 쓴다.
        # CPR은 여기서 채우지 않는다. 액션 duration 환산(60000/소요ms)은 실제 환기(1~2초)가
        # 30회/분 이상으로 환산되어 첫 호흡을 포함해 거의 전부 0점이 되는 아티팩트라 코칭 신호로
        # 쓸 수 없고, make_score에서 사이클 단위 판정(_calc_cycle_vent_rate)의 grade로 채운다.
        if not self.calculation_config.is_cpr():
            self.score_vent_rate_measured = self.score_vent_rate if total_action_count > 1 else None
        self.vent_cnt = len(vent_actions)
        if self.is_rescue_vent_cycle:
            self.score_vent_count = self.score_rescue_vent
        if self.calculation_config.is_infant():
            self.score_vent_speed = custom_round(sum_vent_speed_score / total_action_count)

        return

    def calc_ccf(self) -> int:
        ccf = 0

        if total_action_ms := self.total_action_ms:
            ccf = int(((total_action_ms - self.buffer_assigned_handsoff) / total_action_ms) * 100)

        self.ccf = ccf if ccf <= 100 else 100

        return self.ccf

    @property
    def buffer_assigned_handsoff(self) -> int:
        buffer_assigned_handsoff = self.cycle.handsoff() - self._get_handsoff_buffer_after_aed()
        return buffer_assigned_handsoff if buffer_assigned_handsoff > 0 else 0

    def _get_handsoff_buffer_after_aed(self) -> int:
        if self.part_num > 1:
            return HANDSOFF_BUFFER_AFTER_AED_FOR_LAY

        return 0

    def calc_ccf_score(self, border: BaseBorder) -> int | None:
        if self.calculation_config.is_vent_only() or self.did_rescue_vent:
            return None

        ccf = self.calc_ccf()
        ccf_score = rise_linear_get_point(border, self._get_ccf_border_key(), ccf)

        return ccf_score["grade"]

    def _get_ccf_border_key(self) -> str:
        return "ccf_comp" if self.calculation_config.is_cco() else "ccf_1p"

    def overall(self) -> int | None:
        if self.calc_case == CALC_CASE_NOT_CALC:
            # Rescue Vent를 해야되는데 하지 않았을 경우 0점 INNO-1138
            if self.did_rescue_vent is False:
                return 0
            return None

        if self.null_policy.both_null:
            return None
        if self.calculation_config.is_cpr() and self.null_policy.active:
            return self._overall_with_null_policy()

        overall = self.score_ccf * self.score_weight.CCF if self.score_ccf else 0
        overall = self._overall_comp(overall)
        overall = self._overall_vent(overall)

        overall = self._adj_overall_partial_acted_cycle(overall)
        if self.did_rescue_vent:
            overall = self._adj_rescue_vent_cycle(overall)
        else:
            overall = self._adj_rescue_vent_other_cycles(overall)

        return custom_round(overall)

    def _apply_score_null_policy(self) -> None:
        if self.null_policy.chest_null:
            self.score_comp_depth = None
            self.score_comp_no = None
            self.score_comp_count = None
            self.score_recoil = None
            self.score_hand_position = None
        if self.null_policy.vent_null:
            self.score_vent_vol = None
            self.score_vent_rate = None
            self.score_vent_count = None
            self.score_vent_speed = None

    def _overall_with_null_policy(self) -> int | None:
        # 스펙 §5.3 (B-2/R3-2A) 승인 재설계 — cpr 훈련 전용.
        # 전제: chest/vent 중 정확히 한쪽만 null (양측 null은 overall()에서 이미 None).
        # 분자는 원본 _overall_comp/_overall_vent와 동일한 적용 조건에서 null 그룹만 제외하고,
        # 분모는 원본 경로별 가중치 집합(일반=전체 합 1.0, VP 사이클=score_weight.get_vp_*_weight의
        # 집합)에서 null 그룹 가중치를 제외한 잔여 합이다.
        actor_type = self.cycle.actor_type
        if actor_type == ActorType.ONLY_VIRTUAL_PARTNER:
            # 원본 hstm_v2 cycle_evaluator.py:535-536과 동일(아티팩트 사이클 — 집계에서도 제외됨)
            return 0

        weight = self.score_weight
        chest_null = self.null_policy.chest_null
        vent_null = self.null_policy.vent_null

        numerator = self.score_ccf * weight.CCF if self.score_ccf else 0

        comp_applicable = actor_type.can_calc_comp() and self.calc_case in [CALC_CASE_CPR, CALC_CASE_ONLY_COMP]
        vent_applicable = actor_type.can_calc_vent() and self.calc_case in [CALC_CASE_CPR, CALC_CASE_ONLY_VENT]

        if comp_applicable:
            # 확정 필드 매핑(스펙 §5.3)상 score_comp_rate는 chest null 그룹이 아니므로 유지된다.
            numerator += (self.score_comp_rate or 0) * weight.COMP_RATE
            if not chest_null:
                numerator += (
                    self.score_comp_depth * weight.COMP_DEPTH
                    + self.score_recoil * weight.RECOIL
                    + self.score_hand_position * weight.HAND_POSITION
                    # cpr 훈련 확정 경로이므로 원본 :497-498의 is_cpr() 분기와 동일하게 항상 가산
                    + self.score_comp_count * weight.COMP_COUNT
                )

        if vent_applicable and not vent_null:
            # 환기 0회 사이클은 vol/rate/count가 None일 수 있어 원본과 동일하게 0 가드(:510-519).
            numerator += (self.score_vent_vol or 0) * weight.VENT_VOL + (
                self.score_vent_rate or 0
            ) * weight.VENT_RATE
            if self.calc_case == CALC_CASE_CPR:
                numerator += (self.score_vent_count or 0) * weight.VENT_COUNT
            if self.score_vent_speed is not None:
                numerator += self.score_vent_speed * weight.VENT_SPEED

        denominator = self._null_policy_weight_sum()
        if not denominator:
            return 0

        return custom_round(numerator / denominator)

    def _null_policy_weight_sum(self) -> float:
        weight = self.score_weight
        chest_null = self.null_policy.chest_null
        vent_null = self.null_policy.vent_null
        actor_type = self.cycle.actor_type

        chest_weight = weight.COMP_DEPTH + weight.RECOIL + weight.HAND_POSITION + weight.COMP_COUNT

        if actor_type == ActorType.VIRTUAL_PARTNER_COMP:
            # 원본 score_weight.get_vp_comp_weight(include_vent_speed) 집합에서 null 그룹 제외
            denominator = weight.CCF
            if not vent_null:
                denominator += weight.VENT_VOL + weight.VENT_COUNT
                if self.score_vent_speed is not None:
                    denominator += weight.VENT_SPEED
            return denominator

        if actor_type == ActorType.VIRTUAL_PARTNER_VENT:
            # 원본 score_weight.get_vp_vent_weight 집합에서 null 그룹 제외
            denominator = weight.CCF + weight.COMP_RATE
            if not chest_null:
                denominator += chest_weight
            return denominator

        # 일반 경로: 전체 가중치 합(cpr 가중치 클래스는 합 1.0)에서 null 그룹 제외
        denominator = (
            weight.CCF
            + weight.COMP_DEPTH
            + weight.COMP_RATE
            + weight.RECOIL
            + weight.HAND_POSITION
            + weight.COMP_COUNT
            + weight.VENT_VOL
            + weight.VENT_COUNT
            + weight.VENT_RATE
            + weight.VENT_SPEED
        )
        if chest_null:
            denominator -= chest_weight
        if vent_null:
            denominator -= weight.VENT_VOL + weight.VENT_COUNT + weight.VENT_RATE + weight.VENT_SPEED
        return denominator

    def _overall_rescue_vent(self, overall: float) -> float:
        return overall + (self.score_rescue_vent * self.score_weight.RESCUE_VENT)

    def _adj_rescue_vent_cycle(self, overall: float) -> float:
        return (overall / self.score_weight.get_rescue_vent_weight()) * 100

    def _adj_rescue_vent_other_cycles(self, overall: float):
        if not self.calculation_config.need_rescue_vent():
            return overall
        if self.did_rescue_vent:
            return overall

        adj_ratio = (1 - self.score_weight.RESCUE_VENT) * 100
        return (overall / adj_ratio) * 100

    def _overall_comp(self, overall: float) -> float:
        if (
            not self.cycle.actor_type.can_calc_comp()
            or self.calculation_config.is_vent_only()
            or self.calc_case not in [CALC_CASE_CPR, CALC_CASE_ONLY_COMP]
        ):
            return overall

        overall += (
            self.score_comp_depth * self.score_weight.COMP_DEPTH
            + self.score_comp_rate * self.score_weight.COMP_RATE
            + self.score_recoil * self.score_weight.RECOIL
            + self.score_hand_position * self.score_weight.HAND_POSITION
        )
        if self.calculation_config.is_cpr():
            overall += self.score_comp_count * self.score_weight.COMP_COUNT

        return overall

    def _overall_vent(self, overall: float) -> float:
        if (
            not self.cycle.actor_type.can_calc_vent()
            or self.calc_case not in [CALC_CASE_CPR, CALC_CASE_ONLY_VENT]
            or self.calculation_config.is_cco()
        ):
            return overall

        # 환기 0회 사이클(압박만 있고 환기 없는 CPR 사이클)은 vol/rate/count가 None일 수 있다.
        # None × 가중치는 TypeError(500)이므로 0으로 가드한다(정상 세션은 항상 수치라 no-op).
        if self.did_rescue_vent:
            overall += (self.score_vent_vol or 0) * self.score_weight.VENT_VOL + (
                self.score_vent_count or 0
            ) * self.score_weight.VENT_COUNT
        else:
            overall += (self.score_vent_vol or 0) * self.score_weight.VENT_VOL + (
                self.score_vent_rate or 0
            ) * self.score_weight.VENT_RATE

        if self.calc_case == CALC_CASE_CPR:
            overall += (self.score_vent_count or 0) * self.score_weight.VENT_COUNT

        if self.score_vent_speed is not None:
            overall += self.score_vent_speed * self.score_weight.VENT_SPEED

        return overall

    def _adj_overall_partial_acted_cycle(self, overall: float) -> float:
        if self.cycle.actor_type == ActorType.VIRTUAL_PARTNER_COMP:
            include_vent_speed = self.score_vent_speed is not None
            return custom_round((overall / self.score_weight.get_vp_comp_weight(include_vent_speed)) * 100)
        elif self.cycle.actor_type == ActorType.VIRTUAL_PARTNER_VENT:
            return custom_round((overall / self.score_weight.get_vp_vent_weight()) * 100)
        elif self.cycle.actor_type == ActorType.ONLY_VIRTUAL_PARTNER:
            return 0

        if self.calc_case == CALC_CASE_ONLY_COMP and not self.calculation_config.is_cpr():
            return custom_round((overall / self.score_weight.get_only_comp_weight()) * 100)

        return overall

    def to_dict(self) -> dict:
        return {
            "calc_case": self.calc_case,
            "score_comp_depth": self.score_comp_depth,
            "score_comp_rate": self.score_comp_rate,
            "score_comp_no": self.score_comp_no,
            "score_comp_count": self.score_comp_count,
            "score_recoil": self.score_recoil,
            "score_hand_position": self.score_hand_position,
            "score_vent_vol": self.score_vent_vol,
            "score_vent_rate": self.score_vent_rate,
            "score_vent_count": self.score_vent_count,
            "score_vent_speed": self.score_vent_speed,
            "score_ccf": self.score_ccf,
            "total_action_ms": self.total_action_ms,
            "overall": self.overall(),
        }

    def can_calc_comp(self) -> bool:
        return self.cycle.actor_type.can_calc_comp() and self.calc_case in [CALC_CASE_CPR, CALC_CASE_ONLY_COMP]

    def can_calc_vent(self) -> bool:
        return self.cycle.actor_type.can_calc_vent() and self.calc_case in [CALC_CASE_CPR, CALC_CASE_ONLY_VENT]

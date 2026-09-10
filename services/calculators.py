from calculators.action_evaluator import ActionEvaluator
from calculators.aed_evaluator import calculate_aed_score
from calculators.cycle_evaluator import CycleMaker, CycleWithScore, Cycle, NullPolicy
from calculators.merge_calculator import calculate_part_score, calculate_total_score
from calculators.metric_evaluator import MetricEvaluator
from config.calculation_config import BaseCalculationConfig
from config.score_weight import ScoreWeightFactory
from models.action import ActionWithScore
from models.calculation import CalculationResult
from services.config import Config


def calculate_cpr_score(
    prepared_partial_data: list[dict],
    config: Config,
    null_policy: NullPolicy | None = None,
) -> (list[dict], list[CycleWithScore]):
    action_evaluator = ActionEvaluator(config)

    part_with_score_list = []
    cycle_with_score_list = []
    # 파트 iteration
    for partial_data in prepared_partial_data:
        # 개별 액션 점수 계산
        action_with_score_list = action_evaluator.evaluate_action(partial_data["action_list"])

        # 사이클 점수 계산
        cycle_scores = calculate_cycle(
            partial_data["part_num"],
            action_with_score_list,
            config,
            null_policy,
        )

        part_with_score_list.append(
            {
                "part_num": partial_data["part_num"],
                "result": cycle_scores,
            }
        )
        cycle_with_score_list.extend(cycle_scores["cycle_with_score_list"])

    return part_with_score_list, cycle_with_score_list


def calculate_cycle(
    part_num: int,
    action_with_score_list: list[ActionWithScore],
    config: Config,
    null_policy: NullPolicy | None = None,
) -> dict:
    # overall 계산을 위한 weight 가져옴
    score_weight = ScoreWeightFactory.create(
        config.condition["target"],
        config.condition["training_type"],
        config.condition["guideline"],
    )

    # 파트 내에서 사이클 구분
    cycles = CycleMaker.make_cycles(action_with_score_list)

    # Rescue Vent를 해야되는데 하지 않았을 경우 계산을 위해서 빈 객체 삽입 INNO-1138
    first_cycle = CycleWithScore(cycles[0], part_num, score_weight, config.calculation_config)
    if first_cycle.did_rescue_vent is False:
        cycles.insert(0, Cycle([], 0, False))
        for cycle in cycles:
            cycle.cycle_num += 1

    cycle_with_score_list = []
    for cycle in cycles:
        cycle_with_score = CycleWithScore(
            cycle,
            part_num,
            score_weight,
            config.calculation_config,
            null_policy,
        )
        if score := cycle_with_score.make_score(config.border):
            cycle_with_score_list.append(score)

    return {
        "action_with_score_list": action_with_score_list,
        "cycle_with_score_list": cycle_with_score_list,
        "score": calculate_part_score(cycle_with_score_list, part_num, config, null_policy),
    }


def make_cpr_metrics(part_with_score_list, calculation_config: BaseCalculationConfig) -> dict:
    return MetricEvaluator(calculation_config).evaluate(part_with_score_list)


def calculate_cpr(prepared_data: dict, config: Config) -> CalculationResult:
    null_policy = NullPolicy.create(
        config.calculation_config, prepared_data["comp_count"], prepared_data["vent_count"],
    )
    # 파트, 사이클 점수 계산
    # 분리 할 걸...
    part_with_scores, cycle_with_score_list = calculate_cpr_score(
        prepared_data["prepared_cpr_data"],
        config,
        null_policy,
    )

    cpr_total_scores = calculate_total_score(cycle_with_score_list, config, null_policy)
    aed_score = calculate_aed_score(prepared_data["prepared_aed_data"], config.border)
    cpr_metrics = make_cpr_metrics(part_with_scores, config.calculation_config)

    return CalculationResult(
        cpr_total_scores=cpr_total_scores,
        part_with_scores=part_with_scores,
        cpr_metrics=cpr_metrics,
        aed_score=aed_score,
        comp_count=prepared_data["comp_count"],
        vent_count=prepared_data["vent_count"],
    )


def calculate_action(action_list, config) -> list[ActionWithScore]:
    return ActionEvaluator(config).evaluate_action(action_list)

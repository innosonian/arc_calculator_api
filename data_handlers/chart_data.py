from typing import TypedDict

from services.operational_logs import write_diagnostic
from services.calculation_context import CalculationExecutionContext

from config.constants import ACTION_TYPE_AED, ACTION_TYPE_COMP, PACKET_MS
from config.enums import Actor
from util.uploader import create_signed_url, upload_json_file


class ChartData(TypedDict):
    action_type: str
    comp_depth_max: int
    comp_depth_min: int
    comp_count: int
    vent_vol_max: int
    timestamp: float
    part_num: int
    cycle_num: int
    aed_overlapped_type: str | None
    is_virtual_action: bool


def add_chart_data(
    response: dict,
    prepared_data: dict,
    calculation_result: dict,
    stage: str = "prod",
    key_stem: str | None = None,
    org: str | None = None,
    *,
    execution_context: CalculationExecutionContext | None = None,
) -> dict:
    chart_data = make_chart_data(
        prepared_data["whole_cpr_action_list"],
        prepared_data["prepared_aed_data"],
        calculation_result["part_with_scores"],
    )
    if execution_context is not None:
        response["chart_dataset_url"] = execution_context.publish_chart(chart_data)
        return response
    # s3 signedurl로 리턴 (원본 바이너리/메타와 같은 stem·org prefix로 업로드)
    try:
        chart_key = upload_json_file(chart_data, stage=stage, key_stem=key_stem, org=org)
        signed_url = create_signed_url(chart_key, expires_in=300)
        response["chart_dataset_url"] = signed_url
    except Exception as e:
        # 검토 반영 2026-09-05: P8 CloudWatch 검색 가능한 JSON 한 줄 로그(lambda_handler._log 와 동일 골격)
        write_diagnostic("error", "chart_upload_failed", {"exception": e})
        response["chart_dataset_url"] = None

    return response


def make_chart_data(
    action_list: list[dict],
    aed_part_list: list[dict],
    cpr_scores: list[dict],
) -> dict:
    except_cycles = _get_calculated_cycles(cpr_scores)

    start_timestamp = 0.0
    end_timestamp = 0.0
    cpr_data_set = []
    comp_run = []  # 같은 파트·사이클·actor 구간 안에서 표시 순번을 매긴다.

    def flush_comp_run():
        if comp_run:
            cpr_data_set.extend(_compression_event_bars(comp_run))
            comp_run.clear()

    for action in action_list:
        if action["action_type"] == ACTION_TYPE_AED:
            continue
        calculated_cycle_key = f'part:{action["part_num"]}_{action.get("cycle_cnt")}'
        if action.get("cycle_cnt") and calculated_cycle_key not in except_cycles:
            continue

        if action["action_type"] == ACTION_TYPE_COMP:
            if comp_run and _comp_run_key(comp_run[0]) != _comp_run_key(action):
                flush_comp_run()
            comp_run.append(action)
            continue

        flush_comp_run()
        cpr_data_set.append(_action_chart_data(action))

    flush_comp_run()

    aed_data_set = [
        {
            "part_num": a["part_cnt"],
            "start_timestamp": a["aed_part_data"][0]["timestamp"] / 1000,
            "end_timestamp": a["aed_part_data"][-1]["timestamp"] / 1000,
        }
        for a in aed_part_list
    ]

    if cpr_data_set:
        start_timestamp = cpr_data_set[0]["timestamp"]
        end_timestamp = cpr_data_set[-1]["timestamp"]

    if aed_data_set:
        aed_start_timestamp = aed_data_set[0]["start_timestamp"]
        aed_end_timestamp = aed_data_set[-1]["end_timestamp"]
        start_timestamp = start_timestamp if start_timestamp < aed_start_timestamp else aed_start_timestamp
        end_timestamp = end_timestamp if end_timestamp > aed_end_timestamp else aed_end_timestamp

    return {
        "meta": {
            "start_timestamp": start_timestamp,
            "end_timestamp": end_timestamp,
        },
        "cpr_data_set": cpr_data_set if cpr_data_set else None,
        "aed_data_set": aed_data_set if aed_data_set else None,
    }


def _comp_run_key(action: dict) -> tuple:
    # 분절하지 않고 기존 Chart 순번의 파트·사이클·actor 범위만 유지한다.
    return (action["part_num"], action.get("cycle_cnt"), action.get("actor"))


def _action_chart_data(action: dict) -> ChartData:
    return ChartData(
        action_type=action["action_type"],
        comp_depth_max=max(action["compression_depth"]),
        comp_depth_min=min(action["compression_depth"]),
        comp_count=action.get("compression_count", 0),  # CountMarker가 부여한 사이클 순번
        vent_vol_max=max(action["ventilation_volume"]),
        timestamp=int((action["first_timestamp"] + action["last_timestamp"]) / 2) / 1000,
        part_num=action["part_num"],
        cycle_num=action.get("cycle_cnt"),
        aed_overlapped_type=action["overlap_type"],
        is_virtual_action=action.get("actor") == Actor.VIRTUAL_PARTNER,
    )


def _compression_event_bars(actions: list[dict]) -> list[ChartData]:
    return [
        ChartData(
            action_type=action["action_type"], comp_depth_max=action["depth_raw"],
            comp_depth_min=action["recoil_raw"], comp_count=i, vent_vol_max=0,
            timestamp=(action["peak_timestamp"] + action["peak_offset"] * (PACKET_MS // 10)) / 1000,
            part_num=action["part_num"], cycle_num=action.get("cycle_cnt"),
            aed_overlapped_type=action["overlap_type"],
            is_virtual_action=action.get("actor") == Actor.VIRTUAL_PARTNER,
        )
        for i, action in enumerate(actions, start=1)
    ]


def _get_calculated_cycles(cpr_scores: list[dict]) -> dict:
    result = {}
    for cpr_score in cpr_scores:
        for cycle_with_score in cpr_score["result"]["cycle_with_score_list"]:
            result[f'part:{cpr_score["part_num"]}_{cycle_with_score.cycle.cycle_num}'] = True

    return result

# 원본 hstm_v2 data_handlers/chart_data.py (V1 패리티 보존)
# S3 업로드는 util/uploader(arc prefix) 경유, presigned URL 만료 300초 유지.
# 검토 반영 2026-09-05: P8 업로드 예외 print 를 JSON 한 줄 구조화 로그(chart_upload_failed)로 교체,
# 사장된 chart_dataset_log 주석 잔재 제거. 그 외 로직 불변.
import json

from services.operational_logs import write_diagnostic
from services.calculation_context import CalculationExecutionContext
from typing import TypedDict

from calculators.waveform import drop_tap_events, segment_compressions
from config.constants import ACTION_TYPE_COMP, MINIMUM_COMP_DEPTH, PACKET_MS
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
    comp_run = []  # 같은 사이클의 연속 압박 액션 묶음 → 파형 이벤트 단위 바로 변환

    def flush_comp_run():
        if comp_run:
            cpr_data_set.extend(_compression_event_bars(comp_run))
            comp_run.clear()

    for action in action_list:
        calculated_cycle_key = f'part:{action["part_num"]}_{action.get("cycle_cnt")}'
        if action.get("cycle_cnt") and calculated_cycle_key not in except_cycles:
            continue

        run_key = (comp_run[0]["part_num"], comp_run[0].get("cycle_cnt")) if comp_run else None
        if run_key is not None and run_key != (action["part_num"], action.get("cycle_cnt")):
            flush_comp_run()
        if action["action_type"] == ACTION_TYPE_COMP:
            comp_run.append(action)
            continue
        # A simultaneous/intervening breath does not cut a same-cycle pressure
        # waveform into separate compression runs.
        cpr_data_set.append(_action_chart_data(action))

    flush_comp_run()
    # Bars and breaths were assembled separately; this only orders chart marks,
    # never source packets or detection events.
    cpr_data_set.sort(key=lambda mark: mark["timestamp"])

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


def _action_chart_data(action: dict) -> ChartData:
    return ChartData(
        action_type=action["action_type"],
        comp_depth_max=max(action["compression_depth"]),
        comp_depth_min=min(action["compression_depth"]),
        # F-6: compression_count 이중성(파트 포함 액션은 CountMarker가 int로 덮어쓰고,
        #      AED와 겹쳐 파트 제외된 액션은 패킷별 리스트로 남음)을 여기서 흡수 — 기기 스펙 확인 대기.
        comp_count=(
            action["compression_count"]
            if isinstance(action["compression_count"], int)
            else action["compression_count"][0]
        ),
        vent_vol_max=max(action["ventilation_volume"]),
        timestamp=int((action["first_timestamp"] + action["last_timestamp"]) / 2) / 1000,
        part_num=action["part_num"],
        cycle_num=action.get("cycle_cnt"),
        aed_overlapped_type=action["overlap_type"],
        is_virtual_action=action.get("actor") == Actor.VIRTUAL_PARTNER,
    )


def _compression_event_bars(actions: list[dict]) -> list[ChartData]:
    # 압박 바를 액션이 아니라 파형 이벤트(실제 압박 1회) 단위로 그린다. 경계 어긋남으로
    # 잘린 하강 꼬리·마커 윈도우가 가짜 바(예: 15회 압박이 16개로 표시)를 만들지 않는다.
    packets = []  # (샘플 묶음, 패킷 시각, 출처 액션)
    for action in actions:
        depths = action["compression_depth"]
        for i in range(0, len(depths), 10):
            source_times = action.get("_source_timestamps")
            timestamp = source_times[i // 10] if source_times is not None else action["first_timestamp"] + (i // 10) * PACKET_MS
            packets.append((depths[i : i + 10], timestamp, action))
    events = segment_compressions([p[0] for p in packets])

    # 신호 있는 액션들의 다수 actor로 가상파트너 여부를 정한다(꼬리·마커 윈도우의 actor 오염 방지).
    # F-7: 신호 판정 경계(depth > MINIMUM_COMP_DEPTH strict) — 기기 스펙 확인 대기.
    signal_actors = [a.get("actor") for a in actions if max(a["compression_depth"]) > MINIMUM_COMP_DEPTH]
    vp = sum(1 for actor in signal_actors if actor == Actor.VIRTUAL_PARTNER)
    is_virtual = bool(signal_actors) and vp > len(signal_actors) - vp

    # 실사용자 구간은 채점과 동일하게 탭 이벤트를 걸러 바 수 = 채점 압박 수를 유지한다.
    # VP 구간은 앱이 인위적으로 주입한 재생 데이터라 카운트 신호가 없어 보정하지 않는다.
    if not is_virtual:
        counts = []
        for action in actions:
            action_counts = action["compression_count"]
            counts.extend(action_counts if isinstance(action_counts, list) else [action_counts])
        increments = sum(1 for i in range(1, len(counts)) if counts[i] > counts[i - 1])
        events = drop_tap_events([p[0] for p in packets], events, increments)

    bars = []
    for i, event in enumerate(events, start=1):
        chunk, packet_ts, src = packets[event.peak_packet_index]
        sample_ms = PACKET_MS // max(len(chunk), 1)
        bars.append(
            ChartData(
                action_type=src["action_type"],
                comp_depth_max=event.peak_depth,
                comp_depth_min=event.recoil_depth,
                comp_count=i,
                vent_vol_max=0,
                timestamp=(packet_ts + event.peak_sample_offset * sample_ms) / 1000,
                part_num=src["part_num"],
                cycle_num=src.get("cycle_cnt"),
                aed_overlapped_type=src["overlap_type"],
                is_virtual_action=is_virtual,
            )
        )
    return bars


def _get_calculated_cycles(cpr_scores: list[dict]) -> dict:
    result = {}
    for cpr_score in cpr_scores:
        for cycle_with_score in cpr_score["result"]["cycle_with_score_list"]:
            result[f'part:{cpr_score["part_num"]}_{cycle_with_score.cycle.cycle_num}'] = True

    return result

"""Reference document conversion with approved null preservation and Fail precedence.

Ported from the supplied hstm_v2_calcuator_api/lambda_handler.py.
These legacy document fields do not define the future ARC submission contract.
"""

import os
import random

from services.config import Config
from services.http.schemas import DEFAULT_CONDITION, comp_vent_targets


def _number_or_default(values: dict, key: str) -> object:
    """Keep explicit calculated null; preserve the reference fallback for other values."""
    if key in values and values[key] is None:
        return None
    return values.get(key) or 0


def _has_null_overall(total_score: dict) -> bool:
    return "overall" in total_score and total_score["overall"] is None


def _build_hstm_document(body: dict, result: dict, condition: dict | None) -> dict | None:
    document = {}
    for key in _HSTM_TOP_LEVEL_KEYS:
        if body.get(key) is not None:
            document[key] = body[key]

    if not document:
        return None

    return _apply_calculated_fields(document, result, condition)


def _apply_calculated_fields(document: dict, result: dict, condition: dict | None) -> dict:
    merged = dict(document)
    merged["ResultSummary"] = _build_result_summary(
        merged.get("ResultSummary") or {},
        result,
        merged,
        condition,
    )
    merged["ResultByCycle"] = _build_result_by_cycle(
        merged.get("ResultByCycle") or {},
        result,
    )
    merged["ResultByCriteria"] = _build_result_by_criteria(
        merged.get("ResultByCriteria") or {},
        result,
    )
    # HSTM 문서의 정규 키는 대문자 Certification(_HSTM_TOP_LEVEL_KEYS와 동일). 
    # (소문자 result["certification"]는 계산기 응답 전용)
    merged["Certification"] = _build_certification(
        result,
        condition,
        merged.get("Custom"),
        merged.get("Open_Skill"),
        merged.get("ResultSummary"),
    )
    # 계산기가 생성한 가이드 프롬프트(소문자 guide_prompts)를 HSTM 문서 필드(Guide_prompts)로 매핑한다.
    merged["Guide_prompts"] = result.get("guide_prompts") or []
    # V1 앱이 채우던 Usage/Dummy 필드를 계산기가 완성한다(V2 앱은 HSTM에 직접 제출하지 않음).
    # 판정(_build_judg_result)은 앱 원값 Usage.Type을 보므로 ResultSummary 생성 이후에 적용한다.
    if merged.get("Usage") is not None:
        merged["Usage"] = _complete_usage(merged["Usage"], condition)
    if merged.get("Dummy") is not None:
        merged["Dummy"] = _complete_dummy(merged["Dummy"])
    return merged


# V1 앱(cprModeToString)이 보내던 Usage.Type 문자열. HSTM은 V1 실물 값으로 비교한다.
_V1_USAGE_TYPES = {
    "cpr": "CPR Training",
    "compression_only": "Chest compression only",
    "ventilation_only": "Ventilation only",
}


def _complete_usage(base: dict, condition: dict | None) -> dict:
    # V1 앱(createConfJSON)이 채우던 필드를 계산기가 완성한다.
    # Email은 access token에 claim이 없어 계산기가 만들 수 없는 값이므로 빈 문자열로
    # 키만 보장한다(실값은 앱이 채운다). Comment/Regional_Option/Hardware는 V1 앱도
    # 고정 문자열이었다. validNum은 V1 앱이 업로드 세션당 1회 생성하던 10자리 난수.
    usage = dict(base) if isinstance(base, dict) else {}
    if usage.get("Email") is None:
        usage["Email"] = ""
    if not usage.get("Comment"):
        usage["Comment"] = "This is comment field"
    if not usage.get("Regional_Option"):
        usage["Regional_Option"] = "usa"
    if _is_condition_assess_training(condition):
        usage["Type"] = "Assessment"
    else:
        usage["Type"] = _V1_USAGE_TYPES.get(_normalize_training_type(condition), "CPR Training")
    if usage.get("validNum") is None:
        # V1 범위(999999999 이상)의 부분집합이면서 10자리를 보장하도록 하한을 올린다.
        usage["validNum"] = random.randrange(1000000000, 9999999999)
    return usage


def _complete_dummy(base: dict) -> dict:
    dummy = dict(base) if isinstance(base, dict) else {}
    if not dummy.get("Hardware"):
        dummy["Hardware"] = "1.0"
    return dummy


def _build_result_summary(base: dict, result: dict, document: dict, condition: dict | None) -> dict:
    summary = dict(base)
    total_score = (result.get("cpr_score") or {}).get("total_score") or {}
    action_count = result.get("action_count") or {}
    training_stats = result.get("training_stats") or {}
    metrics = result.get("metrics") or {}
    ccf_metric = metrics.get("CCF") or {}
    open_skill = document.get("Open_Skill") or {}
    dummy = document.get("Dummy") or {}
    organization = document.get("Organization") or {}
    custom = document.get("Custom") or {}
    usage = document.get("Usage") or {}

    summary["CompressionNo"] = action_count.get("comp", 0)
    summary["VentilationNo"] = action_count.get("vent", 0)
    # ResultSummary는 V1 계산기와 동일하게 채점 점수가 아니라 실측 평균값을 담는다
    # (환기량 ml, 환기 속도 ms, 압박 속도 cpm, 깊이 mm, hands-off 초, CCF 비율 %).
    # 예외로 Handposition은 V1도 점수(사이클 평균)를 보낸다.
    summary["VentilationSpd"] = _number_or_default(metrics, "AvgVentilationSpeed")
    summary["VentilationVolume"] = _number_or_default(metrics, "AvgVentilationVolume")
    summary["CompressionRate"] = _number_or_default(metrics, "AvgCompressionRate")
    summary["CompressionDepth"] = _number_or_default(metrics, "AvgCompressionDepth")
    summary["HandsOffTime"] = _number_or_default(metrics, "HandsOffTimeSec")
    summary["%CCF"] = (
        None if "%_CCF" in ccf_metric and ccf_metric["%_CCF"] is None
        else _to_int(ccf_metric.get("%_CCF")) or 0
    )
    summary["Handposition"] = (
        None if "score_hand_position" in total_score and total_score["score_hand_position"] is None
        else _to_int(total_score.get("score_hand_position")) or 0
    )
    summary["CycleNum"] = training_stats.get("cycle_count", 0)
    # V1과 동일하게 compression only에서는 LungGraph를 만들지 않는다.
    # base(앱 전달분)에 온 값도 남기지 않는다.
    if _normalize_training_type(condition) != "compression_only":
        volume = _number_or_default(metrics, "AvgVentilationVolume")
        summary["LungGraph"] = None if volume is None else _build_lung_graph(volume, condition)
    else:
        summary.pop("LungGraph", None)
    summary["JudgResult"] = _build_judg_result(
        summary,
        total_score,
        condition,
        custom,
        usage,
    )
    summary["hStreamScore"] = total_score.get("overall", summary.get("hStreamScore"))
    summary["EventTime"] = _format_elapsed(training_stats.get("elapsed_seconds", 0))
    summary.setdefault("ManikinDeviceId", dummy.get("DeviceID"))
    summary.setdefault("OrgId", organization.get("org_id"))
    # HSTM이 완료 기록을 귀속할 학습자 ID(필수 필드. 누락 시 400 전량 거부 이력).
    # 스펙 필드명은 대문자 HstreamId이며, 앱이 소문자(hstreamId)로 보낸 값도 흡수해
    # 대문자 키 하나로만 전송한다. 앱이 키만 보내고 값이 비어 있으면(null 플레이스홀더 등)
    # 유효한 값으로 대체한다.
    app_hstream_id = summary.pop("hstreamId", None)
    if not summary.get("HstreamId"):
        summary["HstreamId"] = app_hstream_id or usage.get("hstreamId")

    if _has_null_overall(total_score):
        # A prior pass or success explanation cannot override an unscored result.
        # Do this before thresholds/withholding so null never becomes zero or '--'.
        summary["JudgResult"] = "Fail"
        summary["hStreamResult"] = "Fail"
        summary["hStreamScore"] = None
        summary.pop("hStreamReason", None)
        return summary

    passing_score = _to_int(open_skill.get("Passing_Score"))
    overall_score = _to_int(total_score.get("overall"))
    if passing_score is not None and overall_score is not None:
        hstream_result, hstream_reason = _build_hstream_judgement(
            summary, result, condition, overall_score, passing_score
        )
        summary["hStreamResult"] = hstream_result
        summary["hStreamReason"] = hstream_reason
        # 게이트 실패 사유는 모두 "No score will be provided."로 끝난다(총점 미달 Fail만 예외).
        # 이 경우 hStreamScore를 '--'로 숨길 수 있으나, HSTM 완료 API는 이 필드를 int로 검증하므로
        # 문자열 '--' 전송은 HSTM이 수용을 확인하기 전엔 500(완료 기록 전량 유실) 위험이다.
        # 그래서 기본은 OFF(숫자 유지)이고 HSTM_V2_WITHHOLD_SCORE로만 켠다. 앱은 화면 '--'를
        # 자체(client-side) 표시하므로 이 전송값과 무관하다(앱 UI는 항상 '--').
        if _withhold_score_enabled() and hstream_reason.endswith(_NO_SCORE_SUFFIX):
            summary["hStreamScore"] = _WITHHELD_SCORE

    return summary


# 게이트 실패 사유의 공통 접미사와, 그때 hStreamScore 대신 내보내는 표시값.
_NO_SCORE_SUFFIX = "No score will be provided."
_WITHHELD_SCORE = "--"


def _withhold_score_enabled() -> bool:
    # 게이트 실패 시 hStreamScore를 '--'로 숨겨 HSTM에 보낼지 여부. 기본 OFF(숫자 전송).
    # HSTM 완료 API가 문자열 '--'를 int 필드에 수용함을 dev에서 확인한 뒤에만 켠다(미수용 시 500).
    return os.getenv("HSTM_V2_WITHHOLD_SCORE", "").strip().lower() in ("1", "true", "yes", "on")


def _build_hstream_judgement(
    summary: dict,
    result: dict,
    condition: dict | None,
    overall_score: int,
    passing_score: int,
) -> tuple[str, str]:
    # V1 계산기(get_hstream_result)의 판정·문구 규칙 이식.
    # Demo 분기는 제외(V2에 Demo 모드 없음). 총점 >= Passing_Score 외에 추가 4게이트
    # (최소 시도량·사이클별 압박 횟수 점수 최솟값 50·Hand Position 80·Recoil 80)를 검사하며,
    # 하나라도 실패하면 총점이 기준점 이상이어도 Fail이다. 게이트 실패 사유는 모두
    # "No score will be provided."로 끝나며, 이때 호출부가 hStreamScore를 '--'로 대체한다.
    training_type = _normalize_training_type(condition)

    if _is_not_enough_attempt(summary, training_type):
        if overall_score == 0:
            return "Fail", "You did not attempt this skill.\n\nNo score will be provided."
        if overall_score < passing_score:
            return "Fail", "Not quite there yet. You stopped early.\n\nNo score will be provided."
        return "Fail", "You were doing well, but stopped early.\n\nNo score will be provided."

    if overall_score < passing_score:
        return "Fail", "You did not pass.\n\nYou must repeat the skill."

    if training_type == "cpr" and _is_not_enough_comp_no(result):
        # 압박 횟수는 CompVentRatio(cpr_cycle_type)에서 유도한다(30:2→30, 15:2→15). HSTM 전송 문자열에
        # 정확한 숫자가 들어가도록 채운다. 판정(합/불) 로직 자체는 바뀌지 않는다(문구의 숫자만 정합).
        comp_target, _vent_target = comp_vent_targets(condition)
        return (
            "Fail",
            f"You were doing well, but compress the chest {comp_target} times each cycle.\n\nNo score will be provided.",
        )

    if training_type != "ventilation_only":
        total_score = (result.get("cpr_score") or {}).get("total_score") or {}
        hand_position = _to_int(total_score.get("score_hand_position"))
        if hand_position is not None and hand_position < 80:
            return "Fail", "You were doing well, but compress in the middle of the chest.\n\nNo score will be provided."
        recoil = _to_int(total_score.get("score_recoil"))
        if recoil is not None and recoil < 80:
            return "Fail", "You were doing well, but allow the chest to fully recoil between compressions.\n\nNo score will be provided."

    return "Pass", "Well Done!\n\nYou passed!"


def _is_not_enough_attempt(summary: dict, training_type: str) -> bool:
    # V1과 동일한 최소 시도 기준: comp only 압박 60회, vent only 환기 12회, CPR 3사이클.
    if training_type == "compression_only":
        return (_to_int(summary.get("CompressionNo")) or 0) < 60
    if training_type == "ventilation_only":
        return (_to_int(summary.get("VentilationNo")) or 0) < 12
    return (_to_int(summary.get("CycleNum")) or 0) < 3


def _is_not_enough_comp_no(result: dict) -> bool:
    # V1과 동일: 사이클당 압박 수 점수의 최솟값이 50 미만이면 실패.
    # rescue vent·VP 사이클은 score_comp_no가 None이라 자연히 제외된다.
    values = [
        cycle.get("score_comp_no") for cycle in _collect_cycle_scores(result) if cycle.get("score_comp_no") is not None
    ]
    if not values:
        return False
    return min(values) < 50


def _normalize_training_type(condition: dict | None) -> str:
    if not isinstance(condition, dict):
        return "cpr"
    return str(condition.get("training_type") or "cpr").strip().lower()


def _build_lung_graph(avg_volume: int, condition: dict | None) -> dict:
    # V1 계산기(get_lung_graph_gauge) 그대로: 평균 환기량 하나로 percent/level/color를
    # 만들어 좌우에 동일 복제한다(마네킨에 좌/우 폐 센서가 없음). 색은 V1과 같은 이름 표기.
    border = Config(dict(DEFAULT_CONDITION, **(condition or {}))).border.get_border("vent_vol")
    volume = avg_volume or 0
    if volume < border[1]:
        color = "grey"
    elif volume < border[2]:
        color = "blue"
    else:
        color = "red"

    percent = int(min((volume / border[2]) * 100, 100))
    return {
        "LeftPercent": percent,
        "LeftLevel": int(volume),
        "LeftColor": color,
        "RightPercent": percent,
        "RightLevel": int(volume),
        "RightColor": color,
    }


def _build_certification(
    result: dict,
    condition: dict | None,
    custom: dict | None,
    open_skill: dict | None,
    result_summary: dict | None,
) -> dict:
    target = _normalize_target((condition or {}).get("target"))
    required = _get_required_certifications(custom)
    if not required or target not in required:
        return {"Target": "N/A"}

    if _is_pass(result, result_summary, custom, open_skill, target):
        return {"Target": "baby" if target == "infant" else target}
    return {"Target": "N/A"}


def _normalize_target(target: str | None) -> str:
    if not target:
        return "adult"
    return str(target).strip().lower()


def _get_required_certifications(custom: dict | None) -> set[str]:
    if not isinstance(custom, dict):
        return set()
    required: set[str] = set()
    if custom.get("CertificateAdult"):
        required.add("adult")
    if custom.get("CertificateBaby"):
        required.add("baby")
    if custom.get("CertificateChild"):
        required.add("child")
    if custom.get("CertificateInfant"):
        required.add("infant")
    return required


def _is_pass(
    result: dict,
    result_summary: dict | None,
    custom: dict | None,
    open_skill: dict | None,
    target: str,
) -> bool:
    total_score = (result.get("cpr_score") or {}).get("total_score") or {}
    if _has_null_overall(total_score):
        return False
    if isinstance(result_summary, dict):
        judg_result = result_summary.get("JudgResult")
        if judg_result == "Pass":
            return True
        if judg_result == "Fail":
            return False
        hstream_result = result_summary.get("hStreamResult")
        if hstream_result == "Pass":
            return True
        if hstream_result == "Fail":
            return False

    overall = _to_int(total_score.get("overall"))
    if overall is None:
        return False

    threshold = _get_pass_threshold(custom, open_skill, target)
    if threshold is None:
        return False
    return overall >= threshold


def _get_pass_threshold(custom: dict | None, open_skill: dict | None, target: str) -> int | None:
    if isinstance(open_skill, dict):
        value = _to_int(open_skill.get("Passing_Score"))
        if value is not None:
            return value
    if isinstance(custom, dict):
        if target == "child":
            value = _to_int(custom.get("PassThresholdChild"))
            if value is not None:
                return value
        value = _to_int(custom.get("PassThreshold"))
        if value is not None:
            return value
    return 80


def _build_judg_result(
    summary: dict,
    total_score: dict,
    condition: dict | None,
    custom: dict | None,
    usage: dict | None,
) -> str:
    if _has_null_overall(total_score):
        return "Fail"
    overall_score = _to_int(total_score.get("overall")) or 0
    cycle_num = _to_int(summary.get("CycleNum")) or 0
    comp_no = _to_int(summary.get("CompressionNo")) or 0
    vent_no = _to_int(summary.get("VentilationNo")) or 0
    target = _normalize_target((condition or {}).get("target"))

    if _is_custom_assess_training(custom):
        if not _is_custom_exceed_pass_fail_condition(custom, cycle_num, comp_no, vent_no):
            return "Fail"
        threshold = _get_pass_threshold(custom, None, target) or 0
        return "Pass" if overall_score >= threshold else "Fail"

    if _is_usage_assess_training(usage) or _is_condition_assess_training(condition):
        threshold = _get_pass_threshold(custom, None, target) or 0
        if cycle_num >= 3 and overall_score >= threshold:
            return "Pass"
        return "Fail"

    return "N/A"


def _is_usage_assess_training(usage: dict | None) -> bool:
    if not isinstance(usage, dict):
        return True
    value = usage.get("Type")
    if value is None:
        return True
    if str(value).strip() == "":
        return True
    return str(value) == "Assessment"


def _is_condition_assess_training(condition: dict | None) -> bool:
    if not isinstance(condition, dict):
        return False
    return str(condition.get("mode", "")).lower() == "assessment"


def _is_custom_assess_training(custom: dict | None) -> bool:
    train_course = _get_train_course(custom)
    if not train_course:
        return False
    value = train_course.get("Certification")
    if value is None:
        return False
    if str(value).strip() == "":
        return False
    return str(value).lower() == "true"


def _is_custom_exceed_pass_fail_condition(
    custom: dict | None,
    cycle_num: int,
    comp_no: int,
    vent_no: int,
) -> bool:
    train_course = _get_train_course(custom)
    stop = train_course.get("StopCondition") if train_course else None
    if not isinstance(stop, dict):
        return False
    finish_cycle = _to_int(stop.get("finish_cycle"))
    if finish_cycle and finish_cycle > cycle_num:
        return False
    finish_comp = _to_int(stop.get("finish_compression"))
    if finish_comp and finish_comp * 0.95 > comp_no:
        return False
    finish_vent = _to_int(stop.get("finish_ventilation"))
    if finish_vent and finish_vent * 0.95 > vent_no:
        return False
    return True


def _get_train_course(custom: dict | None) -> dict:
    if not isinstance(custom, dict):
        return {}
    train_course = custom.get("TrainCourse")
    return train_course if isinstance(train_course, dict) else {}


def _build_result_by_cycle(base: dict, result: dict) -> dict:
    total_score = (result.get("cpr_score") or {}).get("total_score") or {}
    cycles = _collect_cycle_scores(result)

    mapping = {
        "CompressionDepth": "score_comp_depth",
        "CompressionRate": "score_comp_rate",
        "Recoil": "score_recoil",
        "HandPosition": "score_hand_position",
        "CompressionNo": "score_comp_no",
        "VentilationVolume": "score_vent_vol",
        "VentilationRate": "score_vent_rate",
        "ScoreOfCCF": "score_ccf",
        "ScoreByCycle": "overall",
    }

    result_by_cycle = dict(base)
    # V1 와이어 키는 Recoil이다(HSTM QA가 V1 실물 키로 검증. "CompressionRelease"는
    # V1/V2 앱 모두 화면 표시명일 뿐 페이로드 키가 아님). base에 구 개명 키로 온 값은 남기지 않는다.
    result_by_cycle.pop("CompressionRelease", None)
    for key, score_key in mapping.items():
        overall = total_score.get(score_key, 0)
        by_cycle = _build_cycle_values(cycles, score_key)
        result_by_cycle[key] = {
            "ByCycle": by_cycle,
            "Overall": overall,
        }

    # Preserve a calculated null in the nested score; missing source keys retain
    # the reference empty object instead of inventing a calculated value.
    vent_speed_overall = total_score.get("score_vent_speed")
    if "score_vent_speed" in total_score:
        result_by_cycle["VentilationSpeed"] = {
            "ByCycle": _build_cycle_values(cycles, "score_vent_speed"),
            "Overall": vent_speed_overall,
        }
    else:
        result_by_cycle["VentilationSpeed"] = {}

    return result_by_cycle


def _build_result_by_criteria(base: dict, result: dict) -> dict:
    metrics = result.get("metrics") or {}
    result_by_criteria = dict(base)

    for key in (
        "CompressionDepth", "CompressionRate", "Recoil", "HandPosition", "CompressionNo",
        "VentilationVolume", "VentilationRate", "VentilationSpeed",
    ):
        if key in metrics and (metrics[key] is None or isinstance(metrics[key], dict)):
            result_by_criteria[key] = metrics[key]
    # Explicit null also replaces a stale input metric. Missing metrics retain
    # the original fallback and passthrough behavior.
    result_by_criteria.setdefault("VentilationSpeed", {})

    # V1 와이어 키는 Recoil이다. base(앱 전달분)에 구 개명 키(CompressionRelease)로
    # 온 값은 Recoil로 옮기고 구 키는 남기지 않는다.
    stale_release = result_by_criteria.pop("CompressionRelease", None)
    if stale_release is not None:
        result_by_criteria.setdefault("Recoil", stale_release)

    if "ScoreOfCCF" in metrics:
        result_by_criteria["ScoreOfCCF"] = metrics["ScoreOfCCF"]

    return result_by_criteria


def _collect_cycle_scores(result: dict) -> list[dict]:
    cycles: list[dict] = []
    part_scores = (result.get("cpr_score") or {}).get("part_scores") or []
    for part in part_scores:
        for cycle in part.get("cycle_with_score_list") or []:
            if isinstance(cycle, dict):
                cycles.append(cycle)
    return cycles


def _build_cycle_values(cycles: list[dict], key: str) -> list[int | float | None]:
    if not cycles:
        return [0]
    values = []
    for cycle in cycles:
        values.append(cycle.get(key, 0))
    return values


def _format_elapsed(elapsed_seconds: float) -> str:
    try:
        total_seconds = int(elapsed_seconds or 0)
    except (TypeError, ValueError):
        total_seconds = 0
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}"


def _to_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


_HSTM_TOP_LEVEL_KEYS = {
    "DeviceInfo",
    "Organization",
    "Dummy",
    "Open_Skill",
    "ResultSummary",
    "Custom",
    "ResultByCycle",
    "CalculationService",
    "Certification",
    "Usage",
    "ResultByCriteria",
    "Institution",
}

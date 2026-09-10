# 이식 출처: 원본 hstm_v2 services/calculate_cpr.py (97줄) — 동작 동일 이식.
import json

from services.operational_logs import write_diagnostic
import time

from data_handlers.chart_data import add_chart_data
from services.calculators import calculate_cpr
from services.calculation_context import CalculationExecutionContext
from services.config import Config
from services.serializers import serialize_result


def make_calculate_result(
    config: Config,
    prepared_data: dict,
    stage: str = "prod",
    usage: dict | None = None,
    key_stem: str | None = None,
    org: str | None = None,
    *,
    execution_context: CalculationExecutionContext | None = None,
) -> dict:
    start_time = time.time()
    _log(
        "info",
        "calc_start",
        stage=stage,
        prepared_cpr_count=len(prepared_data.get("prepared_cpr_data", [])),
        prepared_aed_count=len(prepared_data.get("prepared_aed_data", [])),
        whole_action_count=len(prepared_data.get("whole_cpr_action_list", [])),
        comp_count=prepared_data.get("comp_count"),
        vent_count=prepared_data.get("vent_count"),
    )

    try:
        calc_start = time.time()
        calculation_result = calculate_cpr(prepared_data, config)
        if execution_context is not None:
            execution_context.observe_calculation(calculation_result)
        _log("info", "calc_complete", elapsed_ms=_elapsed_ms(calc_start))
    except Exception as e:
        _log(
            "error",
            "calc_failed",
            step="calculate_cpr",
            error_type=type(e).__name__,
            exception=e,
        )
        raise

    try:
        serialize_start = time.time()
        response = serialize_result(calculation_result, condition=config.condition, usage=usage)
        _log("info", "serialize_complete", elapsed_ms=_elapsed_ms(serialize_start))
    except Exception as e:
        _log(
            "error",
            "calc_failed",
            step="serialize_result",
            error_type=type(e).__name__,
            exception=e,
        )
        raise

    try:
        chart_start = time.time()
        context_kwargs = {} if execution_context is None else {"execution_context": execution_context}
        response = add_chart_data(
            response,
            prepared_data,
            calculation_result,
            stage,
            key_stem=key_stem,
            org=org,
            **context_kwargs,
        )
        _log("info", "chart_complete", elapsed_ms=_elapsed_ms(chart_start))
    except Exception as e:
        _log(
            "error",
            "calc_failed",
            step="add_chart_data",
            error_type=type(e).__name__,
            exception=e,
        )
        raise

    _log("info", "calc_response_complete", elapsed_ms=_elapsed_ms(start_time))
    return response


def _elapsed_ms(start_time: float) -> int:
    return int((time.time() - start_time) * 1000)


def _log(level: str, message: str, **fields: object) -> None:
    write_diagnostic(level, message, fields)

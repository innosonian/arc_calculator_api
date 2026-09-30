# 이식 출처: 원본 hstm_v2 services/calculate_cpr.py (97줄) — 동작 동일 이식.
# 단계별 try/except·로그 블록은 _step 문맥 관리자 하나로 모았다(로그 이름·필드·순서·예외 전파
# 동일). 예외는 make_calculate_result 프레임에서 잡히므로 저장 진단의 stacktrace 첫 프레임도
# 원본과 같이 make_calculate_result 다(내부 함수·헬퍼 프레임이 끼지 않는다).
import time

from services.operational_logs import write_diagnostic

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

    # Observer failures stay inside the calculate_cpr step (existing diagnostic contract).
    with _step("calculate_cpr", "calc_complete"):
        # D139: the owning adapter version's minimum-quantity option; a call
        # without a context keeps the existing two-argument form (current rule).
        policy_kwargs = ({} if execution_context is None
                         else {"minimum_quantity_null": execution_context.options.minimum_quantity_null})
        calculation_result = calculate_cpr(prepared_data, config, **policy_kwargs)
        if execution_context is not None:
            execution_context.observe_calculation(calculation_result)

    with _step("serialize_result", "serialize_complete"):
        response = serialize_result(calculation_result, condition=config.condition, usage=usage)

    with _step("add_chart_data", "chart_complete"):
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

    _log("info", "calc_response_complete", elapsed_ms=_elapsed_ms(start_time))
    return response


class _step:
    """One calculation step with the existing complete/failed diagnostics.

    A plain ``__exit__`` (not a generator context manager) so the failure is
    logged with the exception's traceback as ``make_calculate_result`` sees it:
    the stored stacktrace starts at that frame, as in the original try/except
    blocks. The elapsed window covers only the ``with`` body; a failure is
    logged once as calc_failed with this step name and re-raised unchanged.
    Seams (calculate_cpr, serialize_result, add_chart_data, _log) are module
    globals read when the step runs, so tests and scripts may replace them.
    """

    def __init__(self, step: str, completed: str) -> None:
        self.step, self.completed = step, completed

    def __enter__(self) -> "_step":
        self.start = time.time()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc is None:
            try:
                _log("info", self.completed, elapsed_ms=_elapsed_ms(self.start))
            except Exception as e:
                # As in the original blocks, where the completion log sat inside the try.
                self._failed(e)
                raise
        elif isinstance(exc, Exception):
            self._failed(exc)
        return False

    def _failed(self, error: Exception) -> None:
        _log(
            "error",
            "calc_failed",
            step=self.step,
            error_type=type(error).__name__,
            exception=error,
        )


def _elapsed_ms(start_time: float) -> int:
    return int((time.time() - start_time) * 1000)


def _log(level: str, message: str, **fields: object) -> None:
    write_diagnostic(level, message, fields)

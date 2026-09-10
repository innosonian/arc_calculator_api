"""계산 오케스트레이터(라이브러리 코어).

원본 V1 main.py 이식(V1 패리티 보존). lambda_handler와 scripts/run_local.py가 공유하는
코어이며, 로컬 실행 엔트리(`__main__` 블록)는 원본과 동일하게 두지 않는다.

원본 대비 미이식(스펙 §4.4·D-1): run_calculator_as_action(원본 main.py:82-101) —
action 직접 입력 경로 제거. (원본에서도 호출부가 없고 prepare_data 인자 불일치로
호출 시 TypeError가 나는 사장 코드였다.)
"""

import json

from services.operational_logs import write_diagnostic

from services.calculate_cpr import make_calculate_result
from services.calculation_context import CalculationContextError, CalculationExecutionContext
from services.config import Config
from services.http.schemas import ConditionType, DEFAULT_CONDITION, VPEvent, ParsedData
from services.preparers import parse_data, prepare_data, make_pre_action_list
from util.uploader import build_key_stem, build_raw_input_meta, org_prefix, upload_raw_input


def run_calculator(
    cpr_byte_data: bytes,
    aed_byte_data: bytes,
    condition: ConditionType = DEFAULT_CONDITION,
    vp_event_list: list[VPEvent] = None,
    usage: dict | None = None,
    stage: str = "prod",
    organization: dict | None = None,
    *,
    execution_context: CalculationExecutionContext | None = None,
) -> dict:
    # 원본 hstm_v2 main.py:10-61 (파이프라인 순서 그대로 — 임의 변경 금지)
    # initialize
    config = Config(condition)

    # 차트·바이너리·메타가 공유할 stem과 org prefix를 먼저 만든다(1:1 연결)
    if execution_context is None:
        key_stem = build_key_stem()
        org = org_prefix((organization or {}).get("org_id"))

        # 기존 경로는 진단 저장 실패가 계산을 막지 않는 동작을 유지한다.
        _save_raw_input(cpr_byte_data, aed_byte_data, condition, vp_event_list, organization, key_stem, org, stage)
    else:
        # 저장 계층이 접수한 원본과 일치하는 경우에만 같은 저장 이름을 재사용한다.
        if type(execution_context) is not CalculationExecutionContext:
            raise CalculationContextError("Invalid calculation execution context")
        receipt = execution_context.begin(cpr_byte_data, aed_byte_data)
        key_stem, org = receipt.key_stem, receipt.org

    # 패킷 to rtdata
    parsed_data = ParsedData(
        **parse_data(
            cpr_byte_data,
            aed_byte_data,
            config,
        ),
    )
    # rtdata to action data list
    action_list, aed_part_list = make_pre_action_list(
        config,
        parsed_data,
        vp_event_list,
    )

    # action data에 이런저런 양념치기. 계산에 필요한 데이터 준비는 이 단계에서 마무리한다
    prepared_data = prepare_data(
        action_list,
        aed_part_list,
        config,
    )

    context_kwargs = {} if execution_context is None else {"execution_context": execution_context}
    result = make_calculate_result(
        config,
        prepared_data,
        stage,
        usage=usage,
        key_stem=key_stem,
        org=org,
        **context_kwargs,
    )

    return result


def _save_raw_input(cpr_byte_data, aed_byte_data, condition, vp_event_list, organization, key_stem, org, stage):
    # 원본 hstm_v2 main.py:64-79 (실패 삼킴·JSON 한 줄 print 로깅 패턴 현행 유지 — 스펙 R3-6/D-4)
    try:
        meta = build_raw_input_meta(condition, vp_event_list, organization)
        upload_raw_input(cpr_byte_data, aed_byte_data, meta, stage=stage, key_stem=key_stem, org=org)
    except Exception as e:
        write_diagnostic("error", "raw_input_save_failed", {"exception": e})

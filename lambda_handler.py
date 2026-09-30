"""ARC calculator HTTP API with reference-compatible wire formats.

Compatibility exceptions and verification are tracked in
docs/ARC_MOCK_IMPLEMENTED_API_CONTRACT_KO.md.
The legacy document schema is not an ARC submission specification.
"""

from mock_journey.bootstrap import configure_imports

configure_imports()

import json
import os
import time

from main import run_calculator
from services.operational_logs import write_diagnostic
from services.observability import sentry_privacy_options
from services.http.service import parse_body

# Legacy wire parsers, validators and fixed 400 messages live in the neutral
# services.http.legacy_request module (no boto3/main/Sentry imports), so the
# public measurement bridge does not import this entry module. Only the names
# _run_trusted_calculation itself uses are imported here (D126); tests read
# the other fixed messages, allowed sets and _VP_EVENT_IDS from
# services.http.legacy_request directly. ClientError stays the same class
# (isinstance and the "ClientError" log name are unchanged).
from services.http.legacy_request import (
    ClientError,
    _MSG_SANITIZED_CLIENT_ERROR,
    _get_content_type,
    _parse_multipart_body,
    _validate_legacy_document,
    _validate_request,
)
# Historic re-exports of the pinned legacy_document helpers still read through
# this module: tests/test_certification.py (_get_pass_threshold,
# _build_certification) and scripts/verify_reference_parity.py
# (_convert_result_to_legacy, which also runs against the reference checkout).
from services.legacy_document import (  # noqa: F401 - re-exported surface
    _build_certification,
    _get_pass_threshold,
)

from services.legacy_response import _convert_result_to_legacy, finalize_legacy_response  # noqa: F401
from services.submission_response import compose_calculation_response


def run(event, context):
    """Public Lambda entrypoint: all requests use the authenticated API router."""
    from mock_journey.handler import run as run_authenticated

    return run_authenticated(event, context)


def _run_trusted_calculation(event, context):
    """Internal compatibility helper; never configure this as an HTTP entrypoint.

    Public requests must use ``run`` and its session/attempt acceptance boundary.
    This helper retains the existing parser/calculator contract for internal
    callers and regression tests; it does not authorize an app request.
    """
    # Reference wire parsing and result conversion with ARC operational guards.
    _init_sentry()
    start_time = time.time()
    request_id = context.aws_request_id if context else "local"
    stage = os.getenv("STAGE")
    headers = event.get("headers") or {}
    content_type = _get_content_type(headers)
    is_base64_encoded = event.get("isBase64Encoded", False)

    source_ip = _source_ip(event, headers)
    user_agent = headers.get("User-Agent") or headers.get("user-agent")
    content_length = headers.get("Content-Length") or headers.get("content-length")

    _log(
        "info",
        "request_start",
        request_id=request_id,
        stage=stage,
        path=event.get("path"),
        http_method=event.get("httpMethod"),
        content_type=content_type,
        is_base64_encoded=is_base64_encoded,
        source_ip=source_ip,
        user_agent=user_agent,
        content_length=content_length,
    )

    try:
        parse_start = time.time()
        if content_type and "multipart/form-data" in content_type.lower():
            _log("info", "parse_multipart_start", request_id=request_id)
            body = _parse_multipart_body(event.get("body") or "", headers, is_base64_encoded)
        else:
            _log("info", "parse_form_start", request_id=request_id)
            body = parse_body(event.get("body") or "")

        parse_ms = int((time.time() - parse_start) * 1000)
        condition = body.get("condition") or {}
        condition_fields = condition if isinstance(condition, dict) else {}
        vp_event_list = body.get("vp_event_list")
        _log(
            "info",
            "parse_complete",
            request_id=request_id,
            cpr_bytes=len(body.get("cpr_b64_data") or b""),
            aed_bytes=len(body.get("aed_b64_data") or b""),
            # 검토 반영 2026-09-05: O8 비-list(int 등)면 len()이 TypeError→500이었음. list일 때만 개수.
            vp_event_count=len(vp_event_list) if isinstance(vp_event_list, list) else None,
            condition=condition,
            condition_mode=condition_fields.get("mode"),
            condition_target=condition_fields.get("target"),
            condition_training_type=condition_fields.get("training_type"),
            condition_guideline=condition_fields.get("guideline"),
            condition_cpr_cycle_type=condition_fields.get("cpr_cycle_type"),
            condition_is_2rescuers=condition_fields.get("is_2rescuers"),
            parse_ms=parse_ms,
        )

        # 스펙 §5.7: 패킷 파싱/설정 팩토리 진입 전 명시 검증. 통과 입력의 이후 동작은 원본과 동일.
        _validate_request(body)
        _validate_legacy_document(body)

        calc_start = time.time()
        result = run_calculator(
            body["cpr_b64_data"],
            body["aed_b64_data"],
            body["condition"],
            body["vp_event_list"],
            usage=body.get("Usage"),
            stage=stage,
            organization=body.get("Organization"),
        )
        result = finalize_legacy_response(body, result)
        calc_ms = int((time.time() - calc_start) * 1000)
        # 검토 반영 2026-09-05: P4 응답 직렬화를 try 안에서 수행 — 비직렬화 값(set 등)이 섞이면
        # 핸들러 밖 TypeError(Lambda 자체 오류) 대신 아래 500 server_error 계약으로 떨어진다.
        try:
            app_result = compose_calculation_response(result)
        except ValueError:
            # Invalid calculator output is a server error, not invalid input.
            raise TypeError("Invalid calculation result.") from None
        response_body = json.dumps(app_result)
    except ValueError as e:
        from sentry_sdk import capture_exception

        capture_exception(e)
        _log(
            "warning",
            "request_failed",
            request_id=request_id,
            error_type=type(e).__name__,
            exception=e,
        )
        # 스펙 §5.7: 의도적으로 만든 정제 메시지(ClientError)만 그대로 노출하고, 그 외 ValueError
        # (CPR 길이 오류·b64/파싱 오류 등)는 내부 예외 문자열을 노출하지 않는다.
        message = str(e) if isinstance(e, ClientError) else _MSG_SANITIZED_CLIENT_ERROR
        _sentry_flush()
        return {
            "statusCode": 400,
            "body": json.dumps({"type": "client_error", "message": message}),
        }
    except Exception as e:
        from sentry_sdk import capture_exception

        capture_exception(e)
        _log(
            "error",
            "request_failed",
            request_id=request_id,
            error_type=type(e).__name__,
            exception=e,
        )
        _sentry_flush()
        return {
            "statusCode": 500,
            "body": json.dumps({"type": "server_error", "message": "Internal server error"}),
        }

    elapsed_ms = int((time.time() - start_time) * 1000)
    _log(
        "info",
        "request_complete",
        request_id=request_id,
        elapsed_ms=elapsed_ms,
        parse_ms=parse_ms,
        calc_ms=calc_ms,
    )

    return {
        "statusCode": 200,
        "body": response_body,
    }


def _init_sentry() -> None:
    # 원본 hstm_v2 lambda_handler.py:152-161 — 매 요청 init, 샘플링 1.0 현행 유지(스펙 R3-6/G-5)
    # 검토 반영 2026-09-05: O5 init 실패(BadDsn 등)는 로그만 남기고 요청 처리를 계속한다.
    # 검토 반영 2026-09-05: O7 init 전에 이전 요청의 client를 정리(웜 컨테이너 누적 방지).
    # 검토 반영 2026-09-05: O6 include_local_variables=False — 로컬변수(PII 가능) 전송 차단.
    try:
        import sentry_sdk

        try:
            sentry_sdk.get_client().close(timeout=0)
        except Exception:
            pass

        # DSN 은 공백 제거 후 그대로 넘긴다. 빈 값이면 "" — None 을 넘기면 SDK 가 SENTRY_DSN env 를
        # 다시 읽어 공백 DSN 에서 BadDsn 이 나므로, 빈 문자열(SDK 비활성·전송 없음, env 미설정 시의
        # dsn=None 과 동일하게 transport 없음)로 고정한다.
        sentry_sdk.init(
            dsn=(os.getenv("SENTRY_DSN") or "").strip(),
            environment=os.getenv("STAGE"),
            attach_stacktrace=True,
            **sentry_privacy_options(),
            traces_sample_rate=1.0,
            profiles_sample_rate=1.0,
        )
    except Exception as e:
        _log("error", "sentry_init_failed", exception=e)


def _sentry_flush() -> None:
    # 검토 반영 2026-09-05: O7 오류 응답 반환 직전 이벤트 전송 대기(Lambda freeze 전 유실 방지).
    # 지연 import 패턴 유지. DSN 미설정(NonRecordingClient)이면 즉시 반환한다.
    try:
        import sentry_sdk

        sentry_sdk.flush(timeout=2)
    except Exception:
        pass


def _source_ip(event: dict, headers: dict) -> str | None:
    # 검토 반영 2026-09-05: P6 requestContext.identity가 None/비-dict여도 안전(원본은
    # `.get("identity", {})`가 None을 돌려줄 때 핸들러 밖 AttributeError). 우선순위·폴백 규칙은 동일.
    request_context = event.get("requestContext")
    identity = request_context.get("identity") if isinstance(request_context, dict) else None
    identity_ip = identity.get("sourceIp") if isinstance(identity, dict) else None
    return (
        identity_ip
        or (headers.get("X-Forwarded-For") or headers.get("x-forwarded-for") or "").split(",")[0].strip()
        or None
    )


def _log(level: str, message: str, **fields: object) -> None:
    # 원본 hstm_v2 lambda_handler.py:310-316 — JSON 한 줄 print(CloudWatch용) 패턴 현행 유지
    write_diagnostic(level, message, fields)

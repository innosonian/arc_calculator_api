"""ARC calculator HTTP API with reference-compatible wire formats.

Compatibility exceptions and verification are tracked in
docs/ARC_CPR_POLICY_AND_HSTM_COMPARISON_KO.md.
The legacy document schema is not an ARC submission specification.
"""

from mock_journey.bootstrap import configure_imports

configure_imports()

import base64
import json
import os
import time
from email.parser import BytesParser

from config.constants import (
    EVENT_ID_END_AED,
    EVENT_ID_END_COMP,
    EVENT_ID_END_VENT,
    EVENT_ID_START_AED,
    EVENT_ID_START_COMP,
    EVENT_ID_START_VENT,
)
from main import run_calculator
from services.operational_logs import write_diagnostic
from services.observability import sentry_privacy_options
from services.http.schemas import DEFAULT_CONDITION
from services.http.service import parse_body
from services.legacy_document import (
    _HSTM_TOP_LEVEL_KEYS,
    _apply_calculated_fields,
    _build_certification,
    _build_hstm_document,
    _get_pass_threshold,
    _normalize_target,
    _to_int,
)

from services.legacy_response import _convert_result_to_legacy, finalize_legacy_response
from services.submission_response import compose_calculation_response

# 참고 프로젝트에서 실제 계산 테이블이 있는 guideline을 수용한다.
# 입력 검증·오류 정제 및 ARC 운영 설정은 사용자가 승인한 호환성 예외다.
_SUPPORTED_GUIDELINES = {"AHA2020", "ARC2020", "ARC2025", "ERC2020", "STD2015"}
_SUPPORTED_TARGETS = {"adult", "child", "infant"}
_SUPPORTED_TRAINING_TYPES = {"cpr", "compression_only", "ventilation_only"}

# 스펙 §5.7: guideline 오류 문구는 고정(NEW-3/D-3 확정). 나머지 400 문구는 스펙이 "정제 메시지"만
# 요구하므로 아래 고정 문구를 계약으로 삼는다(테스트에서 이 문자열 그대로 검증).
_MSG_UNSUPPORTED_GUIDELINE = "This guideline is not supported."
_MSG_UNSUPPORTED_TARGET = "This target is not supported."
_MSG_UNSUPPORTED_TRAINING_TYPE = "This training type is not supported."
_MSG_CPR_FILE_REQUIRED = "CPR file is required."
# 스펙 §5.7: 명시 검증·파서 밖(계산 파이프라인 내부 등)에서 발생한 ValueError는 내부 예외
# 문자열(traceback성 메시지)을 노출하지 않고 이 고정 문구로 응답한다(원본은 str(e) 노출 — :117).
_MSG_SANITIZED_CLIENT_ERROR = "Invalid request data."

# 검토 반영 2026-09-05: O8 vp_event_list 원소의 event 허용값. data_handlers/vp_action.py가
# `item.get("event") in [START..., END...]`(== 비교, 리스트 멤버십)로 소비하므로 같은 의미론을
# 위해 set이 아닌 tuple을 쓴다(0.0·True 같은 동치값은 원본과 동일하게 통과). 이 6종 외의 event는
# 원본에서 VP 이벤트 dict가 rtdata 패킷으로 흘러들어 KeyError→500이었다.
_VP_EVENT_IDS = (
    EVENT_ID_START_COMP,
    EVENT_ID_END_COMP,
    EVENT_ID_START_VENT,
    EVENT_ID_END_VENT,
    EVENT_ID_START_AED,
    EVENT_ID_END_AED,
)


class ClientError(ValueError):
    """메시지를 클라이언트에 그대로 노출해도 되는 400용 예외(스펙 §5.7).

    이 타입이 아닌 ValueError는 400은 유지하되 메시지를 _MSG_SANITIZED_CLIENT_ERROR로 정제한다.
    """


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


def _get_content_type(headers: dict) -> str | None:
    # 원본 hstm_v2 lambda_handler.py:164-168 — 헤더 키 대소문자 무시 조회
    for key, value in (headers or {}).items():
        if key.lower() == "content-type":
            return value
    return None


def _validate_request(body: dict) -> None:
    """파싱된 요청 바디의 기존 명시 검증.

    검증 순서는 guideline → target → training_type → CPR 파일이며, 복수 위반 시 첫 항목의
    메시지로 응답한다. 헤더 매직바이트 불일치는 여기서 걸러지지 않고 원본대로 200 전부
    0점으로 흘러간다(스펙 §5.7 현행 유지 항목).

    검토 반영 2026-09-05: O8 타입 검증(위 4개 검증 뒤에 이어서, 메시지·순서 불변).
    원칙 — "수정 전 500(TypeError/AttributeError/KeyError)이던 입력만 400으로 바꾸고,
    수정 전 200이던 입력은 그대로 200". 따라서 소비 코드가 `x or {}` / `if not x` 로 흡수하던
    falsy 값({}, "", 0, false, null, [])은 여기서도 거부하지 않는다.
    - condition.guideline/target/training_type: str이 아니면(list/dict 등 — 원본은 set 멤버십에서
      TypeError→500) 각 기존 고정 문구로 400.
    - vp_event_list: truthy 비-list → 400 정제 문구. list 원소는 data_handlers/vp_action.py가 읽는
      형태만 강제: dict, `last_timestamp or timestamp` 가 숫자(int/float; bool 포함 — 원본 200),
      event 가 _VP_EVENT_IDS 중 하나(== 비교; 미지 event 는 원본에서 rtdata로 오인→KeyError→500).
    - Organization: truthy 비-dict → 400; dict 이고 org_id 가 truthy 비-str → 400
      (원본 org_prefix의 .strip() AttributeError→500).
    - Usage: dict 이고 Regional_Option 이 truthy 비-str → 400(원본 _normalize_region .strip()→500;
      단 overall null/0 세션은 코칭 분기를 안 타서 원본이 200이었음 — 보고서에 기재).
    - Open_Skill/Custom 의 Passing_Score 등 OverflowError("inf"/1e999)는 건드리지 않음(U7 결정 대기).
    모든 타입 위반 문구는 _MSG_SANITIZED_CLIENT_ERROR("Invalid request data.").
    """
    condition = body.get("condition") or {}
    if not isinstance(condition, dict):
        raise ClientError(_MSG_SANITIZED_CLIENT_ERROR)
    guideline = condition.get("guideline")
    if not (isinstance(guideline, str) and guideline in _SUPPORTED_GUIDELINES):
        # 문구 고정(스펙 §5.7/NEW-3·D-3 확정). isinstance 선검사로 list/dict 값도 같은 400.
        raise ClientError(_MSG_UNSUPPORTED_GUIDELINE)
    target = condition.get("target")
    if not (isinstance(target, str) and target in _SUPPORTED_TARGETS):
        raise ClientError(_MSG_UNSUPPORTED_TARGET)
    training_type = condition.get("training_type")
    if not (isinstance(training_type, str) and training_type in _SUPPORTED_TRAINING_TYPES):
        raise ClientError(_MSG_UNSUPPORTED_TRAINING_TYPE)
    if not body.get("cpr_b64_data"):
        # rawHexBPfile part 누락 또는 빈 바이트(스펙 §5.7)
        raise ClientError(_MSG_CPR_FILE_REQUIRED)

    # --- 검토 반영 2026-09-05: O8 타입 검증(여기부터) ---
    vp_event_list = body.get("vp_event_list")
    if vp_event_list:  # falsy({}, "", 0, null, false)는 vp_action.py가 `if not` 으로 무시 → 원본 200 유지
        if not isinstance(vp_event_list, list):
            raise ClientError(_MSG_SANITIZED_CLIENT_ERROR)
        for item in vp_event_list:
            if not isinstance(item, dict):
                raise ClientError(_MSG_SANITIZED_CLIENT_ERROR)
            # 정렬 키 식은 vp_action.py:32 와 동일하게 계산한다(last_timestamp 우선, 0/None 이면 timestamp).
            sort_key = item.get("last_timestamp") or item.get("timestamp")
            if not isinstance(sort_key, (int, float)):
                raise ClientError(_MSG_SANITIZED_CLIENT_ERROR)
            if item.get("event") not in _VP_EVENT_IDS:
                raise ClientError(_MSG_SANITIZED_CLIENT_ERROR)

    organization = body.get("Organization")
    if organization:  # falsy([], "", 0, false)는 main.py가 `or {}` 로 흡수 → 원본 200 유지
        if not isinstance(organization, dict):
            raise ClientError(_MSG_SANITIZED_CLIENT_ERROR)
        org_id = organization.get("org_id")
        if org_id and not isinstance(org_id, str):  # falsy(0, [], {}, false)는 org_prefix가 "" 로 흡수
            raise ClientError(_MSG_SANITIZED_CLIENT_ERROR)

    usage = body.get("Usage")
    if isinstance(usage, dict):  # 비-dict Usage 는 guide_prompts 가 isinstance 로 무시 → 원본 200 유지
        regional_option = usage.get("Regional_Option")
        if regional_option and not isinstance(regional_option, str):
            raise ClientError(_MSG_SANITIZED_CLIENT_ERROR)


def _validate_legacy_document(body: dict) -> None:
    """Apply the existing sanitized-input boundary to newly restored document use.

    Validate the same document selected by the reference. Preserve accepted
    dict-convertible sections and falsy fallbacks; do not validate unused fields.
    """
    raw_document = body.get("hstm_document")
    try:
        document = dict(raw_document) if raw_document else {
            key: body[key] for key in _HSTM_TOP_LEVEL_KEYS if body.get(key) is not None
        }
        for key in ("ResultSummary", "ResultByCycle", "ResultByCriteria"):
            dict(document.get(key) or {})
    except (TypeError, ValueError):
        raise ClientError(_MSG_SANITIZED_CLIENT_ERROR) from None
    for key in ("Organization", "Dummy", "Open_Skill"):
        value = document.get(key)
        if value and not isinstance(value, dict):
            raise ClientError(_MSG_SANITIZED_CLIENT_ERROR)
    # Reference only dereferences Usage for a missing learner ID; otherwise its
    # later completion function accepts non-dict Usage and replaces it with {}.
    summary = dict(document.get("ResultSummary") or {})
    usage = document.get("Usage")
    needs_usage_id = not summary.get("HstreamId") and not summary.get("hstreamId")
    if needs_usage_id and usage and not isinstance(usage, dict):
        raise ClientError(_MSG_SANITIZED_CLIENT_ERROR)


def _parse_multipart_body(request_body: str, headers: dict, is_base64_encoded: bool) -> dict:
    content_type = _get_content_type(headers)
    if not content_type or "multipart/form-data" not in content_type.lower():
        raise ClientError("Content-Type must be multipart/form-data")

    if not request_body:
        raise ClientError("Empty body")

    body_bytes = base64.b64decode(request_body) if is_base64_encoded else request_body.encode("utf-8")
    pseudo_headers = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8")
    msg = BytesParser().parsebytes(pseudo_headers + body_bytes)

    result: dict = {
        "cpr_b64_data": b"",
        "aed_b64_data": b"",
        "condition": dict(DEFAULT_CONDITION),
        "vp_event_list": [],
        "hstm_document": None,
        "DeviceInfo": None,
        "Organization": None,
        "Dummy": None,
        "Open_Skill": None,
        "ResultSummary": None,
        "Custom": None,
        "ResultByCycle": None,
        "CalculationService": None,
        "Certification": None,
        "Usage": None,
        "ResultByCriteria": None,
        "Institution": None,
        "access_token": None,
        "refresh_token": None,
        "client_id": None,
        "client_secret": None,
        "token_expired": None,
        "access_token_url": None,
        "send_result_url": None,
        "source_endpoint": None,
    }

    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_maintype() == "multipart":
                continue

            name = part.get_param("name", header="Content-Disposition")
            if not name:
                continue

            payload = part.get_payload(decode=True) or b""

            if name == "rawHexBPfile":
                result["cpr_b64_data"] = payload
            elif name == "aedHexBPfile":
                result["aed_b64_data"] = payload
            elif name == "condition":
                try:
                    condition = json.loads(payload.decode(part.get_content_charset("utf-8")))
                    if isinstance(condition, dict):
                        merged = dict(DEFAULT_CONDITION)
                        merged.update(condition)
                        result["condition"] = merged
                except Exception:
                    result["condition"] = dict(DEFAULT_CONDITION)
            elif name == "vp_event_list":
                try:
                    result["vp_event_list"] = json.loads(payload.decode(part.get_content_charset("utf-8")))
                except Exception:
                    result["vp_event_list"] = []
            elif name in _HSTM_TOP_LEVEL_KEYS:
                try:
                    result[name] = json.loads(payload.decode(part.get_content_charset("utf-8")))
                except Exception:
                    result[name] = None
            elif name == "hstm_document":
                try:
                    result["hstm_document"] = json.loads(payload.decode(part.get_content_charset("utf-8")))
                except Exception:
                    result["hstm_document"] = None
            elif name == "hstm_document_b64":
                try:
                    decoded = base64.urlsafe_b64decode(payload).decode("utf-8")
                    result["hstm_document"] = json.loads(decoded)
                except Exception:
                    result["hstm_document"] = None
            elif name in ("access_token", "hstm_access_token"):
                result["access_token"] = payload.decode(part.get_content_charset("utf-8"))
            elif name in ("refresh_token", "hstm_refresh_token"):
                result["refresh_token"] = payload.decode(part.get_content_charset("utf-8"))
            elif name in ("client_id", "hstm_client_id"):
                result["client_id"] = payload.decode(part.get_content_charset("utf-8"))
            elif name in ("client_secret", "hstm_client_secret"):
                result["client_secret"] = payload.decode(part.get_content_charset("utf-8"))
            elif name in ("token_expired", "hstm_token_expired"):
                result["token_expired"] = payload.decode(part.get_content_charset("utf-8")).lower() in (
                    "1",
                    "true",
                    "yes",
                    "y",
                )
            elif name in ("access_token_url", "hstm_access_token_url"):
                result["access_token_url"] = payload.decode(part.get_content_charset("utf-8"))
            elif name in ("send_result_url", "hstm_send_result_url"):
                result["send_result_url"] = payload.decode(part.get_content_charset("utf-8"))
            elif name in ("source_endpoint", "hstm_source_endpoint"):
                result["source_endpoint"] = payload.decode(part.get_content_charset("utf-8"))

    return result


def _log(level: str, message: str, **fields: object) -> None:
    # 원본 hstm_v2 lambda_handler.py:310-316 — JSON 한 줄 print(CloudWatch용) 패턴 현행 유지
    write_diagnostic(level, message, fields)

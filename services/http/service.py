"""참고 HSTM V2의 base64 폼 파서. 파싱만 수행하며 외부 호출·로깅은 없다."""

import base64
import json
from urllib import parse

from services.http.schemas import DEFAULT_CONDITION, RequestBody, ActionRequestBody


def parse_body(request_body: str) -> RequestBody:
    body = _parse(request_body)

    req_body = {}
    if "cpr_b64_data" in body:
        req_body["cpr_b64_data"] = base64.urlsafe_b64decode(body["cpr_b64_data"][0])
    else:
        req_body["cpr_b64_data"] = b""

    if "aed_b64_data" in body:
        req_body["aed_b64_data"] = base64.urlsafe_b64decode(body["aed_b64_data"][0])
    else:
        req_body["aed_b64_data"] = b""

    try:
        condition = body.get("condition")
        req_body["condition"] = json.loads(condition[0]) if condition else DEFAULT_CONDITION
    except Exception as e:
        req_body["condition"] = DEFAULT_CONDITION

    try:
        vp_event_list = body.get("vp_event_list")
        req_body["vp_event_list"] = json.loads(vp_event_list[0]) if vp_event_list else []
    except Exception as e:
        req_body["vp_event_list"] = []

    req_body["hstm_document"] = _get_json_body_value(body, "hstm_document")
    if not req_body["hstm_document"]:
        req_body["hstm_document"] = _get_json_body_value(body, "hstm_document_b64", is_base64=True)
    for key in _HSTM_TOP_LEVEL_KEYS:
        req_body[key] = _get_json_body_value(body, key)

    req_body["access_token"] = _get_body_value(body, "access_token") or _get_body_value(body, "hstm_access_token")
    req_body["refresh_token"] = _get_body_value(body, "refresh_token") or _get_body_value(body, "hstm_refresh_token")
    req_body["client_id"] = _get_body_value(body, "client_id") or _get_body_value(body, "hstm_client_id")
    req_body["client_secret"] = _get_body_value(body, "client_secret") or _get_body_value(body, "hstm_client_secret")
    req_body["access_token_url"] = _get_body_value(body, "access_token_url") or _get_body_value(
        body, "hstm_access_token_url"
    )
    req_body["send_result_url"] = _get_body_value(body, "send_result_url") or _get_body_value(
        body, "hstm_send_result_url"
    )
    req_body["source_endpoint"] = _get_body_value(body, "source_endpoint") or _get_body_value(
        body, "hstm_source_endpoint"
    )
    token_expired = _get_bool_body_value(body, "token_expired")
    if token_expired is None:
        token_expired = _get_bool_body_value(body, "hstm_token_expired")
    req_body["token_expired"] = token_expired

    return req_body


def parse_body_as_action(request_body: str) -> ActionRequestBody:
    body = json.loads(request_body)
    req_body = {
        "action_list": body.get("action_list", []),
        "aed_part_list": body.get("aed_part_list", []),
        "vp_action_list": body.get("vp_action_list", []),
    }

    return req_body


def _parse(reqeust_body: str) -> dict:
    b64body = base64.urlsafe_b64decode(reqeust_body)
    decoded_body = parse.unquote(b64body)
    return parse.parse_qs(decoded_body)


def _get_body_value(body: dict, key: str) -> str | None:
    value = body.get(key)
    return value[0] if value else None


def _get_bool_body_value(body: dict, key: str) -> bool | None:
    value = _get_body_value(body, key)
    if value is None:
        return None
    return value.lower() in ("1", "true", "yes", "y")


def _get_json_body_value(body: dict, key: str, is_base64: bool = False) -> dict | None:
    raw = _get_body_value(body, key)
    if not raw:
        return None
    try:
        if is_base64:
            raw = base64.urlsafe_b64decode(raw).decode("utf-8")
        return json.loads(raw)
    except Exception:
        return None


_HSTM_TOP_LEVEL_KEYS = (
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
)

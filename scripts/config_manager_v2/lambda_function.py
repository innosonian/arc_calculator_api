"""ConfigManager v2 — Secrets Manager 기반 설정 반환 Lambda.

API Gateway AWS_PROXY 통합으로 호출된다.
path parameter `clientid`와 query parameter `type`에 따라
Secrets Manager에서 해당 환경의 설정 JSON을 읽어 반환한다.

Secret 이름 규칙: {SECRET_PREFIX}/{type}
예: config-manager/arc/dev, config-manager/arc/prod
"""
# 원본: hstm_v2 scripts/config_manager_v2/lambda_function.py (arc 이식 — 동작 동일,
# SECRET_PREFIX 기본값과 clientid 값만 arc化. 스펙 50-PORTING-SPEC §3 Secrets 네이밍).

import json
import os

import boto3

SECRET_PREFIX = os.environ.get("SECRET_PREFIX", "config-manager/arc")
REGION = os.environ.get("AWS_REGION", "us-east-2")
VALID_TYPES = {"dev", "testflight-prod", "prod"}

_sm_client = None
_cache: dict[str, str] = {}


def handler(event, context):
    path_params = event.get("pathParameters") or {}
    query_params = event.get("queryStringParameters") or {}

    clientid = path_params.get("clientid", "")
    config_type = query_params.get("type", "")

    if clientid != "arc":
        return _response(200, {"message": "Invalid clientId"})

    if config_type not in VALID_TYPES:
        return _response(200, {"message": "Invalid type parameter"})

    config = _get_config(config_type)
    if config is None:
        _log("error", "secret_not_found", config_type=config_type)
        return _response(500, {"message": "Configuration not available"})

    return _response(200, config)


def _get_config(config_type: str) -> dict | None:
    if config_type in _cache:
        try:
            return json.loads(_cache[config_type])
        except (json.JSONDecodeError, TypeError):
            pass

    secret_name = f"{SECRET_PREFIX}/{config_type}"
    try:
        client = _get_sm_client()
        response = client.get_secret_value(SecretId=secret_name)
        secret_string = response.get("SecretString", "")
    except Exception:
        # Client creation/credential providers and transport failures can raise
        # beyond ClientError. Preserve the fixed unavailable response without
        # logging SDK text, configured names, endpoints or exception objects.
        _log("error", "sm_get_failed", config_type=config_type)
        return None

    _cache[config_type] = secret_string

    try:
        return json.loads(secret_string)
    except (json.JSONDecodeError, TypeError):
        _log("error", "sm_parse_failed", config_type=config_type)
        return None


def _get_sm_client():
    global _sm_client
    if _sm_client is None:
        _sm_client = boto3.client("secretsmanager", region_name=REGION)
    return _sm_client


def _response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
        },
        "body": json.dumps(body, ensure_ascii=False),
    }


def _log(level: str, message: str, **fields) -> None:
    try:
        if level != "error" or message not in {"secret_not_found", "sm_get_failed", "sm_parse_failed"}:
            return
        payload = {"level": level, "message": message}
        config_type = fields.get("config_type")
        if type(config_type) is str and config_type in VALID_TYPES:
            payload["config_type"] = config_type
        print(json.dumps(payload))
    except Exception:
        # Logging is optional; cache/lookup and response contracts are not.
        pass

"""ConfigManager API Gateway HMAC Authorizer.

요청 헤더의 X-Timestamp, X-Signature를 검증하여
API Gateway에 Allow/Deny IAM Policy를 반환한다.

Canonical String 형식:
    {METHOD}\n{PATH}\n{QUERY_STRING}\n{TIMESTAMP}

서명 알고리즘: HMAC-SHA256 (hex digest, lowercase)
"""
# 원본: hstm_v2 scripts/hmac_authorizer/lambda_function.py (arc 이식 — 동작 동일, 무수정).

import hashlib
import hmac
import json
import os
import time

MAX_DRIFT_SECONDS = 300  # ±5분


def handler(event, context):
    headers = _normalize_headers(event.get("headers") or {})
    timestamp = headers.get("x-timestamp", "")
    signature = headers.get("x-signature", "")
    method = event.get("httpMethod", "GET")
    path = event.get("path", "")
    query = event.get("queryStringParameters") or {}
    method_arn = event.get("methodArn", "")

    if not timestamp or not signature:
        _log("warn", "missing_headers", has_timestamp=bool(timestamp), has_signature=bool(signature))
        raise Exception("Unauthorized")

    if not _is_timestamp_valid(timestamp):
        _log("warn", "timestamp_expired", timestamp=timestamp)
        raise Exception("Unauthorized")

    query_string = _build_sorted_query_string(query)
    message = f"{method}\n{path}\n{query_string}\n{timestamp}"

    secret = _get_secret()
    expected = hmac.new(
        secret.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected, signature.lower()):
        _log("warn", "signature_mismatch")
        raise Exception("Unauthorized")

    _log("info", "auth_success", path=path)
    return _build_allow_policy(method_arn)


def _normalize_headers(headers: dict) -> dict:
    return {k.lower(): v for k, v in headers.items()}


def _is_timestamp_valid(timestamp: str) -> bool:
    try:
        ts = int(timestamp)
    except (ValueError, TypeError):
        return False
    return abs(time.time() - ts) <= MAX_DRIFT_SECONDS


def _build_sorted_query_string(params: dict) -> str:
    if not params:
        return ""
    return "&".join(f"{k}={v}" for k, v in sorted(params.items()))


def _get_secret() -> str:
    secret = os.environ.get("HMAC_SECRET", "")
    if not secret:
        raise Exception("Unauthorized")
    return secret


def _build_allow_policy(method_arn: str) -> dict:
    return {
        "principalId": "app",
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Action": "execute-api:Invoke",
                    "Effect": "Allow",
                    "Resource": method_arn,
                }
            ],
        },
    }


def _log(level: str, message: str, **fields) -> None:
    payload = {"level": level, "message": message, **fields}
    print(json.dumps(payload, default=str))

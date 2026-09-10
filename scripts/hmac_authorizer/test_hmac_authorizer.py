"""HMAC Authorizer 로컬 테스트."""
# 원본: hstm_v2 scripts/hmac_authorizer/test_hmac_authorizer.py (arc 이식 — 검증 항목 동일,
# 테스트 경로/methodArn만 arc化: /config/arc, API id는 예시값).

import hashlib
import hmac
import os
import time

os.environ["HMAC_SECRET"] = "test-secret-key-for-local"

# [통합 수정] 저장소 루트 pytest 수집 시 scripts/config_manager_v2/lambda_function.py와
# 모듈명 'lambda_function'이 충돌(sys.modules 캐시)하므로 고유 이름으로 로드한다.
# 원본 저장소는 이 디렉터리를 개별 실행했기에 문제가 없었음 — 동작 변경 아님.
import importlib.util as _ilu
import pathlib as _pl
import sys as _sys

_spec = _ilu.spec_from_file_location(
    "hmac_authorizer_lambda_function", _pl.Path(__file__).parent / "lambda_function.py"
)
_mod = _ilu.module_from_spec(_spec)
_sys.modules["hmac_authorizer_lambda_function"] = _mod
_spec.loader.exec_module(_mod)
handler = _mod.handler


def _make_signature(method, path, query_string, timestamp, secret):
    message = f"{method}\n{path}\n{query_string}\n{timestamp}"
    return hmac.new(
        secret.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _make_event(timestamp=None, signature=None, path="/config/arc", query=None):
    if query is None:
        query = {"type": "prod"}
    headers = {}
    if timestamp is not None:
        headers["X-Timestamp"] = str(timestamp)
    if signature is not None:
        headers["X-Signature"] = signature
    return {
        "httpMethod": "GET",
        "path": path,
        "queryStringParameters": query,
        "headers": headers,
        "methodArn": "arn:aws:execute-api:us-east-2:150612770165:exampleapi/Prod/GET/config/arc",
    }


def test_valid_signature():
    secret = "test-secret-key-for-local"
    ts = str(int(time.time()))
    sig = _make_signature("GET", "/config/arc", "type=prod", ts, secret)
    event = _make_event(timestamp=ts, signature=sig)

    result = handler(event, None)
    assert result["policyDocument"]["Statement"][0]["Effect"] == "Allow"
    print("PASS: valid signature")


def test_invalid_signature():
    ts = str(int(time.time()))
    event = _make_event(timestamp=ts, signature="invalid-signature")

    try:
        handler(event, None)
        assert False, "should have raised"
    except Exception as e:
        assert str(e) == "Unauthorized"
    print("PASS: invalid signature rejected")


def test_missing_headers():
    event = _make_event()

    try:
        handler(event, None)
        assert False, "should have raised"
    except Exception as e:
        assert str(e) == "Unauthorized"
    print("PASS: missing headers rejected")


def test_expired_timestamp():
    secret = "test-secret-key-for-local"
    ts = str(int(time.time()) - 600)  # 10분 전
    sig = _make_signature("GET", "/config/arc", "type=prod", ts, secret)
    event = _make_event(timestamp=ts, signature=sig)

    try:
        handler(event, None)
        assert False, "should have raised"
    except Exception as e:
        assert str(e) == "Unauthorized"
    print("PASS: expired timestamp rejected")


def test_query_string_sorting():
    secret = "test-secret-key-for-local"
    ts = str(int(time.time()))
    # 복수 파라미터: key 기준 알파벳 정렬 → "foo=bar&type=dev"
    sig = _make_signature("GET", "/config/arc", "foo=bar&type=dev", ts, secret)
    event = _make_event(
        timestamp=ts,
        signature=sig,
        query={"type": "dev", "foo": "bar"},
    )

    result = handler(event, None)
    assert result["policyDocument"]["Statement"][0]["Effect"] == "Allow"
    print("PASS: query string sorting")


if __name__ == "__main__":
    test_valid_signature()
    test_invalid_signature()
    test_missing_headers()
    test_expired_timestamp()
    test_query_string_sorting()
    print("\nAll tests passed.")

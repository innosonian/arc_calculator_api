"""Config Manager v2 Lambda 로컬 테스트.

Secrets Manager 호출을 mock하여 테스트한다.
"""
# 원본: hstm_v2 scripts/config_manager_v2/test_config_manager_v2.py (arc 이식 —
# 검증 항목 동일, secret 이름/clientid/mock 값만 arc化).

import json
import os
from unittest.mock import MagicMock, patch

os.environ["SECRET_PREFIX"] = "config-manager/arc"

# [통합 수정] 저장소 루트 pytest 수집 시 scripts/hmac_authorizer/lambda_function.py와
# 모듈명 'lambda_function'이 충돌(sys.modules 캐시)하므로 고유 이름으로 로드한다.
# 원본 저장소는 이 디렉터리를 개별 실행했기에 문제가 없었음 — 동작 변경 아님.
import importlib.util as _ilu
import pathlib as _pl
import sys as _sys

_spec = _ilu.spec_from_file_location(
    "config_manager_v2_lambda_function", _pl.Path(__file__).parent / "lambda_function.py"
)
_mod = _ilu.module_from_spec(_spec)
_sys.modules["config_manager_v2_lambda_function"] = _mod
_spec.loader.exec_module(_mod)
handler, _cache = _mod.handler, _mod._cache


MOCK_DEV_CONFIG = {
    "success": True,
    "Watermark": "Develop",
    "ClientId": "test-client-id",
    "ClientSecret": "test-client-secret",
    "CalcURL": ["https://arcdev.braydenlab.com/"],
}

MOCK_PROD_CONFIG = {
    "success": True,
    "Watermark": "",
    "ClientId": "prod-client-id",
    "ClientSecret": "prod-client-secret",
    "CalcURL": ["https://arcprod.braydenlab.com/"],
}


def _make_event(clientid="arc", config_type="prod"):
    return {
        "pathParameters": {"clientid": clientid},
        "queryStringParameters": {"type": config_type},
    }


def _mock_get_secret_value(SecretId):
    configs = {
        "config-manager/arc/dev": MOCK_DEV_CONFIG,
        "config-manager/arc/prod": MOCK_PROD_CONFIG,
    }
    if SecretId in configs:
        return {"SecretString": json.dumps(configs[SecretId])}
    from botocore.exceptions import ClientError
    raise ClientError({"Error": {"Code": "ResourceNotFoundException"}}, "GetSecretValue")


@patch("config_manager_v2_lambda_function._get_sm_client")
def test_valid_prod(mock_client):
    _cache.clear()
    mock_client.return_value.get_secret_value = _mock_get_secret_value

    result = handler(_make_event("arc", "prod"), None)
    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    assert body["ClientId"] == "prod-client-id"
    assert body["Watermark"] == ""
    print("PASS: valid prod")


@patch("config_manager_v2_lambda_function._get_sm_client")
def test_valid_dev(mock_client):
    _cache.clear()
    mock_client.return_value.get_secret_value = _mock_get_secret_value

    result = handler(_make_event("arc", "dev"), None)
    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    assert body["ClientId"] == "test-client-id"
    assert body["Watermark"] == "Develop"
    print("PASS: valid dev")


def test_invalid_clientid():
    _cache.clear()
    result = handler(_make_event("unknown", "prod"), None)
    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    assert body["message"] == "Invalid clientId"
    print("PASS: invalid clientid")


def test_invalid_type():
    _cache.clear()
    result = handler(_make_event("arc", "staging"), None)
    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    assert body["message"] == "Invalid type parameter"
    print("PASS: invalid type")


def test_empty_type():
    _cache.clear()
    result = handler(_make_event("arc", ""), None)
    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    assert body["message"] == "Invalid type parameter"
    print("PASS: empty type")


@patch("config_manager_v2_lambda_function._get_sm_client")
def test_secret_not_found(mock_client):
    _cache.clear()
    mock_client.return_value.get_secret_value = _mock_get_secret_value

    result = handler(_make_event("arc", "testflight-prod"), None)
    assert result["statusCode"] == 500
    body = json.loads(result["body"])
    assert body["message"] == "Configuration not available"
    print("PASS: secret not found")


@patch("config_manager_v2_lambda_function._get_sm_client")
def test_cache_works(mock_client):
    _cache.clear()
    call_count = 0
    original = _mock_get_secret_value

    def counting_mock(SecretId):
        nonlocal call_count
        call_count += 1
        return original(SecretId)

    mock_client.return_value.get_secret_value = counting_mock

    handler(_make_event("arc", "prod"), None)
    handler(_make_event("arc", "prod"), None)
    assert call_count == 1, f"Expected 1 SM call, got {call_count}"
    print("PASS: cache works (1 SM call for 2 requests)")


@patch("config_manager_v2_lambda_function._get_sm_client")
def test_no_store_header(mock_client):
    _cache.clear()
    mock_client.return_value.get_secret_value = _mock_get_secret_value

    result = handler(_make_event("arc", "prod"), None)
    assert result["headers"]["Cache-Control"] == "no-store"
    print("PASS: no-store header")


if __name__ == "__main__":
    test_valid_prod()
    test_valid_dev()
    test_invalid_clientid()
    test_invalid_type()
    test_empty_type()
    test_secret_not_found()
    test_cache_works()
    test_no_store_header()
    print("\nAll tests passed.")

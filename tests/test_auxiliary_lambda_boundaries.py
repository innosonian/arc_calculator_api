"""Synthetic secrets/SDK failures; no AWS client or real secret is used."""

import hashlib
import hmac
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError, ReadTimeoutError


ROOT = Path(__file__).resolve().parents[1]
MARKER = "DO_NOT_LOG_AUX_SENTINEL"


def load(name, directory):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / directory / "lambda_function.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def authorizer(monkeypatch):
    module = load("aux_boundary_hmac", "hmac_authorizer")
    monkeypatch.setenv("HMAC_SECRET", "synthetic-boundary-test-key")
    monkeypatch.setattr(module.time, "time", lambda: 1000)
    return module


def signed_event(timestamp="1000", path="/config/arc"):
    canonical = f"GET\n{path}\na=1&z=2\n{timestamp}"
    signature = hmac.new(b"synthetic-boundary-test-key", canonical.encode(), hashlib.sha256).hexdigest()
    return {"httpMethod": "GET", "path": path, "queryStringParameters": {"z": "2", "a": "1"},
            "headers": {"X-Timestamp": timestamp, "X-Signature": signature.upper()},
            "methodArn": "arn:aws:execute-api:us-east-1:000000000000:test/dev/GET/config/arc"}


@pytest.mark.parametrize("timestamp,allowed", [("700", True), ("1300", True), ("699", False),
                                               ("1301", False), (MARKER, False)])
def test_hmac_timestamp_boundary_and_no_input_log(authorizer, timestamp, allowed, capsys):
    event = signed_event(timestamp)
    if allowed:
        assert authorizer.handler(event, None)["policyDocument"]["Statement"][0]["Effect"] == "Allow"
    else:
        with pytest.raises(Exception, match="^Unauthorized$"):
            authorizer.handler(event, None)
    captured = capsys.readouterr()
    assert MARKER not in captured.out + captured.err


def test_authenticated_path_is_not_a_log_field(authorizer, capsys):
    event = signed_event(path="/" + MARKER)
    result = authorizer.handler(event, None)
    assert result["policyDocument"]["Statement"][0]["Resource"] == event["methodArn"]
    captured = capsys.readouterr()
    assert MARKER not in captured.out + captured.err + json.dumps(result)


@pytest.mark.parametrize("valid", [True, False])
def test_hmac_logging_failure_does_not_change_auth(authorizer, monkeypatch, valid):
    def broken(*args, **kwargs):
        raise OSError(MARKER)
    monkeypatch.setattr(authorizer, "print", broken, raising=False)
    event = signed_event()
    if valid:
        assert authorizer.handler(event, None)["policyDocument"]["Statement"][0]["Effect"] == "Allow"
    else:
        event["headers"]["X-Signature"] = "invalid"
        with pytest.raises(Exception, match="^Unauthorized$"):
            authorizer.handler(event, None)


@pytest.fixture
def manager():
    return load("aux_boundary_config", "config_manager_v2")


def request():
    return {"pathParameters": {"clientid": "arc"}, "queryStringParameters": {"type": "dev"}}


@pytest.mark.parametrize("failure", [
    lambda: ClientError({"Error": {"Code": "InternalError", "Message": MARKER}}, "GetSecretValue"),
    lambda: EndpointConnectionError(endpoint_url="https://" + MARKER + ".invalid"),
    lambda: ReadTimeoutError(endpoint_url="https://" + MARKER + ".invalid"),
    lambda: RuntimeError(MARKER),
])
@pytest.mark.parametrize("during_creation", [False, True])
def test_config_sdk_boundary_is_sanitized(manager, monkeypatch, capsys, failure, during_creation):
    def fail(*args, **kwargs):
        raise failure()
    factory = fail if during_creation else lambda: SimpleNamespace(get_secret_value=fail)
    monkeypatch.setattr(manager, "_get_sm_client", factory)
    result = manager.handler(request(), None)
    assert result["statusCode"] == 500
    assert json.loads(result["body"]) == {"message": "Configuration not available"}
    captured = capsys.readouterr()
    assert MARKER not in captured.out + captured.err + json.dumps(result)
    assert manager._cache == {}


def test_config_invalid_json_then_recovery_and_cache(manager, monkeypatch, capsys):
    replies = iter([{"SecretString": MARKER}, {"SecretString": '{"success":true}'}])
    calls = []
    def get(**kwargs):
        calls.append(kwargs)
        return next(replies)
    monkeypatch.setattr(manager, "SECRET_PREFIX", MARKER)
    monkeypatch.setattr(manager, "_get_sm_client", lambda: SimpleNamespace(get_secret_value=get))
    assert manager.handler(request(), None)["statusCode"] == 500
    assert json.loads(manager.handler(request(), None)["body"]) == {"success": True}
    assert json.loads(manager.handler(request(), None)["body"]) == {"success": True}
    assert len(calls) == 2
    captured = capsys.readouterr()
    assert MARKER not in captured.out + captured.err


def test_config_logging_failure_preserves_controlled_response(manager, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError(MARKER)
    monkeypatch.setattr(manager, "_get_sm_client", fail)
    monkeypatch.setattr(manager, "print", fail, raising=False)
    assert manager.handler(request(), None)["statusCode"] == 500


@pytest.mark.parametrize("directory", ["hmac_authorizer", "config_manager_v2"])
def test_aux_logger_rejects_unknown_fields_and_codes(directory, capsys):
    module = load("aux_boundary_logger", directory)
    module._log(MARKER, MARKER, body=MARKER, error=MARKER)
    captured = capsys.readouterr()
    assert MARKER not in captured.out + captured.err

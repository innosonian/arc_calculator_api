"""Collection guards and pure preflight output checks; no AWS action execution."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from scripts import deployment_preflight as preflight
from tests.test_deployment_preflight import configuration, variables


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts/run_actions_regression.py"
AWS_FORBIDDEN = "ACTIONS_REGRESSION_AWS_FORBIDDEN"
NETWORK_FORBIDDEN = "ACTIONS_REGRESSION_NETWORK_FORBIDDEN"


def run_collection_probe(tmp_path, body, *, extra_env=None):
    probe = tmp_path / "test_collection_probe.py"
    probe.write_text(body + "\n\ndef test_collected():\n    pass\n", encoding="utf-8")
    # Do not inherit the user's credential, config, proxy, or application env.
    env = {"PATH": os.defpath, "STAGE": "test", "PYTHONDONTWRITEBYTECODE": "1"}
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, "-B", str(RUNNER), str(probe)],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30,
    )


@pytest.mark.parametrize("body", [
    "import boto3\nboto3.client('sts')",
    "import boto3\nboto3.resource('s3')",
    "import boto3\nboto3.Session().client('sts')",
    "import boto3\nboto3.Session().resource('s3')",
    "from util.uploader import client\nclient('s3')",
    "import botocore.session\nbotocore.session.get_session().create_client('sts')",
    "from botocore.client import BaseClient\nBaseClient._make_api_call(None, 'GetCallerIdentity', {})",
    "from botocore.httpsession import URLLib3Session\nURLLib3Session.send(None, None)",
    "import boto3\ntry:\n    boto3.client('sts')\nexcept Exception:\n    pass",
])
def test_sdk_is_forbidden_during_collection_before_fixtures(tmp_path, body):
    result = run_collection_probe(tmp_path, body)
    visible = result.stdout + result.stderr
    assert result.returncode != 0
    assert AWS_FORBIDDEN in visible, visible
    assert "1 passed" not in visible


@pytest.mark.parametrize("operation", [
    "socket.socket().connect(('127.0.0.1', 9))",
    "socket.socket().connect_ex(('127.0.0.1', 9))",
    "socket.getaddrinfo('127.0.0.1', 9)",
    "socket.gethostbyname('localhost')",
    "socket.gethostbyaddr('127.0.0.1')",
    "socket.getnameinfo(('127.0.0.1', 9), 0)",
    "socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b'probe', ('127.0.0.1', 9))",
])
def test_python_network_attempts_fail_at_audit_hook_during_collection(tmp_path, operation):
    # A second, independent audit hook stops the operation if the runner's guard
    # regresses. CPython calls the runner's earlier hook first: the exact marker
    # proves which hook stopped it without allowing an actual socket operation.
    body = """import socket
import sys
def fallback(event, args):
    if event.startswith('socket.') and event != 'socket.__new__':
        raise BaseException('PROBE_FALLBACK_BLOCKED_NETWORK')
sys.addaudithook(fallback)
""" + operation
    result = run_collection_probe(tmp_path, body)
    visible = result.stdout + result.stderr
    assert result.returncode != 0
    assert NETWORK_FORBIDDEN in visible, visible
    assert "PROBE_FALLBACK_BLOCKED_NETWORK" not in visible


def test_bootstrap_clears_external_configuration_before_collection(tmp_path):
    body = """import os
assert os.environ['STAGE'] == 'test'
assert os.environ['AWS_CONFIG_FILE'] == os.devnull
assert os.environ['AWS_SHARED_CREDENTIALS_FILE'] == os.devnull
assert os.environ['BOTO_CONFIG'] == os.devnull
assert os.environ['AWS_EC2_METADATA_DISABLED'] == 'true'
assert os.environ['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] == '1'
for name in ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN',
             'AWS_PROFILE', 'ARC_SUBMIT_LAMBDA_NAME', 'HSTM_SUBMIT_LAMBDA_NAME',
             'SENTRY_DSN', 'PYTEST_ADDOPTS', 'PYTEST_PLUGINS'):
    assert name not in os.environ
"""
    result = run_collection_probe(tmp_path, body, extra_env={
        "STAGE": "unused-stage",
        "AWS_ACCESS_KEY_ID": "SYNTHETIC_ENV_MUST_BE_REMOVED",
        "AWS_SECRET_ACCESS_KEY": "SYNTHETIC_ENV_MUST_BE_REMOVED",
        "AWS_SESSION_TOKEN": "SYNTHETIC_ENV_MUST_BE_REMOVED",
        "AWS_PROFILE": "unused-profile",
        "AWS_CONFIG_FILE": str(tmp_path / "must-not-be-read"),
        "AWS_SHARED_CREDENTIALS_FILE": str(tmp_path / "must-not-be-read"),
        "ARC_SUBMIT_LAMBDA_NAME": "unused-function",
        "HSTM_SUBMIT_LAMBDA_NAME": "unused-function",
        "SENTRY_DSN": "SYNTHETIC_ENV_MUST_BE_REMOVED",
        "PYTEST_ADDOPTS": "--definitely-not-an-option",
        "PYTEST_PLUGINS": "must_not_import_this_plugin",
    })
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
    assert "SYNTHETIC_ENV_MUST_BE_REMOVED" not in result.stdout + result.stderr


def test_preflight_writes_exact_github_region_without_private_values(tmp_path, capsys):
    document = configuration("development")
    env = {**variables(), "SYNTHETIC_SECRET": "ACTIONS_REGION_PRIVATE_MARKER"}
    binding, env_file, output = tmp_path / "bindings.json", tmp_path / "env", tmp_path / "github-output"
    binding.write_text(json.dumps(document), encoding="utf-8")
    env_file.write_text("".join(f"{key}={value}\n" for key, value in env.items()), encoding="utf-8")
    output.write_text("previous_output=preserved\n", encoding="utf-8")
    assert preflight.main([
        "--environment", "development", "--component", "calculator",
        "--bindings", str(binding), "--env-file", str(env_file), "--github-output", str(output),
    ]) == 0
    visible = capsys.readouterr()
    expected_region = document["environments"]["development"]["region"]
    assert output.read_text() == f"previous_output=preserved\naws_region={expected_region}\n"
    assert env["SYNTHETIC_SECRET"] not in visible.out + visible.err + output.read_text()
    result = json.loads(visible.out)
    assert result["aws_resources_verified"] is False and result["runtime_verified"] is False


def test_failed_preflight_does_not_export_region(tmp_path, capsys):
    binding, env_file, output = tmp_path / "bindings.json", tmp_path / "env", tmp_path / "github-output"
    binding.write_text(json.dumps({"schema_version": 1, "environments": {}}), encoding="utf-8")
    env_file.write_text("".join(f"{key}={value}\n" for key, value in variables().items()), encoding="utf-8")
    output.write_text("previous_output=preserved\n", encoding="utf-8")
    assert preflight.main([
        "--environment", "development", "--component", "calculator",
        "--bindings", str(binding), "--env-file", str(env_file), "--github-output", str(output),
    ]) == 1
    assert output.read_text() == "previous_output=preserved\n"
    visible = capsys.readouterr()
    assert visible.err.strip() == "ENVIRONMENT_NOT_CONFIGURED"
    assert visible.out == ""

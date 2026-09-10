"""Independent offline deployment boundaries; external executables are fakes."""

from copy import deepcopy
import json
from pathlib import Path
import stat
import subprocess
import sys

import pytest

from scripts import deployment_preflight as preflight


ROOT = Path(__file__).resolve().parents[1]
SECRET = "INDEPENDENT_PREFLIGHT_PRIVATE_MARKER"
SHELLS = ("deploy_arc_lambda.sh", "deploy_arc_api_gateway.sh")


def document():
    bucket, prefix = preflight.read_storage_contract()
    return {"schema_version": 1, "environments": {"beta": {
        "account_id": "000000000000", "region": "xy-fixture-1", "runtime_stage": "explicit-stage",
        "calculator": {"function_name": "FixtureCalculator", "role_name": "FixtureRole", "memory_mb": 512,
                       "timeout_seconds": 120, "log_retention_days": 30, "reserved_concurrency": None,
                       "storage_bucket": bucket, "storage_prefix": prefix, "storage_region": "xy-fixture-1"},
        "gateway": {"api_id": "fixtureapi", "stage": "explicit-gateway", "route": "/cpr-analysis",
                    "rate_limit": 3.5, "burst_limit": 4},
    }}}


def variables():
    return {"STAGE": "explicit-stage", "ARC_STORAGE_REGION": "xy-fixture-1", "TEST_SECRET": SECRET}


@pytest.mark.parametrize("separator", ["\r", "\v", "\f", "\x85", "\u2028", "\u2029"])
@pytest.mark.parametrize("placement", ["middle", "end"])
def test_unsupported_line_separator_cannot_split_settings_or_change_a_secret(tmp_path, separator, placement):
    value = SECRET + separator + ("UNAUTHORIZED_SETTING=true" if placement == "middle" else "")
    path = tmp_path / "private-env"
    path.write_bytes(("TEST_SECRET=" + value).encode("utf-8"))
    with pytest.raises(preflight.PreflightError, match="ENV_FILE_INVALID"):
        preflight.parse_env_file(path)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_permitted_line_endings_preserve_every_value_character(tmp_path, newline):
    path = tmp_path / "private-env"
    values = {**variables(), "LITERAL": "$(do-not-run);`do-not-run` # text=literal, spaces"}
    path.write_bytes(newline.join(f"{key}={value}" for key, value in values.items()).encode("utf-8"))
    assert preflight.parse_env_file(path) == values


@pytest.mark.parametrize("name", ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
                                  "_HANDLER", "AWS_LAMBDA_EXEC_WRAPPER", "LAMBDA_TASK_ROOT"])
def test_application_env_cannot_replace_reserved_execution_identity(tmp_path, name):
    path = tmp_path / "env"
    path.write_text(f"{name}={SECRET}\n")
    with pytest.raises(preflight.PreflightError, match="ENV_FILE_INVALID") as caught:
        preflight.parse_env_file(path)
    assert SECRET not in str(caught.value)


def test_binding_storage_disagreement_is_rejected_without_mutating_inputs():
    supplied, env = document(), variables()
    supplied["environments"]["beta"]["calculator"]["storage_bucket"] = "different-fixture-bucket"
    before, env_before = deepcopy(supplied), deepcopy(env)
    with pytest.raises(preflight.PreflightError) as caught:
        preflight.validate_configuration("beta", "calculator", supplied, env,
                                         storage_contract=preflight.read_storage_contract())
    assert supplied == before and env == env_before and SECRET not in str(caught.value)


def test_preflight_exports_private_values_without_claiming_runtime_or_resource_verification(tmp_path, capsys):
    binding, env, output = tmp_path / "binding.json", tmp_path / "env", tmp_path / "private output"
    binding.write_text(json.dumps(document()))
    env.write_text("".join(f"{name}={value}\n" for name, value in variables().items()))
    assert preflight.main(["--environment", "beta", "--component", "calculator", "--bindings", str(binding),
                           "--env-file", str(env), "--output-dir", str(output)]) == 0
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["aws_resources_verified"] is False and result["runtime_verified"] is False
    assert SECRET not in captured.out + captured.err
    assert json.loads((output / "environment.json").read_text())["Variables"] == variables()
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    for path in output.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        if path.name != "environment.json":
            assert SECRET not in path.read_text()


def test_existing_export_directory_is_never_overwritten(tmp_path, capsys):
    binding, env, output = tmp_path / "binding.json", tmp_path / "env", tmp_path / "private"
    binding.write_text(json.dumps(document()))
    env.write_text("".join(f"{name}={value}\n" for name, value in variables().items()))
    output.mkdir()
    sentinel = output / "environment.json"
    sentinel.write_text("existing-private-data")
    assert preflight.main(["--environment", "beta", "--component", "calculator", "--bindings", str(binding),
                           "--env-file", str(env), "--output-dir", str(output)]) == 1
    assert sentinel.read_text() == "existing-private-data"
    assert SECRET not in repr(capsys.readouterr())


def shell_fixture(tmp_path, *, handler="lambda_handler.run", failed_action=None):
    commands = tmp_path / "commands"
    commands.mkdir()
    log = tmp_path / "calls.jsonl"
    code = f'''#!{sys.executable}
import json, os, pathlib, sys
args = sys.argv[1:]
tool = pathlib.Path(sys.argv[0]).name
if tool == 'python3' and args and args[0].endswith('/deployment_preflight.py'):
    os.execv({sys.executable!r}, [{sys.executable!r}, *args])
with open(os.environ['FIXTURE_CALLS'], 'a') as stream:
    stream.write(json.dumps([tool, *args]) + '\\n')
if tool == 'python3' and args[:2] == ['-m', 'pip']:
    sys.exit(0)
if tool == 'python3' and args and args[0].endswith('/build_mock_artifact.py'):
    output = pathlib.Path(args[args.index('--outdir') + 1]); output.mkdir()
    (output / 'mock-lambda.zip').write_bytes(b'non-deployable fixture')
    sys.exit(0)
if tool != 'aws':
    sys.exit(97)
action = args[1] if len(args) > 1 else ''
if action == os.environ.get('FIXTURE_FAILED_ACTION'):
    print({SECRET!r}, file=sys.stderr); print({SECRET!r}); sys.exit(23)
query = args[args.index('--query') + 1] if '--query' in args else ''
role = 'arn:aws:iam::000000000000:role/FixtureRole'
if action == 'get-caller-identity': print('000000000000')
elif action == 'get-role': print(role)
elif action == 'get-function' and query == 'Configuration.Role': print(role)
elif action == 'get-function' and query == 'Configuration.Handler': print(os.environ['FIXTURE_HANDLER'])
elif action == 'get-function-configuration' and query == 'Handler': print(os.environ['FIXTURE_HANDLER'])
elif action in ('get-function', 'get-function-configuration'):
    print(json.dumps({{'Configuration': {{'Role': role, 'Handler': os.environ['FIXTURE_HANDLER']}},
                      'Role': role, 'Handler': os.environ['FIXTURE_HANDLER']}}))
elif action == 'get-resources': print('fixtureresource')
elif action == 'get-rest-api' and query: print('multipart/form-data')
else: print({SECRET!r})
'''
    for name in ("python3", "aws", "pip", "curl", "rsync", "zip"):
        path = commands / name
        path.write_text(code)
        path.chmod(0o700)
    binding, env_file = tmp_path / "binding.json", tmp_path / "private env"
    binding.write_text(json.dumps(document()))
    env_file.write_text("".join(f"{name}={value}\n" for name, value in variables().items()))
    env = {"PATH": str(commands) + ":/usr/bin:/bin", "FIXTURE_CALLS": str(log),
           "FIXTURE_HANDLER": handler, "LANG": "en_US.UTF-8"}
    if failed_action:
        env["FIXTURE_FAILED_ACTION"] = failed_action
    return binding, env_file, env, log


def run_shell(tmp_path, script, **options):
    binding, env_file, env, log = shell_fixture(tmp_path, **options)
    result = subprocess.run(["/bin/bash", str(ROOT / "scripts" / script), "beta", str(env_file), str(binding)],
                            env=env, cwd=tmp_path, capture_output=True, text=True, timeout=10)
    calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return result, calls


def mutations(calls):
    return [call for call in calls if call[0] == "aws" and len(call) > 2
            and call[2].startswith(("update-", "put-", "publish-", "create-", "add-", "delete-"))]


@pytest.mark.parametrize("script", SHELLS)
@pytest.mark.parametrize("handler", ["lambda_handler._run_trusted_calculation", "other_module.run"])
def test_wrong_existing_handler_stops_before_any_aws_mutation(tmp_path, script, handler):
    result, calls = run_shell(tmp_path, script, handler=handler)
    assert result.returncode != 0
    assert mutations(calls) == []
    assert SECRET not in result.stdout + result.stderr


def test_gateway_throttling_is_limited_to_the_explicit_calculation_method(tmp_path):
    result, calls = run_shell(tmp_path, "deploy_arc_api_gateway.sh")
    assert result.returncode == 0, result.stderr
    stage = [call for call in calls if call[:3] == ["aws", "apigateway", "update-stage"]]
    assert len(stage) == 1
    patches = [value for value in stage[0] if value.startswith("op=")]
    assert len(patches) == 2
    assert all("path=/~1cpr-analysis/POST/throttling/" in patch for patch in patches)
    assert all("/*/*" not in patch for patch in patches)
    assert not any(call[:3] == ["aws", "apigateway", "put-method"] for call in calls)
    assert SECRET not in result.stdout + result.stderr


@pytest.mark.parametrize("script,action", [(SHELLS[0], "get-function"), (SHELLS[0], "update-function-code"),
                                          (SHELLS[1], "get-method"), (SHELLS[1], "update-stage")])
def test_aws_diagnostic_secret_never_reaches_shell_output_or_success_message(tmp_path, script, action):
    result, calls = run_shell(tmp_path, script, failed_action=action)
    assert result.returncode != 0
    assert SECRET not in result.stdout + result.stderr
    assert "DEPLOYMENT_FINISHED" not in result.stdout
    assert any(call[:3] == ["aws", "lambda" if "function" in action else "apigateway", action] for call in calls)

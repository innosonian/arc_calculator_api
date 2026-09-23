"""Offline deployment counterexamples; shell tools below are explicit fakes."""

from copy import deepcopy
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import zipfile

import pytest

from scripts import deployment_preflight as preflight
from scripts import build_mock_artifact as builder
from tests.test_mock_artifact import roots, write


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ("deploy_arc_lambda.sh", "deploy_arc_api_gateway.sh")


def fixture_binding():
    # Syntax fixtures only, never configured AWS resources or policy defaults.
    return {"account_id": "000000000000", "region": "xz-region-1", "runtime_stage": "unchanged_runtime_label",
            "calculator": {"function_name": "FixtureCalc", "role_name": "FixtureRole", "memory_mb": 512,
                           "timeout_seconds": 120, "log_retention_days": 30, "reserved_concurrency": None,
                           "storage_bucket": preflight.read_storage_contract()[0], "storage_prefix": preflight.read_storage_contract()[1], "storage_region": "xy-region-1"},
            "gateway": {"api_id": "fixtureapi", "stage": "unchanged_gateway_label", "route": "/cpr-analysis",
                        "rate_limit": None, "burst_limit": None}}


def configuration(environment="beta"):
    return {"schema_version": 1, "environments": {environment: fixture_binding()}}


def variables():
    return {"STAGE": "unchanged_runtime_label", "ARC_STORAGE_REGION": "xy-region-1",
            "ARC_MOCK_ENVIRONMENT": "unchanged_identity_namespace", "ARC_MOCK_TABLE_NAME": "unchanged_table"}


@pytest.mark.parametrize("selector,selected", [("dev", "development"), ("development", "development"),
                                                ("beta", "beta"), ("prod", "production"), ("production", "production")])
def test_selector_alias_does_not_rewrite_runtime_names(selector, selected):
    document, env = configuration(selected), variables()
    original, before_env = deepcopy(document), deepcopy(env)
    result = preflight.validate_configuration(selector, "calculator", document, env, storage_contract=preflight.read_storage_contract())
    assert result["environment"] == selected
    assert result["binding"] == document["environments"][selected]
    assert document == original and env == before_env
    result["binding"]["calculator"]["storage_bucket"] = "different"
    assert document == original


@pytest.mark.parametrize("selector", ["local", "dev", "development", "beta", "prod", "production", "SECRET-MARKER"])
def test_unconfigured_environment_never_falls_back(selector):
    expected = "LOCAL_AWS_DEPLOYMENT_FORBIDDEN" if selector == "local" else (
        "ENVIRONMENT_UNSUPPORTED" if selector == "SECRET-MARKER" else "ENVIRONMENT_NOT_CONFIGURED")
    with pytest.raises(preflight.PreflightError, match=expected):
        preflight.validate_configuration(selector, "calculator", {"schema_version": 1, "environments": {}}, variables(), storage_contract=preflight.read_storage_contract())


@pytest.mark.parametrize("change", ["account", "region", "stage", "storage_region", "wildcard_bucket", "wildcard_prefix",
                                   "negative_memory", "boolean_timeout", "unknown_field", "missing_gateway", "half_throttle",
                                   "nonfinite_throttle", "role_shell", "route", "env_conflict", "schema_bool", "alias_binding"])
def test_bad_or_conflicting_explicit_bindings_fail_without_disclosing_values(change):
    doc, env = configuration(), variables()
    binding = doc["environments"]["beta"]
    if change == "account": binding["account_id"] = "SECRET-MARKER"
    elif change == "region": binding["region"] = "SECRET-MARKER\n"
    elif change == "stage": env["STAGE"] = "production"
    elif change == "storage_region": env["ARC_STORAGE_REGION"] = "different-region-1"
    elif change == "wildcard_bucket": binding["calculator"]["storage_bucket"] = "*"
    elif change == "wildcard_prefix": binding["calculator"]["storage_prefix"] = "fixture/*"
    elif change == "negative_memory": binding["calculator"]["memory_mb"] = -1
    elif change == "boolean_timeout": binding["calculator"]["timeout_seconds"] = True
    elif change == "unknown_field": binding["unused"] = "SECRET-MARKER"
    elif change == "missing_gateway": del binding["gateway"]
    elif change == "half_throttle": binding["gateway"]["rate_limit"] = 2
    elif change == "nonfinite_throttle": binding["gateway"].update(rate_limit=float("inf"), burst_limit=2)
    elif change == "role_shell": binding["calculator"]["role_name"] = "$(SECRET-MARKER)"
    elif change == "route": binding["gateway"]["route"] = "/other/route"
    elif change == "env_conflict": env["ARC_API_STAGE"] = "different"
    elif change == "schema_bool": doc["schema_version"] = True
    elif change == "alias_binding": doc["environments"]["prod"] = binding
    with pytest.raises(preflight.PreflightError) as error:
        preflight.validate_configuration("beta", "gateway", doc, env, storage_contract=preflight.read_storage_contract())
    assert "SECRET-MARKER" not in str(error.value)


@pytest.mark.parametrize("body", ['STAGE=a\nSTAGE=b\n', 'export STAGE=a\n', 'SECRET-MARKER\n',
                                 'AWS_ACCESS_KEY_ID=SECRET-MARKER\n', 'STAGE="SECRET-MARKER"\n'])
def test_dotenv_errors_never_print_fields_or_values(tmp_path, body, capsys):
    path = tmp_path / "env"
    path.write_text(body)
    with pytest.raises(preflight.PreflightError, match="ENV_FILE_INVALID"):
        preflight.parse_env_file(path)
    assert not capsys.readouterr().out


def test_private_snapshot_preserves_literal_secret_without_shell_execution(tmp_path, capsys):
    binding, env_file, output = tmp_path / "bindings.json", tmp_path / "env", tmp_path / "private"
    binding.write_text(json.dumps(configuration()))
    marker = tmp_path / "must-not-exist"
    secret = f'$(touch {marker});`touch {marker}` # literal=value, spaces'
    env = {**variables(), "SYNTHETIC_SECRET": secret}
    env_file.write_text("".join(f"{key}={value}\n" for key, value in env.items()))
    assert preflight.main(["--environment", "beta", "--component", "calculator", "--bindings", str(binding),
                           "--env-file", str(env_file), "--output-dir", str(output)]) == 0
    visible = capsys.readouterr()
    assert secret not in visible.out + visible.err
    assert not marker.exists()
    assert json.loads((output / "environment.json").read_text())["Variables"] == env
    assert secret not in (output / "bindings.tsv").read_text()
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in output.iterdir())
    assert json.loads(visible.out)["aws_resources_verified"] is False


def test_duplicate_binding_fields_and_symlink_inputs_are_rejected(tmp_path):
    source = tmp_path / "bindings.json"
    source.write_text('{"schema_version":1,"schema_version":1,"environments":{}}')
    with pytest.raises(preflight.PreflightError, match="DUPLICATE"):
        preflight.read_bindings(source)
    link = tmp_path / "link"
    link.symlink_to(source)
    with pytest.raises(preflight.PreflightError, match="DEPLOYMENT_BINDINGS_REQUIRED"):
        preflight.read_bindings(link)


def sandbox_commands(tmp_path):
    """Every external command is a fake; Python delegates only pure preflight."""
    commands = tmp_path / "commands"
    commands.mkdir()
    log = tmp_path / "calls.jsonl"
    program = f'''#!{sys.executable}
import json, os, pathlib, sys
args = sys.argv[1:]
tool = pathlib.Path(sys.argv[0]).name
if tool == 'python3' and args and args[0].endswith('/deployment_preflight.py'):
    os.execv({sys.executable!r}, [{sys.executable!r}, *args])
with open(os.environ['FAKE_CALL_LOG'], 'a') as stream:
    stream.write(json.dumps([tool, *args]) + '\\n')
if tool == 'python3' and args[:2] == ['-m', 'pip']:
    sys.exit(0)
if tool == 'python3' and args and args[0].endswith('/build_mock_artifact.py'):
    output = pathlib.Path(args[args.index('--outdir') + 1]); output.mkdir()
    (output / 'mock-lambda.zip').write_bytes(b'explicit shell wiring fixture')
    sys.exit(0)
if tool != 'aws': sys.exit(99)
action = args[1] if len(args) > 1 else ''
print('SECRET-MARKER', file=sys.stderr)
if action == os.environ.get('FAIL_AWS_ACTION'):
    print('SECRET-MARKER'); sys.exit(1)
query = args[args.index('--query') + 1] if '--query' in args else ''
if query and query == os.environ.get('FAIL_AWS_QUERY'):
    print('SECRET-MARKER'); sys.exit(1)
if args[:2] == ['sts', 'get-caller-identity']: print(os.environ.get('FAKE_ACCOUNT', '000000000000'))
elif action == 'get-function' and '--query' in args and args[args.index('--query') + 1] == 'Configuration.Handler':
    print(os.environ.get('FAKE_HANDLER', 'lambda_handler.run'))
elif action == 'get-function' and query == 'Configuration.Runtime': print(os.environ.get('FAKE_RUNTIME', 'python3.12'))
elif action == 'get-function' and query == 'Configuration.PackageType': print(os.environ.get('FAKE_PACKAGE_TYPE', 'Zip'))
elif action == 'get-function' and query == 'Configuration.LoggingConfig.LogGroup':
    print(os.environ.get('FAKE_LOG_GROUP_JSON', 'null'))
elif args[:2] == ['iam', 'get-role'] or (action == 'get-function' and '--query' in args):
    print(os.environ.get('FAKE_ROLE', 'arn:aws:iam::000000000000:role/FixtureRole'))
elif action == 'get-resources': print('fixtureresource')
elif action == 'get-rest-api' and '--query' in args: print('multipart/form-data')
else: print('{{"Environment":{{"Variables":{{"SECRET":"SECRET-MARKER"}}}}}}')
'''
    for name in ("python3", "aws", "rsync", "pip", "zip", "curl"):
        path = commands / name
        path.write_text(program)
        path.chmod(0o700)
    env = {"PATH": str(commands) + ":/usr/bin:/bin", "FAKE_CALL_LOG": str(log), "LANG": "en_US.UTF-8"}
    return env, log


def run_shell(tmp_path, script, selector, *, configured=False, changes=None, calculator_changes=None):
    env, log = sandbox_commands(tmp_path)
    env.update(changes or {})
    env_file = tmp_path / "env"
    env_file.write_text("".join(f"{key}={value}\n" for key, value in variables().items()) + "SYNTHETIC_SECRET=SECRET-MARKER\n")
    binding = tmp_path / "bindings.json"
    document = configuration(preflight.normalize_selector(selector)) if configured else {"schema_version": 1, "environments": {}}
    if calculator_changes:
        document["environments"][preflight.normalize_selector(selector)]["calculator"].update(calculator_changes)
    binding.write_text(json.dumps(document))
    result = subprocess.run(["/bin/bash", str(ROOT / "scripts" / script), selector, str(env_file), str(binding)],
                            env=env, cwd=tmp_path, capture_output=True, text=True, timeout=10)
    calls = [json.loads(row) for row in log.read_text().splitlines()] if log.exists() else []
    return result, calls


@pytest.mark.parametrize("script", SCRIPTS)
@pytest.mark.parametrize("selector", ["local", "dev", "development", "beta", "prod", "production"])
def test_real_deploy_entrypoints_fail_before_any_external_command_for_missing_binding(tmp_path, script, selector):
    result, calls = run_shell(tmp_path, script, selector)
    assert result.returncode != 0
    assert calls == []
    assert "SECRET-MARKER" not in result.stdout + result.stderr


@pytest.mark.parametrize("script", SCRIPTS)
def test_explicit_beta_binding_is_used_without_prod_fallback_or_secret_output(tmp_path, script):
    result, calls = run_shell(tmp_path, script, "beta", configured=True)
    assert result.returncode == 0, result.stderr
    assert "SECRET-MARKER" not in result.stdout + result.stderr
    assert calls[0][:3] == ["aws", "sts", "get-caller-identity"]
    encoded = json.dumps(calls)
    assert "arc_calc_prod" not in encoded and "arc_calc_dev" not in encoded
    assert "FixtureCalc" in encoded
    assert not any(call[0] in ("rsync", "zip", "curl", "pip") for call in calls)
    assert not any(len(call) > 2 and call[2] in ("create-role", "create-function", "put-role-policy", "put-method", "add-permission") for call in calls)
    if script == "deploy_arc_lambda.sh":
        assert any(call[0] == "python3" and call[1].endswith("/build_mock_artifact.py") for call in calls)
        assert next(i for i, c in enumerate(calls) if c[0] == "python3" and c[1].endswith("/build_mock_artifact.py")) < next(
            i for i, c in enumerate(calls) if c[:3] == ["aws", "lambda", "update-function-code"])


@pytest.mark.parametrize("script", SCRIPTS)
def test_account_mismatch_stops_before_install_or_mutation(tmp_path, script):
    result, calls = run_shell(tmp_path, script, "beta", configured=True, changes={"FAKE_ACCOUNT": "111111111111"})
    assert result.returncode != 0 and "AWS_ACCOUNT_MISMATCH" in result.stderr
    assert len(calls) == 1
    assert "SECRET-MARKER" not in result.stdout + result.stderr


@pytest.mark.parametrize("script,action", [(SCRIPTS[0], "update-function-configuration"), (SCRIPTS[1], "put-integration")])
def test_aws_failure_output_is_not_echoed(tmp_path, script, action):
    result, calls = run_shell(tmp_path, script, "beta", configured=True, changes={"FAIL_AWS_ACTION": action})
    assert result.returncode != 0
    assert "SECRET-MARKER" not in result.stdout + result.stderr
    assert not any(call[:3] == ["aws", "lambda", "publish-version"] for call in calls)


@pytest.mark.parametrize("script", SCRIPTS)
def test_existing_private_handler_is_rejected_before_code_update(tmp_path, script):
    result, calls = run_shell(tmp_path, script, "beta", configured=True,
                             changes={"FAKE_HANDLER": "lambda_handler._run_trusted_calculation"})
    assert result.returncode != 0 and "EXISTING_PUBLIC_HANDLER_REQUIRED" in result.stderr
    assert not any(call[:3] == ["aws", "lambda", "update-function-code"] for call in calls)
    assert not any(call[:3] == ["aws", "apigateway", "put-integration"] for call in calls)


@pytest.mark.parametrize("separator", ["\v", "\f", "\x85", "\u2028", "\u2029", "\r"])
def test_dotenv_never_reinterprets_value_separator_as_another_setting(tmp_path, separator):
    path = tmp_path / "env"
    path.write_bytes(("A=literal" + separator + "STAGE=changed\n").encode())
    with pytest.raises(preflight.PreflightError, match="ENV_FILE_INVALID"):
        preflight.parse_env_file(path)


def test_explicit_storage_destination_must_match_preserved_runtime_source():
    document = configuration()
    document["environments"]["beta"]["calculator"]["storage_bucket"] = "different-bucket"
    with pytest.raises(preflight.PreflightError, match="STORAGE_SOURCE_BINDING_MISMATCH"):
        preflight.validate_configuration("beta", "calculator", document, variables(),
                                         storage_contract=preflight.read_storage_contract())


def test_final_bare_cr_is_not_silently_removed(tmp_path):
    path = tmp_path / "env"
    path.write_bytes(b"FIRST=literal\r")
    with pytest.raises(preflight.PreflightError, match="ENV_FILE_INVALID"):
        preflight.parse_env_file(path)


@pytest.mark.parametrize("key,value,code", [
    ("memory_mb", 127, "LAMBDA_LIMIT_INVALID"), ("memory_mb", 10241, "LAMBDA_LIMIT_INVALID"),
    ("timeout_seconds", 901, "LAMBDA_LIMIT_INVALID"),
    ("log_retention_days", 2, "LOG_RETENTION_INVALID"),
    ("log_retention_days", True, "LOG_RETENTION_INVALID"),
])
def test_invalid_aws_limits_stop_before_external_commands(tmp_path, key, value, code):
    result, calls = run_shell(tmp_path, "deploy_arc_lambda.sh", "beta", configured=True,
                             calculator_changes={key: value})
    assert result.returncode != 0 and code in result.stderr
    assert calls == []


@pytest.mark.parametrize("memory,timeout,retention", [(128, 1, 1), (10240, 900, 3653), (512, 120, None)])
def test_service_limit_boundaries_and_unchanged_retention(memory, timeout, retention):
    document = configuration()
    document["environments"]["beta"]["calculator"].update(
        memory_mb=memory, timeout_seconds=timeout, log_retention_days=retention)
    preflight.validate_configuration("beta", "calculator", document, variables(),
                                     storage_contract=preflight.read_storage_contract())


def test_null_retention_never_changes_or_removes_existing_policy(tmp_path):
    result, calls = run_shell(tmp_path, "deploy_arc_lambda.sh", "beta", configured=True,
                             calculator_changes={"log_retention_days": None})
    assert result.returncode == 0, result.stderr
    assert not any(call[:2] == ["aws", "logs"] for call in calls)
    assert not any("Configuration.LoggingConfig.LogGroup" in call for call in calls)


@pytest.mark.parametrize("configured_group,expected", [
    ("null", "/aws/lambda/FixtureCalc"),
    ('"/shared/arc-journey"', "/shared/arc-journey"),
    ('"None"', "None"),
    ('"-group.with_#valid/chars"', "-group.with_#valid/chars"),
])
def test_retention_uses_actual_logging_destination_before_first_mutation(tmp_path, configured_group, expected):
    result, calls = run_shell(tmp_path, "deploy_arc_lambda.sh", "beta", configured=True,
                             changes={"FAKE_LOG_GROUP_JSON": configured_group})
    assert result.returncode == 0, result.stderr
    lookup = next(i for i, call in enumerate(calls) if "Configuration.LoggingConfig.LogGroup" in call)
    mutation = next(i for i, call in enumerate(calls) if call[:3] == ["aws", "lambda", "update-function-code"])
    assert lookup < mutation
    policies = [call for call in calls if call[:3] == ["aws", "logs", "put-retention-policy"]]
    assert len(policies) == 1 and "--log-group-name=" + expected in policies[0]
    assert "SECRET-MARKER" not in result.stdout + result.stderr


@pytest.mark.parametrize("group", ['""', '"a b"', '"SECRET-MARKER\\nPRIVATE"', '{}', 'None', '"' + 'a' * 513 + '"'])
def test_invalid_log_destination_stops_before_aws_mutation(tmp_path, group):
    result, calls = run_shell(tmp_path, "deploy_arc_lambda.sh", "beta", configured=True,
                             changes={"FAKE_LOG_GROUP_JSON": group})
    assert result.returncode != 0 and "EXISTING_LOG_GROUP_INVALID" in result.stderr
    assert not any(call[0] == "aws" and call[2].startswith(("update-", "put-", "publish-", "create-", "delete-"))
                   for call in calls)
    assert "SECRET-MARKER" not in result.stdout + result.stderr


def test_log_destination_lookup_failure_stops_before_aws_mutation(tmp_path):
    result, calls = run_shell(tmp_path, "deploy_arc_lambda.sh", "beta", configured=True,
                             changes={"FAIL_AWS_QUERY": "Configuration.LoggingConfig.LogGroup"})
    assert result.returncode != 0 and "AWS_READ_FAILED" in result.stderr
    assert not any(call[0] == "aws" and call[2].startswith(("update-", "put-", "publish-", "create-", "delete-"))
                   for call in calls)
    assert "SECRET-MARKER" not in result.stdout + result.stderr


def test_explicit_service_role_path_is_used_without_changing_identity(tmp_path):
    role = "arn:aws:iam::000000000000:role/service-role/FixtureRole"
    result, calls = run_shell(tmp_path, "deploy_arc_lambda.sh", "beta", configured=True,
                             calculator_changes={"role_arn": role}, changes={"FAKE_ROLE": role})
    assert result.returncode == 0, result.stderr
    assert not any(call[:3] == ["aws", "iam", "create-role"] for call in calls)


def test_different_live_role_is_still_rejected_before_mutation(tmp_path):
    result, calls = run_shell(tmp_path, "deploy_arc_lambda.sh", "beta", configured=True,
                             calculator_changes={"role_arn": "arn:aws:iam::000000000000:role/service-role/FixtureRole"})
    assert result.returncode != 0 and "EXISTING_ROLE_BINDING_MISMATCH" in result.stderr
    assert not any(call[:3] == ["aws", "lambda", "update-function-code"] for call in calls)


@pytest.mark.parametrize("role", ["arn:aws:iam::111111111111:role/FixtureRole",
                                 "arn:aws:iam::000000000000:role/service-role/OtherRole",
                                 "arn:aws:iam::000000000000:role/*/FixtureRole"])
def test_explicit_role_cannot_change_the_account_or_name(role):
    document = configuration()
    document["environments"]["beta"]["calculator"]["role_arn"] = role
    with pytest.raises(preflight.PreflightError):
        preflight.validate_configuration("beta", "calculator", document, variables(),
                                         storage_contract=preflight.read_storage_contract())


@pytest.mark.parametrize("key", ["A", "_PRIVATE", "1PRIVATE"])
def test_lambda_env_key_constraints(tmp_path, key):
    path = tmp_path / "env"
    path.write_text(key + "=PRIVATE\n")
    with pytest.raises(preflight.PreflightError, match="ENV_FILE_INVALID"):
        preflight.parse_env_file(path)


def test_environment_limit_counts_utf8_bytes_and_accepts_exact_boundary():
    env = variables()
    remaining = 4096 - sum(len(k.encode()) + len(v.encode()) for k, v in env.items()) - len("PRIVATE")
    env["PRIVATE"] = "한" * (remaining // 3) + "x" * (remaining % 3)
    preflight.validate_configuration("beta", "calculator", configuration(), env,
                                     storage_contract=preflight.read_storage_contract())
    env["PRIVATE"] += "x"
    with pytest.raises(preflight.PreflightError, match="LAMBDA_ENVIRONMENT_TOO_LARGE"):
        preflight.validate_configuration("beta", "calculator", configuration(), env,
                                         storage_contract=preflight.read_storage_contract())


def test_workflow_preflight_precedes_every_aws_credential_step():
    # This static check is intentionally not a claim that GitHub ran the job.
    workflow = (ROOT / ".github/workflows/deploy_arc_lambdas.yml").read_text()
    jobs = re.split(r"^  deploy-(?:dev|prod):\n", workflow, flags=re.MULTILINE)[1:]
    assert len(jobs) == 2
    for job in jobs:
        check = job.index("- name: Offline deployment preflight")
        assert job.index("- name: Write env file") < check
        credentials = [match.start() for match in re.finditer("- name: Configure AWS credentials", job)]
        assert len(credentials) == 2 and all(check < position for position in credentials)
        assert "aws-region: us-east-2" not in job
        assert "${{ steps.preflight.outputs.aws_region }}" in job
        assert "--bindings \"$ARC_DEPLOYMENT_BINDINGS\"" in job
    assert "BETA_AWS" not in workflow and "BETA_ENV" not in workflow


def test_actual_allowlist_never_reads_local_database_keys_or_pipeline_tests(roots, monkeypatch):
    source, packages, output = roots
    forbidden = ("var/local-server/shared-local-instance.db", "var/local-server/identity.json",
                 "var/local-server/resume.key", "var/local-python/private.py", "local_server/private.py",
                 "local_server_tests/private.py", "http_pipeline_tests/private.py", "transport_integration_tests/private.py")
    for name in forbidden:
        write(source, name, "SECRET-MARKER")
    # pip --target installs entry-point scripts alongside the dependency roots.
    write(packages, "bin/jp.py", "SECRET-MARKER")
    denied = {source / name for name in forbidden} | {packages / "bin/jp.py"}
    original = Path.open
    def checked(path, *args, **kwargs):
        assert path not in denied, "Unselected private file was read."
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", checked)
    result = builder.build_artifact(source, packages, output)
    with zipfile.ZipFile(output / "mock-lambda.zip") as archive:
        assert not set(forbidden).intersection(archive.namelist())
        assert "packages/bin/jp.py" not in archive.namelist()
        assert all(b"SECRET-MARKER" not in archive.read(name) for name in archive.namelist())
    assert "lambda_handler.py" in result["files"] and "submit_arc.py" in result["files"]


@pytest.mark.parametrize("script", SCRIPTS)
def test_script_owned_symlink_parent_temp_path_reaches_real_artifact_builder(roots, script):
    """Exercise the real shell prefix, mktemp, strict path check, and ZIP build.

    Stop at the first Python command after checking the produced build path.
    No preflight/deployment/SDK/install command runs. Dependencies are the
    existing artifact fixtures, not fake-success replacement builder logic.
    """
    source, packages, _ = roots
    physical = source.parent / "physical-temp-parent"
    physical.mkdir()
    preserved = physical / "unrelated-sentinel"
    preserved.write_text("keep")
    logical = source.parent / "logical-temp-parent"
    logical.symlink_to(physical, target_is_directory=True)
    commands = source.parent / "path-check-commands"
    commands.mkdir()
    evidence = source.parent / "path-evidence.json"
    generated = source.parent / "generated-path"
    # Keep real mktemp semantics, but deliberately give it a symlink ancestor
    # on every OS rather than relying on the host's default TMPDIR spelling.
    mktemp = commands / "mktemp"
    mktemp.write_text('''#!/bin/bash
set -euo pipefail
created="$(/usr/bin/mktemp -d "${TEST_LOGICAL_PARENT}/build.XXXXXXXX")"
printf '%s' "$created" > "$TEST_GENERATED_PATH"
printf '%s\n' "$created"
''')
    mktemp.chmod(0o700)
    checker = commands / "python3"
    checker.write_text(f'''#!{sys.executable}
import json, os, pathlib, shutil, sys, zipfile
sys.path.insert(0, {str(ROOT)!r})
from scripts import build_mock_artifact as builder
args = sys.argv[1:]
assert args[0].endswith('/deployment_preflight.py')
out = pathlib.Path(args[args.index('--output-dir') + 1])
owned = out.parent
logical = pathlib.Path(os.environ['TEST_GENERATED_PATH']).read_text()
try:
    builder._path(logical, existing=True)
except builder.ArtifactError as error:
    rejected = error.code
else:
    raise AssertionError('Builder unexpectedly accepted the symlink ancestor')
checked = builder._path(owned, existing=True)
assert checked == pathlib.Path(logical).resolve()
assert str(checked) != logical
assert not any(path.is_symlink() for path in (checked, *checked.parents))
dependencies = owned / 'packages'
shutil.copytree({str(packages)!r}, dependencies)
manifest = builder.build_artifact({str(source)!r}, dependencies, owned / 'artifact')
with zipfile.ZipFile(owned / 'artifact' / 'mock-lambda.zip') as archive:
    assert 'lambda_handler.py' in archive.namelist()
    assert 'submit_arc.py' in archive.namelist()
pathlib.Path({str(evidence)!r}).write_text(json.dumps({{
    'logical_rejection': rejected, 'physical_path': str(checked),
    'file_count': len(manifest['files']), 'zip_sha256': manifest['zip']['sha256'],
}}))
sys.exit(73)
''')
    checker.chmod(0o700)
    # A regression must stop before any other external command can run.
    external = source.parent / "unexpected-external-command"
    for name in ("aws", "pip", "rsync", "zip", "curl"):
        command = commands / name
        command.write_text(f'#!/bin/bash\nprintf called > "{external}"\nexit 99\n')
        command.chmod(0o700)
    result = subprocess.run(["/bin/bash", str(ROOT / "scripts" / script), "beta"],
                            env={"PATH": str(commands) + ":/usr/bin:/bin",
                                 "TEST_LOGICAL_PARENT": str(logical), "TEST_GENERATED_PATH": str(generated)},
                            cwd=source.parent, capture_output=True, text=True, timeout=10)
    assert result.returncode == 73, result.stderr
    observed = json.loads(evidence.read_text())
    assert observed["logical_rejection"] == "SYMLINK_NOT_ALLOWED"
    assert observed["file_count"] > 0 and re.fullmatch(r"[0-9a-f]{64}", observed["zip_sha256"])
    # The EXIT trap uses the owned physical path and leaves siblings alone.
    assert not Path(observed["physical_path"]).exists()
    assert not Path(generated.read_text()).exists()
    assert logical.is_symlink() and preserved.read_text() == "keep"
    assert not external.exists()


@pytest.mark.parametrize("changes", [{"FAKE_RUNTIME": "python3.11"}, {"FAKE_PACKAGE_TYPE": "Image"},
                                    {"FAKE_RUNTIME": "None"}, {"FAKE_PACKAGE_TYPE": "SECRET-MARKER"}])
def test_incompatible_existing_runtime_stops_before_any_mutation(tmp_path, changes):
    result, calls = run_shell(tmp_path, "deploy_arc_lambda.sh", "beta", configured=True, changes=changes)
    assert result.returncode != 0 and "EXISTING_PYTHON312_ZIP_REQUIRED" in result.stderr
    assert not any(call[0] == "aws" and call[2].startswith(("update-", "put-", "publish-", "create-", "delete-"))
                   for call in calls)
    assert "SECRET-MARKER" not in result.stdout + result.stderr


@pytest.mark.parametrize("region", ["cn-north-1", "cn-northwest-1", "us-gov-east-1", "us-gov-west-1"])
def test_legacy_scripts_do_not_accept_regions_with_incompatible_hardcoded_arns(region):
    document = configuration()
    document["environments"]["beta"]["region"] = region
    with pytest.raises(preflight.PreflightError, match="DEPLOYMENT_PARTITION_UNSUPPORTED"):
        preflight.validate_configuration("beta", "gateway", document, variables(),
                                         storage_contract=preflight.read_storage_contract())

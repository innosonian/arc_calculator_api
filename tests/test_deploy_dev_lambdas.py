"""Offline counterexamples for scripts/deploy_dev_lambdas.py; fake clients only, no AWS or network."""

import base64
import hashlib
import io
import json
from pathlib import Path
import zipfile

import pytest

from mock_journey.contracts import CURRENT_ADAPTER_VERSION, RETAINED_ADAPTER_VERSIONS
from mock_journey.execution_definitions import PROJECTION_VERSION
from scripts import deploy_dev_lambdas as deployer
from tests.aws_runtime_support import configuration


ROOT = Path(__file__).resolve().parents[1]
REGION = "us-east-2"
FUNCTIONS = {"api": "fixture-dev-api", "worker": "fixture-dev-worker", "relay": "fixture-dev-relay"}
# Fixture values that must never appear in any output line.
PRIVATE_MARKERS = ("fixture-dev-", "fixture-token", "123456789012", "fixture-bucket", "fixture-table")


def configuration_document(role):
    document = json.loads(json.dumps(configuration(role)))
    document["account_id"] = "123456789012"
    document["state"]["table_name"] = "fixture-table"
    document["storage"]["bucket"] = "fixture-bucket"
    return document


class FakeWaiter:
    def __init__(self, client):
        self.client = client

    def wait(self, **kwargs):
        self.client.calls.append(("wait", kwargs["FunctionName"], kwargs.get("WaiterConfig")))
        if self.client.waiter_failures.get(kwargs["FunctionName"]):
            raise RuntimeError("fixture waiter failure with fixture-dev-arn")


class FakeLambda:
    """The subset of the Lambda client the script uses; records every call in order."""

    def __init__(self, configurations, *, update_failures=None, waiter_failures=None, code_sha=None):
        self.configurations = configurations
        self.calls = []
        self.update_failures = update_failures or {}
        self.waiter_failures = waiter_failures or {}
        self.code_sha = code_sha or {}

    def get_function_configuration(self, FunctionName):
        self.calls.append(("get", FunctionName))
        if FunctionName not in self.configurations:
            raise RuntimeError("fixture ResourceNotFound for " + FunctionName)
        return json.loads(json.dumps(self.configurations[FunctionName]))

    def update_function_code(self, FunctionName, ZipFile, Publish):
        self.calls.append(("update", FunctionName, hashlib.sha256(ZipFile).hexdigest(), Publish))
        if self.update_failures.get(FunctionName):
            raise RuntimeError("fixture update failure with fixture-dev-arn")
        configuration = self.configurations[FunctionName]
        configuration["CodeSha256"] = self.code_sha.get(
            FunctionName, base64.b64encode(hashlib.sha256(ZipFile).digest()).decode("ascii"))
        configuration["LastModified"] = "2026-09-30T00:00:00.000+0000"
        configuration["LastUpdateStatus"] = "Successful"

    def get_waiter(self, name):
        assert name == "function_updated_v2"
        return FakeWaiter(self)


def lambda_configurations(**overrides):
    result = {}
    for role, name in FUNCTIONS.items():
        variables = {"STAGE": "dev", "ARC_STORAGE_REGION": REGION}
        if role != "relay":
            document = configuration_document(role)
            if role in overrides:
                document = overrides[role](document)
            variables["ARC_JOURNEY_CONFIG"] = json.dumps(document) if isinstance(document, dict) else document
        result[name] = {"FunctionName": name, "Runtime": "python3.12", "PackageType": "Zip",
                        "Environment": {"Variables": variables},
                        "CodeSha256": "fixture-before", "LastModified": "2026-09-01T00:00:00.000+0000"}
    return result


@pytest.fixture
def fake_lambda(monkeypatch):
    holder = {}

    def install(client):
        holder["client"] = client
        monkeypatch.setattr(deployer, "lambda_client", lambda region: client)
        return client
    return install


def run(capsys, *argv):
    code = deployer.main(list(argv))
    captured = capsys.readouterr()
    lines = [json.loads(line) for line in captured.out.splitlines()]
    visible = captured.out + captured.err
    for marker in PRIVATE_MARKERS:
        if marker == "fixture-dev-" and argv[0] == "deploy":
            continue  # deploy prints function names on purpose (never configuration values)
        assert marker not in visible, marker
    assert "ARC_JOURNEY_CONFIG\": {" not in visible and "\"schema\"" not in visible
    return code, lines


# --- check-config ---------------------------------------------------------

def test_check_config_passes_when_every_role_matches_the_code_registry(fake_lambda, capsys):
    client = fake_lambda(FakeLambda(lambda_configurations()))
    code, lines = run(capsys, "check-config", "--region", REGION,
                      "--function", "api=" + FUNCTIONS["api"], "--function", "worker=" + FUNCTIONS["worker"])
    assert code == 0
    assert lines == [{"role": "api", "check": "config_matches_code_registry"},
                     {"role": "worker", "check": "config_matches_code_registry"}]
    assert client.calls == [("get", FUNCTIONS["api"]), ("get", FUNCTIONS["worker"])]
    assert configuration("api")["execution"] == {"current_adapter_version": CURRENT_ADAPTER_VERSION,
                                                "projection_version": PROJECTION_VERSION,
                                                "retained_adapter_versions": list(RETAINED_ADAPTER_VERSIONS)}


def set_execution(field, value):
    def mutate(document):
        document["execution"][field] = value
        return document
    return mutate


@pytest.mark.parametrize("mutate,field,expected", [
    (set_execution("current_adapter_version", "arc-fixture-v0"), "execution.current_adapter_version", CURRENT_ADAPTER_VERSION),
    (set_execution("retained_adapter_versions", list(reversed(RETAINED_ADAPTER_VERSIONS))),
     "execution.retained_adapter_versions", list(RETAINED_ADAPTER_VERSIONS)),
    (set_execution("retained_adapter_versions", []), "execution.retained_adapter_versions", list(RETAINED_ADAPTER_VERSIONS)),
    (set_execution("retained_adapter_versions", list(RETAINED_ADAPTER_VERSIONS[:1])),
     "execution.retained_adapter_versions", list(RETAINED_ADAPTER_VERSIONS)),
    (set_execution("projection_version", "arc-fixture-projection"), "execution.projection_version", PROJECTION_VERSION),
    (lambda document: {**document, "role": "api"}, "role", "worker"),
    (lambda document: {k: v for k, v in document.items() if k != "execution"}, "execution", "object"),
    (lambda document: "{not json", "ARC_JOURNEY_CONFIG", "json_object"),
    (lambda document: "[]", "ARC_JOURNEY_CONFIG", "json_object"),
    (lambda document: "", "ARC_JOURNEY_CONFIG", "present"),
], ids=["current", "retained_order", "retained_empty", "retained_partial", "projection", "role", "no_execution",
        "not_json", "json_array", "empty"])
def test_check_config_reports_drift_with_only_the_role_field_and_code_expectation(fake_lambda, capsys, mutate, field, expected):
    fake_lambda(FakeLambda(lambda_configurations(worker=mutate)))
    code, lines = run(capsys, "check-config", "--region", REGION,
                      "--function", "api=" + FUNCTIONS["api"], "--function", "worker=" + FUNCTIONS["worker"])
    assert code == 2
    assert lines[-1] == {"code": "CONFIG_DRIFT", "role": "worker", "field": field, "expected": expected}
    assert lines[0]["role"] == "api", "the API passed before the Worker drift was found"


def test_check_config_reports_a_missing_variable_without_naming_the_others(fake_lambda, capsys):
    configurations = lambda_configurations()
    del configurations[FUNCTIONS["api"]]["Environment"]["Variables"]["ARC_JOURNEY_CONFIG"]
    fake_lambda(FakeLambda(configurations))
    code, lines = run(capsys, "check-config", "--region", REGION, "--function", "api=" + FUNCTIONS["api"])
    assert code == 2 and lines == [{"code": "CONFIG_DRIFT", "role": "api", "field": "ARC_JOURNEY_CONFIG", "expected": "present"}]


def test_check_config_rejects_a_document_the_settings_parser_rejects(fake_lambda, capsys):
    def mutate(document):
        document["course"]["catalog_version"] = "fixture-catalog"
        return document
    fake_lambda(FakeLambda(lambda_configurations(api=mutate)))
    code, lines = run(capsys, "check-config", "--region", REGION, "--function", "api=" + FUNCTIONS["api"])
    assert code == 2 and lines == [{"code": "CONFIG_DRIFT", "role": "api", "field": "AwsSettings.parse", "expected": "valid"}]


def test_check_config_aws_failure_is_a_fixed_code_without_the_exception_text(fake_lambda, capsys):
    fake_lambda(FakeLambda({}))
    code, lines = run(capsys, "check-config", "--region", REGION, "--function", "api=" + FUNCTIONS["api"])
    assert code == 3 and lines == [{"code": "AWS_CALL_FAILED", "role": "api", "call": "GetFunctionConfiguration"}]


@pytest.mark.parametrize("argv", [
    ("check-config", "--region", "US-EAST-2", "--function", "api=" + FUNCTIONS["api"]),
    ("check-config", "--region", REGION, "--function", "calculator=" + FUNCTIONS["api"]),
    ("check-config", "--region", REGION, "--function", "api=arn:aws:lambda:us-east-2:123456789012:function:x"),
    ("check-config", "--region", REGION, "--function", "api=" + FUNCTIONS["api"], "--function", "api=" + FUNCTIONS["api"]),
    ("check-config", "--region", REGION, "--function", FUNCTIONS["api"]),
])
def test_check_config_rejects_invalid_arguments_before_any_client_is_created(monkeypatch, capsys, argv):
    def forbidden(region):
        pytest.fail("no client before argument validation")
    monkeypatch.setattr(deployer, "lambda_client", forbidden)
    code, lines = run(capsys, *argv)
    assert code == 3 and lines[0]["code"] == "ARGUMENT_INVALID"


# --- deploy ---------------------------------------------------------------

def artifact(tmp_path, *, manifest=True, manifest_digest=None):
    output = tmp_path / "artifact"
    output.mkdir()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("lambda_handler.py", "def run(event, context): return None\n")
    data = buffer.getvalue()
    (output / "mock-lambda.zip").write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    if manifest:
        (output / "artifact-manifest.json").write_text(
            json.dumps({"schema": "arc-mock-local-artifact-v1", "zip": {"sha256": manifest_digest or digest, "size": len(data)}}))
    return output, digest, base64.b64encode(bytes.fromhex(digest)).decode("ascii")


def deploy_argv(output, report, *, manifest=True):
    argv = ["deploy", "--region", REGION, "--zip", str(output / "mock-lambda.zip"),
            "--function", "worker=" + FUNCTIONS["worker"], "--function", "relay=" + FUNCTIONS["relay"],
            "--function", "api=" + FUNCTIONS["api"], "--report", str(report)]
    if manifest:
        argv += ["--manifest", str(output / "artifact-manifest.json")]
    return argv


def test_deploy_updates_worker_relay_api_in_order_waits_and_compares_code_sha(fake_lambda, capsys, tmp_path):
    output, digest, code_sha = artifact(tmp_path)
    client = fake_lambda(FakeLambda(lambda_configurations()))
    report = tmp_path / "deploy-report.json"
    code, lines = run(capsys, *deploy_argv(output, report))
    assert code == 0
    order = [FUNCTIONS[role] for role in ("worker", "relay", "api")]
    assert [call for call in client.calls if call[0] == "update"] == [("update", name, digest, False) for name in order]
    assert [call for call in client.calls if call[0] == "wait"] == [("wait", name, deployer.WAITER_CONFIG) for name in order]
    # Pre-check reads, then per function: update -> wait -> read back.
    assert client.calls[:3] == [("get", name) for name in order]
    assert client.calls[3:] == [item for name in order for item in
                                (("update", name, digest, False), ("wait", name, deployer.WAITER_CONFIG), ("get", name))]
    assert lines == [{"FunctionName": name, "CodeSha256": code_sha, "LastModified": "2026-09-30T00:00:00.000+0000"}
                     for name in order]
    saved = json.loads(report.read_text())
    assert saved["status"] == "completed" and saved["zip_sha256"] == digest
    assert [entry["role"] for entry in saved["functions"]] == ["worker", "relay", "api"]
    assert all(entry["code_sha256_matches"] is True for entry in saved["functions"])


def test_deploy_stops_at_the_first_code_sha_mismatch_and_keeps_updated_functions_in_the_report(fake_lambda, capsys, tmp_path):
    output, _, _ = artifact(tmp_path)
    client = fake_lambda(FakeLambda(lambda_configurations(), code_sha={FUNCTIONS["relay"]: "ZmFrZS1zaGE="}))
    report = tmp_path / "deploy-report.json"
    code, lines = run(capsys, *deploy_argv(output, report))
    assert code == 4
    assert lines[-1] == {"code": "CODE_SHA_MISMATCH", "role": "relay", "updated": ["worker"]}
    assert [call[1] for call in client.calls if call[0] == "update"] == [FUNCTIONS["worker"], FUNCTIONS["relay"]]
    saved = json.loads(report.read_text())
    assert saved["status"] == "failed" and saved["failed_role"] == "relay" and saved["code"] == "CODE_SHA_MISMATCH"
    assert [(entry["role"], entry["code_sha256_matches"]) for entry in saved["functions"]] == [("worker", True), ("relay", False)]


@pytest.mark.parametrize("failure", ["update", "waiter"])
def test_deploy_stops_at_the_first_aws_failure_without_touching_later_functions(fake_lambda, capsys, tmp_path, failure):
    output, _, _ = artifact(tmp_path)
    kwargs = {"update_failures": {FUNCTIONS["relay"]: True}} if failure == "update" else {"waiter_failures": {FUNCTIONS["relay"]: True}}
    client = fake_lambda(FakeLambda(lambda_configurations(), **kwargs))
    report = tmp_path / "deploy-report.json"
    code, lines = run(capsys, *deploy_argv(output, report))
    assert code == 3
    assert lines[-1] == {"code": "AWS_CALL_FAILED", "role": "relay", "call": "UpdateFunctionCode", "updated": ["worker"]}
    assert FUNCTIONS["api"] not in [call[1] for call in client.calls if call[0] == "update"]
    saved = json.loads(report.read_text())
    assert saved["status"] == "failed" and [entry["role"] for entry in saved["functions"]] == ["worker"]


@pytest.mark.parametrize("change", ["runtime", "package_type", "missing"])
def test_deploy_refuses_an_unsupported_target_before_the_first_upload(fake_lambda, capsys, tmp_path, change):
    output, _, _ = artifact(tmp_path)
    configurations = lambda_configurations()
    if change == "runtime":
        configurations[FUNCTIONS["api"]]["Runtime"] = "python3.11"
    elif change == "package_type":
        configurations[FUNCTIONS["api"]]["PackageType"] = "Image"
    else:
        del configurations[FUNCTIONS["api"]]
    client = fake_lambda(FakeLambda(configurations))
    code, lines = run(capsys, *deploy_argv(output, tmp_path / "deploy-report.json"))
    assert code == 3 and lines[-1]["code"] in ("FUNCTION_UNSUPPORTED", "AWS_CALL_FAILED") and lines[-1]["role"] == "api"
    assert not [call for call in client.calls if call[0] == "update"]


@pytest.mark.parametrize("change,code", [
    ("manifest_digest", "ARTIFACT_MANIFEST_MISMATCH"),
    ("manifest_missing", "ARTIFACT_MANIFEST_INVALID"),
    ("zip_missing", "ARTIFACT_MISSING"),
    ("zip_corrupt", "ARTIFACT_INVALID"),
])
def test_deploy_validates_the_artifact_before_any_aws_call(fake_lambda, capsys, tmp_path, change, code):
    output, _, _ = artifact(tmp_path, manifest_digest="0" * 64 if change == "manifest_digest" else None)
    if change == "manifest_missing":
        (output / "artifact-manifest.json").unlink()
    elif change == "zip_missing":
        (output / "mock-lambda.zip").unlink()
    elif change == "zip_corrupt":
        (output / "mock-lambda.zip").write_bytes(b"not a zip archive")
    client = fake_lambda(FakeLambda(lambda_configurations()))
    result, lines = run(capsys, *deploy_argv(output, tmp_path / "deploy-report.json"))
    assert result == 3 and lines == [{"code": code}]
    assert client.calls == []


# --- smoke ----------------------------------------------------------------

class FakeResponse:
    def __init__(self, status, body):
        self.status, self.body = status, json.dumps(body).encode("utf-8")

    def read(self, _limit=None):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class FakeHTTPError(deployer.urllib.error.HTTPError):
    def __init__(self, status, body):
        super().__init__("https://fixture.invalid", status, "fixture", {}, io.BytesIO(json.dumps(body).encode("utf-8")))


LOGIN_OK = {"data": {"sessionId": "00000000-0000-4000-8000-000000000000", "expiresAt": "2026-10-01T00:00:00Z",
                     "learningAvailability": {"state": "available"}, "accessToken": "fixture-token-value",
                     "tokenType": "Bearer", "userName": "Test User"}}
PROGRESS_OK = {"data": {"results": [{"courseId": "c1"}], "count": 15, "next": None, "previous": None}}


def install_http(monkeypatch, responses):
    """Serve (method, path) -> response in order; records every request (headers included) for assertions."""
    requests = []

    def open_url(request, timeout):
        assert timeout == deployer.HTTP_TIMEOUT_SECONDS
        requests.append((request.get_method(), request.full_url, dict(request.header_items()), request.data))
        response = responses[(request.get_method(), request.full_url)]
        if isinstance(response, Exception):
            raise response
        return response
    monkeypatch.setattr(deployer, "open_url", open_url)
    return requests


BASE = "https://fixture-api.example.invalid/dev"


def test_smoke_logs_in_lists_courses_and_never_logs_out_or_prints_the_token(monkeypatch, capsys, tmp_path):
    requests = install_http(monkeypatch, {
        ("POST", BASE + "/api/v2/sessions/"): FakeResponse(201, LOGIN_OK),
        ("GET", BASE + "/api/v2/courses/progress/?page=1&pageSize=100"): FakeResponse(200, PROGRESS_OK),
    })
    report = tmp_path / "smoke-report.json"
    code, lines = run(capsys, "smoke", "--base-url", BASE + "/", "--report", str(report))
    assert code == 0
    assert lines == [{"step": "login", "status": 201}, {"step": "progress", "status": 200, "count": 15}]
    assert [(method, url) for method, url, _, _ in requests] == [
        ("POST", BASE + "/api/v2/sessions/"), ("GET", BASE + "/api/v2/courses/progress/?page=1&pageSize=100")]
    assert json.loads(requests[0][3]) == {"loginId": "test@test.com", "password": "2222"}
    assert requests[1][2]["Authorization"] == "Bearer fixture-token-value"
    assert all(method != "DELETE" for method, _, _, _ in requests), "the shared Dummy progress must not be reset"
    saved = report.read_text()
    assert "fixture-token" not in saved and json.loads(saved)["status"] == "completed"


@pytest.mark.parametrize("login,progress,step,status,reason", [
    (FakeHTTPError(401, {"error": {"code": "LOGIN_FAILED", "message": "Login failed."}}), None, "login", 401, "LOGIN_FAILED"),
    (FakeResponse(200, LOGIN_OK), None, "login", 200, "UNEXPECTED_STATUS"),
    (FakeResponse(201, {"data": {**LOGIN_OK["data"], "tokenType": "Token"}}), None, "login", 201, "TOKEN_TYPE_INVALID"),
    (FakeResponse(201, {"data": {**LOGIN_OK["data"], "userName": ""}}), None, "login", 201, "USER_NAME_MISSING"),
    (FakeResponse(201, {"data": {**LOGIN_OK["data"], "accessToken": ""}}), None, "login", 201, "ACCESS_TOKEN_MISSING"),
    (FakeResponse(201, LOGIN_OK), FakeHTTPError(503, {"error": {"code": "TEMPORARILY_UNAVAILABLE", "message": "x"}}),
     "progress", 503, "TEMPORARILY_UNAVAILABLE"),
    (FakeResponse(201, LOGIN_OK), FakeResponse(200, {"data": {"results": [], "count": 0}}), "progress", 200, "COURSE_LIST_EMPTY"),
    (FakeResponse(201, LOGIN_OK), FakeHTTPError(500, {"error": {"code": "<html>", "message": "x"}}),
     "progress", 500, "ERROR_CODE_INVALID"),
    (deployer.urllib.error.URLError("fixture connection refused fixture-dev-host"), None, "login", None, "REQUEST_FAILED"),
], ids=["login_401", "login_200", "token_type", "user_name", "access_token", "progress_503", "empty", "bad_code", "unreachable"])
def test_smoke_failures_print_only_step_status_and_fixed_codes(monkeypatch, capsys, tmp_path, login, progress, step, status, reason):
    responses = {("POST", BASE + "/api/v2/sessions/"): login}
    if progress is not None:
        responses[("GET", BASE + "/api/v2/courses/progress/?page=1&pageSize=100")] = progress
    install_http(monkeypatch, responses)
    report = tmp_path / "smoke-report.json"
    code, lines = run(capsys, "smoke", "--base-url", BASE, "--report", str(report))
    assert code == 5
    assert lines[-1] == {"code": "SMOKE_FAILED", "step": step, "status": status, "reason": reason}
    assert "Login failed" not in capsys.readouterr().out
    saved = json.loads(report.read_text())
    assert saved["status"] == "failed" and saved["failed_step"] == step and "fixture-token" not in report.read_text()


@pytest.mark.parametrize("url", ["http://fixture-api.example.invalid/dev", "https://user:pw@fixture-api.example.invalid/dev",
                                 "https://fixture-api.example.invalid/dev?x=1", "", "fixture-api.example.invalid"])
def test_smoke_rejects_a_base_url_that_is_not_plain_https(monkeypatch, capsys, url):
    def forbidden(request, timeout):
        pytest.fail("no request for an invalid base URL")
    monkeypatch.setattr(deployer, "open_url", forbidden)
    code, lines = run(capsys, "smoke", "--base-url", url)
    assert code == 3 and lines == [{"code": "ARGUMENT_INVALID", "argument": "base-url"}]


# --- summary --------------------------------------------------------------

def test_summary_lists_commit_artifact_functions_and_smoke_without_any_token(tmp_path, capsys):
    output, digest, code_sha = artifact(tmp_path)
    deploy_report = tmp_path / "deploy-report.json"
    deploy_report.write_text(json.dumps({"status": "completed", "zip_sha256": digest, "functions": [
        {"role": "worker", "FunctionName": FUNCTIONS["worker"], "CodeSha256": code_sha,
         "LastModified": "2026-09-30T00:00:00.000+0000", "code_sha256_matches": True}]}))
    smoke_report = tmp_path / "smoke-report.json"
    smoke_report.write_text(json.dumps({"status": "completed", "steps": [
        {"step": "login", "status": 201, "error_code": None}, {"step": "progress", "status": 200, "error_code": None, "count": 15}]}))
    target = tmp_path / "summary.md"
    target.write_text("existing\n")
    code = deployer.main(["summary", "--commit", "a" * 40, "--zip", str(output / "mock-lambda.zip"),
                          "--deploy-report", str(deploy_report), "--smoke-report", str(smoke_report), "--output", str(target)])
    assert code == 0 and capsys.readouterr().out == ""
    text = target.read_text()
    assert text.startswith("existing\n## Dev deployment (D137)")
    assert "a" * 40 in text and digest in text and code_sha in text and FUNCTIONS["worker"] in text
    assert "login: HTTP 201" in text and "progress: HTTP 200, count 15" in text
    assert "fixture-token" not in text and "Bearer" not in text


def test_summary_reports_missing_artifact_and_reports_as_not_run(tmp_path, capsys):
    code = deployer.main(["summary", "--commit", "b" * 40, "--zip", str(tmp_path / "absent.zip"),
                          "--deploy-report", str(tmp_path / "absent.json"), "--smoke-report", str(tmp_path / "absent2.json")])
    text = capsys.readouterr().out
    assert code == 0 and "artifact: not built or not downloaded" in text and "deploy: not run" in text and "smoke: not run" in text


def test_summary_rejects_a_commit_that_is_not_a_full_sha(tmp_path, capsys):
    code = deployer.main(["summary", "--commit", "main", "--zip", str(tmp_path / "absent.zip")])
    assert code == 3 and json.loads(capsys.readouterr().out) == {"code": "ARGUMENT_INVALID", "argument": "commit"}


# --- workflow binding -----------------------------------------------------

def test_workflow_invokes_the_subcommands_this_script_defines():
    text = (ROOT / ".github/workflows/deploy_dev.yml").read_text()
    for subcommand in ("check-config", "deploy", "smoke", "summary"):
        assert f"scripts/deploy_dev_lambdas.py {subcommand}" in text
    parser = deployer.build_parser()
    assert set(parser._subparsers._group_actions[0].choices) == {"check-config", "deploy", "smoke", "summary"}


def test_script_invocation_resolves_the_code_registry_without_the_repository_on_sys_path(tmp_path):
    # The workflow runs `python scripts/deploy_dev_lambdas.py ...` from a bare
    # environment: scripts/ is sys.path[0], the repository root is not on the path,
    # and no STAGE or PYTHONPATH is exported to that step.
    import os
    import subprocess
    import sys
    from pathlib import Path

    from mock_journey.contracts import CURRENT_ADAPTER_VERSION, RETAINED_ADAPTER_VERSIONS
    from mock_journey.execution_definitions import PROJECTION_VERSION

    root = Path(__file__).resolve().parents[1]
    script = root / "scripts/deploy_dev_lambdas.py"
    probe = (
        "import json, runpy, sys\n"
        f"sys.path[0] = {str(script.parent)!r}\n"
        f"assert {str(root)!r} not in sys.path\n"
        f"ns = runpy.run_path({str(script)!r}, run_name='deploy_probe')\n"
        "from mock_journey.aws_settings import AwsSettings\n"
        "print(json.dumps(ns['expected_execution']()))\n"
    )
    environment = {key: value for key, value in os.environ.items()
                   if key in ("PATH", "HOME", "SYSTEMROOT", "TMPDIR")}
    completed = subprocess.run([sys.executable, "-c", probe], cwd=tmp_path, env=environment,
                               capture_output=True, text=True, timeout=60)
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "current_adapter_version": CURRENT_ADAPTER_VERSION,
        "retained_adapter_versions": list(RETAINED_ADAPTER_VERSIONS),
        "projection_version": PROJECTION_VERSION,
    }


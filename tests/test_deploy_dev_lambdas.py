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
PRIVATE_MARKERS = ("fixture-dev-", "fixture-token", "123456789012", "fixture-bucket", "fixture-table",
                   "fixture-other-value", "arn:aws")
CODE_REGISTRY = {"current_adapter_version": CURRENT_ADAPTER_VERSION,
                 "retained_adapter_versions": list(RETAINED_ADAPTER_VERSIONS),
                 "projection_version": PROJECTION_VERSION}
# The registry of the revision before D138 (a rollback target): v4 current, two retained versions.
PREVIOUS_REGISTRY = {"current_adapter_version": "arc-internal-detection-v4",
                     "retained_adapter_versions": ["arc-local-calculator-pending-v2",
                                                   "arc-internal-detection-pending-v3"],
                     "projection_version": PROJECTION_VERSION}


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


class FakeClientError(Exception):
    """The shape of botocore.exceptions.ClientError the script reads (``response`` only)."""

    def __init__(self, code, message):
        super().__init__(message)
        self.response = {"Error": {"Code": code, "Message": message}}


DENIED_MESSAGE = ("User: arn:aws:sts::123456789012:assumed-role/fixture-dev-deploy/arc-deploy-development is not "
                  "authorized to perform: lambda:UpdateFunctionConfiguration on resource: "
                  "arn:aws:lambda:us-east-2:123456789012:function:fixture-dev-worker")


class FakeLambda:
    """The subset of the Lambda client the script uses; records every call in order."""

    def __init__(self, configurations, *, update_failures=None, waiter_failures=None, code_sha=None,
                 config_failures=None, config_ignored=()):
        self.configurations = configurations
        self.calls = []
        self.update_failures = update_failures or {}
        self.waiter_failures = waiter_failures or {}
        self.code_sha = code_sha or {}
        self.config_failures = config_failures or {}
        self.config_ignored = set(config_ignored)
        self.config_requests = []

    def get_function_configuration(self, FunctionName):
        self.calls.append(("get", FunctionName))
        if FunctionName not in self.configurations:
            raise RuntimeError("fixture ResourceNotFound for " + FunctionName)
        return json.loads(json.dumps(self.configurations[FunctionName]))

    def update_function_configuration(self, **request):
        name = request["FunctionName"]
        self.calls.append(("config", name))
        self.config_requests.append(json.loads(json.dumps(request)))
        if name in self.config_failures:
            raise self.config_failures[name]
        assert set(request) <= {"FunctionName", "Environment", "RevisionId"}, "no other setting is sent"
        configuration = self.configurations[name]
        assert request.get("RevisionId") == configuration["RevisionId"]
        if name not in self.config_ignored:
            configuration["Environment"] = json.loads(json.dumps(request["Environment"]))
        if configuration["RevisionId"] is not None:
            configuration["RevisionId"] += "-next"

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
        variables = {"STAGE": "dev", "ARC_STORAGE_REGION": REGION, "ARC_FIXTURE_OTHER": "fixture-other-value"}
        if role != "relay":
            document = configuration_document(role)
            if role in overrides:
                document = overrides[role](document)
            variables["ARC_JOURNEY_CONFIG"] = json.dumps(document) if isinstance(document, dict) else document
        result[name] = {"FunctionName": name, "Runtime": "python3.12", "PackageType": "Zip",
                        "Environment": {"Variables": variables}, "RevisionId": "fixture-revision-" + role,
                        "CodeSha256": "fixture-before", "LastModified": "2026-09-01T00:00:00.000+0000"}
    return result


def stored_document(client, role):
    return json.loads(client.configurations[FUNCTIONS[role]]["Environment"]["Variables"]["ARC_JOURNEY_CONFIG"])


@pytest.fixture
def fake_lambda(monkeypatch):
    holder = {}

    def install(client):
        holder["client"] = client
        monkeypatch.setattr(deployer, "lambda_client", lambda region: client)
        return client
    return install


@pytest.fixture
def registry(tmp_path):
    """Write an execution-registry.json (the code's by default) and return its path."""
    def write(document=None, name="execution-registry.json"):
        path = tmp_path / name
        path.write_text(json.dumps(CODE_REGISTRY if document is None else document), encoding="utf-8")
        return str(path)
    return write


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


# --- registry -------------------------------------------------------------

def test_registry_writes_the_execution_versions_of_the_checked_out_code(tmp_path, capsys):
    target = tmp_path / "execution-registry.json"
    code, lines = run(capsys, "registry", "--output", str(target))
    assert code == 0 and lines == [{"registry": "written", **CODE_REGISTRY}]
    assert json.loads(target.read_text()) == CODE_REGISTRY
    assert deployer.load_registry(str(target)) == CODE_REGISTRY
    assert list(deployer.load_registry(str(target))) == list(deployer.REGISTRY_FIELDS)


def test_registry_reports_an_unwritable_output_with_a_fixed_code(tmp_path, capsys):
    code, lines = run(capsys, "registry", "--output", str(tmp_path / "absent-directory" / "registry.json"))
    assert code == 3 and lines == [{"code": "REGISTRY_WRITE_FAILED"}]


INVALID_REGISTRIES = {
    "absent": None,
    "not_json": "{not json",
    "array": "[]",
    "missing_field": json.dumps({k: v for k, v in CODE_REGISTRY.items() if k != "projection_version"}),
    "extra_field": json.dumps({**CODE_REGISTRY, "role": "api"}),
    "retained_text": json.dumps({**CODE_REGISTRY, "retained_adapter_versions": "arc-internal-detection-v4"}),
    "retained_number": json.dumps({**CODE_REGISTRY, "retained_adapter_versions": [4]}),
    "current_empty": json.dumps({**CODE_REGISTRY, "current_adapter_version": ""}),
    "current_not_a_version_name": json.dumps({**CODE_REGISTRY, "current_adapter_version": "v5\"} injected"}),
    "projection_null": json.dumps({**CODE_REGISTRY, "projection_version": None}),
    "too_large": json.dumps({**CODE_REGISTRY, "retained_adapter_versions": ["v"] * 40000}),
}


def invalid_registry(tmp_path, change):
    path = tmp_path / "execution-registry.json"
    if INVALID_REGISTRIES[change] is not None:
        path.write_text(INVALID_REGISTRIES[change], encoding="utf-8")
    return str(path)


# --- check-config ---------------------------------------------------------

def check_argv(registry_path, *roles):
    argv = ["check-config", "--region", REGION, "--registry", registry_path]
    for role in roles or ("api", "worker"):
        argv += ["--function", role + "=" + FUNCTIONS[role]]
    return argv


def test_check_config_passes_when_every_role_matches_the_code_registry(fake_lambda, capsys, registry):
    client = fake_lambda(FakeLambda(lambda_configurations()))
    code, lines = run(capsys, *check_argv(registry()))
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


def previous_execution(document):
    document["execution"] = {"current_adapter_version": PREVIOUS_REGISTRY["current_adapter_version"],
                             "projection_version": PREVIOUS_REGISTRY["projection_version"],
                             "retained_adapter_versions": list(PREVIOUS_REGISTRY["retained_adapter_versions"])}
    return document


def drop_execution_field(field):
    def mutate(document):
        del document["execution"][field]
        return document
    return mutate


@pytest.mark.parametrize("mutate,fields", [
    (set_execution("current_adapter_version", "arc-fixture-v0"), ["execution.current_adapter_version"]),
    (set_execution("retained_adapter_versions", list(reversed(RETAINED_ADAPTER_VERSIONS))),
     ["execution.retained_adapter_versions"]),
    (set_execution("retained_adapter_versions", []), ["execution.retained_adapter_versions"]),
    (set_execution("retained_adapter_versions", list(RETAINED_ADAPTER_VERSIONS[:1])),
     ["execution.retained_adapter_versions"]),
    (set_execution("projection_version", "arc-fixture-projection"), ["execution.projection_version"]),
    (previous_execution, ["execution.current_adapter_version", "execution.retained_adapter_versions"]),
    (drop_execution_field("projection_version"), ["execution.projection_version"]),
    (set_execution("fixture-other-value", "fixture-other-value"), ["execution.extra_keys"]),
    (lambda document: {**document, "execution": {}},
     ["execution.current_adapter_version", "execution.retained_adapter_versions", "execution.projection_version"]),
], ids=["current", "retained_order", "retained_empty", "retained_partial", "projection", "previous_revision",
        "missing_field", "extra_key", "empty_block"])
def test_check_config_passes_and_names_the_fields_the_deploy_will_sync(fake_lambda, capsys, registry, mutate, fields):
    # D140: a difference inside the execution block is no longer a failure.
    client = fake_lambda(FakeLambda(lambda_configurations(worker=mutate)))
    code, lines = run(capsys, *check_argv(registry()))
    assert code == 0
    assert lines == [{"role": "api", "check": "config_matches_code_registry"},
                     {"role": "worker", "check": "execution_will_be_synced", "fields": fields}]
    assert [call[0] for call in client.calls] == ["get", "get"], "check-config never writes"


@pytest.mark.parametrize("mutate,field,expected", [
    (lambda document: {**document, "role": "api"}, "role", "worker"),
    (lambda document: {k: v for k, v in document.items() if k != "role"}, "role", "worker"),
    (lambda document: {k: v for k, v in document.items() if k != "execution"}, "execution", "object"),
    (lambda document: {**document, "execution": [CURRENT_ADAPTER_VERSION]}, "execution", "object"),
    (lambda document: {**document, "execution": None}, "execution", "object"),
    (lambda document: "{not json", "ARC_JOURNEY_CONFIG", "json_object"),
    (lambda document: "[]", "ARC_JOURNEY_CONFIG", "json_object"),
    (lambda document: json.dumps(document)[:-1] + ', "role": "worker"}', "ARC_JOURNEY_CONFIG", "json_object"),
    (lambda document: json.dumps(document).replace('"max_conflict_retries": 4', '"max_conflict_retries": NaN'),
     "ARC_JOURNEY_CONFIG", "json_object"),
    (lambda document: "", "ARC_JOURNEY_CONFIG", "present"),
], ids=["role", "no_role", "no_execution", "execution_array", "execution_null", "not_json", "json_array",
        "duplicate_key", "nan", "empty"])
def test_check_config_reports_drift_with_only_the_role_field_and_expectation(fake_lambda, capsys, registry, mutate,
                                                                             field, expected):
    # Everything outside the execution block still stops the run before any code is replaced.
    fake_lambda(FakeLambda(lambda_configurations(worker=mutate)))
    code, lines = run(capsys, *check_argv(registry()))
    assert code == 2
    assert lines[-1] == {"code": "CONFIG_DRIFT", "role": "worker", "field": field, "expected": expected}
    assert lines[0]["role"] == "api", "the API passed before the Worker drift was found"


def test_check_config_reports_a_missing_variable_without_naming_the_others(fake_lambda, capsys, registry):
    configurations = lambda_configurations()
    del configurations[FUNCTIONS["api"]]["Environment"]["Variables"]["ARC_JOURNEY_CONFIG"]
    fake_lambda(FakeLambda(configurations))
    code, lines = run(capsys, *check_argv(registry(), "api"))
    assert code == 2 and lines == [{"code": "CONFIG_DRIFT", "role": "api", "field": "ARC_JOURNEY_CONFIG", "expected": "present"}]


def other_catalog(document):
    document["course"]["catalog_version"] = "fixture-catalog"
    return document


@pytest.mark.parametrize("mutate", [other_catalog, lambda document: previous_execution(other_catalog(document))],
                         ids=["execution_matches", "execution_will_be_synced"])
def test_check_config_rejects_a_document_the_settings_parser_rejects(fake_lambda, capsys, registry, mutate):
    # With the code's own registry the document is parsed as it will be after the sync,
    # so a problem outside the execution block is found even when the block differs.
    fake_lambda(FakeLambda(lambda_configurations(api=mutate)))
    code, lines = run(capsys, *check_argv(registry(), "api"))
    assert code == 2 and lines == [{"code": "CONFIG_DRIFT", "role": "api", "field": "AwsSettings.parse", "expected": "valid"}]


def test_check_config_with_a_rollback_registry_skips_the_checked_out_parser(fake_lambda, capsys, registry):
    # The artifact was built from another revision: its own code will parse the document,
    # so the checked-out AwsSettings.parse (which demands the current registry) is not applied.
    fake_lambda(FakeLambda(lambda_configurations(worker=previous_execution)))
    code, lines = run(capsys, *check_argv(registry(PREVIOUS_REGISTRY)))
    assert code == 0
    assert lines == [{"check": "settings_parse_skipped", "reason": "registry_differs_from_checkout"},
                     {"role": "api", "check": "execution_will_be_synced",
                      "fields": ["execution.current_adapter_version", "execution.retained_adapter_versions"]},
                     {"role": "worker", "check": "config_matches_code_registry"}]
    # The structural checks still apply to a rollback.
    fake_lambda(FakeLambda(lambda_configurations(worker=lambda document: {**document, "role": "api"})))
    code, lines = run(capsys, *check_argv(registry(PREVIOUS_REGISTRY)))
    assert code == 2 and lines[-1] == {"code": "CONFIG_DRIFT", "role": "worker", "field": "role", "expected": "worker"}


@pytest.mark.parametrize("change", sorted(INVALID_REGISTRIES))
def test_check_config_without_a_usable_registry_stops_before_any_client_is_created(monkeypatch, capsys, tmp_path, change):
    # An artifact built before D140 has no registry file: it cannot be rolled back to automatically.
    def forbidden(region):
        pytest.fail("no client without the artifact registry")
    monkeypatch.setattr(deployer, "lambda_client", forbidden)
    code, lines = run(capsys, *check_argv(invalid_registry(tmp_path, change)))
    assert code == 3 and lines == [{"code": "REGISTRY_MISSING"}]


def test_check_config_aws_failure_is_a_fixed_code_without_the_exception_text(fake_lambda, capsys, registry):
    fake_lambda(FakeLambda({}))
    code, lines = run(capsys, *check_argv(registry(), "api"))
    assert code == 3 and lines == [{"code": "AWS_CALL_FAILED", "role": "api", "call": "GetFunctionConfiguration"}]


@pytest.mark.parametrize("argv", [
    ("check-config", "--region", "US-EAST-2", "--function", "api=" + FUNCTIONS["api"]),
    ("check-config", "--region", REGION, "--function", "calculator=" + FUNCTIONS["api"]),
    ("check-config", "--region", REGION, "--function", "api=arn:aws:lambda:us-east-2:123456789012:function:x"),
    ("check-config", "--region", REGION, "--function", "api=" + FUNCTIONS["api"], "--function", "api=" + FUNCTIONS["api"]),
    ("check-config", "--region", REGION, "--function", FUNCTIONS["api"]),
])
def test_check_config_rejects_invalid_arguments_before_any_client_is_created(monkeypatch, capsys, registry, argv):
    def forbidden(region):
        pytest.fail("no client before argument validation")
    monkeypatch.setattr(deployer, "lambda_client", forbidden)
    code, lines = run(capsys, *argv, "--registry", registry())
    assert code == 3 and lines[0]["code"] == "ARGUMENT_INVALID"


@pytest.mark.parametrize("command", ["check-config", "deploy"])
def test_the_registry_argument_is_required(capsys, command):
    argv = [command, "--region", REGION, "--function", "api=" + FUNCTIONS["api"]]
    if command == "deploy":
        argv += ["--zip", "absent.zip"]
    with pytest.raises(SystemExit) as stopped:
        deployer.main(argv)
    assert stopped.value.code == 2 and "--registry" in capsys.readouterr().err


# --- deploy ---------------------------------------------------------------

ORDER = [FUNCTIONS[role] for role in ("worker", "relay", "api")]


def artifact(tmp_path, *, manifest=True, manifest_digest=None, registry=CODE_REGISTRY):
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
    if registry is not None:
        (output / "execution-registry.json").write_text(json.dumps(registry))
    return output, digest, base64.b64encode(bytes.fromhex(digest)).decode("ascii")


def deploy_argv(output, report, *, manifest=True):
    argv = ["deploy", "--region", REGION, "--zip", str(output / "mock-lambda.zip"),
            "--function", "worker=" + FUNCTIONS["worker"], "--function", "relay=" + FUNCTIONS["relay"],
            "--function", "api=" + FUNCTIONS["api"], "--report", str(report),
            "--registry", str(output / "execution-registry.json")]
    if manifest:
        argv += ["--manifest", str(output / "artifact-manifest.json")]
    return argv


def code_steps(name, digest):
    return [("update", name, digest, False), ("wait", name, deployer.WAITER_CONFIG), ("get", name)]


def sync_steps(name):
    return [("get", name), ("config", name), ("wait", name, deployer.WAITER_CONFIG), ("get", name)]


def assert_no_configuration_value(text):
    for marker in ("fixture-bucket", "fixture-table", "fixture-other-value", "123456789012", "arn:aws",
                   "fixture-revision", "\"schema\"", "payload_limit"):
        assert marker not in text, marker


def test_deploy_updates_worker_relay_api_in_order_waits_and_compares_code_sha(fake_lambda, capsys, tmp_path):
    output, digest, code_sha = artifact(tmp_path)
    client = fake_lambda(FakeLambda(lambda_configurations()))
    report = tmp_path / "deploy-report.json"
    code, lines = run(capsys, *deploy_argv(output, report))
    assert code == 0
    assert [call for call in client.calls if call[0] == "update"] == [("update", name, digest, False) for name in ORDER]
    # The configuration already matches the registry: it is read, never written.
    assert not [call for call in client.calls if call[0] == "config"] and client.config_requests == []
    # Pre-check reads, then per function: (API/Worker: read the configuration) -> update -> wait -> read back.
    assert client.calls[:3] == [("get", name) for name in ORDER]
    assert client.calls[3:] == ([("get", ORDER[0])] + code_steps(ORDER[0], digest) + code_steps(ORDER[1], digest)
                                + [("get", ORDER[2])] + code_steps(ORDER[2], digest))
    assert lines == [{"FunctionName": name, "CodeSha256": code_sha, "LastModified": "2026-09-30T00:00:00.000+0000",
                      "config": state}
                     for name, state in zip(ORDER, ("unchanged", "not_applicable", "unchanged"))]
    saved = json.loads(report.read_text())
    assert saved["status"] == "completed" and saved["zip_sha256"] == digest
    assert [entry["role"] for entry in saved["functions"]] == ["worker", "relay", "api"]
    assert all(entry["code_sha256_matches"] is True for entry in saved["functions"])
    assert saved["config"] == {"worker": "unchanged", "api": "unchanged"}
    assert [entry["config"] for entry in saved["functions"]] == ["unchanged", "not_applicable", "unchanged"]


def spaced_previous_execution(document):
    """The pre-D138 block, in a document stored with its own key order and whitespace (as typed in the console)."""
    document = previous_execution(document)
    document["execution"] = {key: document["execution"][key] for key in
                             ("retained_adapter_versions", "current_adapter_version", "projection_version")}
    reordered = {key: document[key] for key in reversed(list(document))}
    reordered["state"]["fixture_note"] = "보존 ✓"
    return json.dumps(reordered, indent=2, ensure_ascii=False)


def test_deploy_syncs_the_execution_block_right_before_each_code_update(fake_lambda, capsys, tmp_path):
    # D140: Worker [config, code] -> Relay [code] -> API [config, code].
    output, digest, code_sha = artifact(tmp_path)
    configurations = lambda_configurations(worker=spaced_previous_execution, api=spaced_previous_execution)
    before = json.loads(json.dumps(configurations))
    client = fake_lambda(FakeLambda(configurations))
    report = tmp_path / "deploy-report.json"
    code, lines = run(capsys, *deploy_argv(output, report))
    assert code == 0
    assert client.calls[:3] == [("get", name) for name in ORDER]
    assert client.calls[3:] == (sync_steps(ORDER[0]) + code_steps(ORDER[0], digest) + code_steps(ORDER[1], digest)
                                + sync_steps(ORDER[2]) + code_steps(ORDER[2], digest))
    assert [call[:2] for call in client.calls if call[0] in ("config", "update")] == [
        ("config", ORDER[0]), ("update", ORDER[0]), ("update", ORDER[1]), ("config", ORDER[2]), ("update", ORDER[2])]
    assert [request["FunctionName"] for request in client.config_requests] == [ORDER[0], ORDER[2]]
    for role, request in zip(("worker", "api"), client.config_requests):
        name = FUNCTIONS[role]
        old_variables = before[name]["Environment"]["Variables"]
        sent = request["Environment"]["Variables"]
        # Only FunctionName, the whole environment map and the revision guard are sent.
        assert set(request) == {"FunctionName", "Environment", "RevisionId"} and set(request["Environment"]) == {"Variables"}
        assert request["RevisionId"] == "fixture-revision-" + role
        # Every other variable is written back exactly as read, in the same order.
        assert list(sent) == list(old_variables)
        assert {key: value for key, value in sent.items() if key != "ARC_JOURNEY_CONFIG"} == \
            {key: value for key, value in old_variables.items() if key != "ARC_JOURNEY_CONFIG"}
        old_document, new_document = json.loads(old_variables["ARC_JOURNEY_CONFIG"]), json.loads(sent["ARC_JOURNEY_CONFIG"])
        # Only the execution block changed; key order of the document and of the block is kept.
        assert list(new_document) == list(old_document)
        assert {key: value for key, value in new_document.items() if key != "execution"} == \
            {key: value for key, value in old_document.items() if key != "execution"}
        assert new_document["execution"] == CODE_REGISTRY
        assert list(new_document["execution"]) == ["retained_adapter_versions", "current_adapter_version", "projection_version"]
        assert new_document["state"]["fixture_note"] == "보존 ✓" and "보존 ✓" in sent["ARC_JOURNEY_CONFIG"]
        assert sent["ARC_JOURNEY_CONFIG"] == json.dumps(new_document, separators=(",", ":"), ensure_ascii=False)
        assert stored_document(client, role)["execution"] == CODE_REGISTRY
        # The document the new code will read is valid for the code registry.
        AwsSettingsDocument = json.loads(sent["ARC_JOURNEY_CONFIG"])
        del AwsSettingsDocument["state"]["fixture_note"]
        from mock_journey.aws_settings import AwsSettings
        AwsSettings.parse(json.dumps(AwsSettingsDocument), role)
    # The Relay has no execution block and is never written.
    assert configurations[FUNCTIONS["relay"]]["Environment"] == before[FUNCTIONS["relay"]]["Environment"]
    assert lines == [{"FunctionName": name, "CodeSha256": code_sha, "LastModified": "2026-09-30T00:00:00.000+0000",
                      "config": state}
                     for name, state in zip(ORDER, ("synced", "not_applicable", "synced"))]
    saved = json.loads(report.read_text())
    assert saved["status"] == "completed" and saved["config"] == {"worker": "synced", "api": "synced"}
    assert [entry["config"] for entry in saved["functions"]] == ["synced", "not_applicable", "synced"]
    assert_no_configuration_value(report.read_text())


def test_deploy_syncs_only_the_role_that_differs(fake_lambda, capsys, tmp_path):
    output, _, _ = artifact(tmp_path)
    client = fake_lambda(FakeLambda(lambda_configurations(api=previous_execution)))
    report = tmp_path / "deploy-report.json"
    code, lines = run(capsys, *deploy_argv(output, report))
    assert code == 0 and [call[1] for call in client.calls if call[0] == "config"] == [FUNCTIONS["api"]]
    assert [line["config"] for line in lines] == ["unchanged", "not_applicable", "synced"]
    assert json.loads(report.read_text())["config"] == {"worker": "unchanged", "api": "synced"}


def test_deploy_without_a_revision_id_still_sends_only_the_environment(fake_lambda, capsys, tmp_path):
    output, _, _ = artifact(tmp_path)
    configurations = lambda_configurations(worker=previous_execution)
    for configuration_ in configurations.values():
        configuration_["RevisionId"] = None
    client = fake_lambda(FakeLambda(configurations))
    code, _ = run(capsys, *deploy_argv(output, tmp_path / "deploy-report.json"))
    assert code == 0 and [set(request) for request in client.config_requests] == [{"FunctionName", "Environment"}]


def test_rollback_with_the_registry_of_the_earlier_artifact_restores_its_execution_block(fake_lambda, capsys, tmp_path):
    # The functions run the current configuration (v5); the artifact to roll back to carries the v4 registry.
    output, digest, _ = artifact(tmp_path, registry=PREVIOUS_REGISTRY)
    client = fake_lambda(FakeLambda(lambda_configurations()))
    report = tmp_path / "deploy-report.json"
    code, lines = run(capsys, *deploy_argv(output, report))
    assert code == 0
    assert [call[:2] for call in client.calls if call[0] in ("config", "update")] == [
        ("config", ORDER[0]), ("update", ORDER[0]), ("update", ORDER[1]), ("config", ORDER[2]), ("update", ORDER[2])]
    for role in ("worker", "api"):
        document = stored_document(client, role)
        assert document["execution"] == PREVIOUS_REGISTRY
        assert {key: value for key, value in document.items() if key != "execution"} == \
            {key: value for key, value in configuration_document(role).items() if key != "execution"}
    assert json.loads(report.read_text())["config"] == {"worker": "synced", "api": "synced"}
    assert [line["config"] for line in lines] == ["synced", "not_applicable", "synced"]


@pytest.mark.parametrize("code_name", ["AccessDeniedException", "AccessDenied"])
@pytest.mark.parametrize("role", ["worker", "api"])
def test_deploy_without_the_configuration_permission_stops_before_that_code_is_replaced(fake_lambda, capsys, tmp_path,
                                                                                       code_name, role):
    output, _, _ = artifact(tmp_path)
    client = fake_lambda(FakeLambda(
        lambda_configurations(worker=previous_execution, api=previous_execution),
        config_failures={FUNCTIONS[role]: FakeClientError(code_name, DENIED_MESSAGE)}))
    report = tmp_path / "deploy-report.json"
    code, lines = run(capsys, *deploy_argv(output, report))
    assert code == 6
    # Only the fixed code, the missing action and the role: no ARN, account or message text.
    assert lines[-1] == {"code": "CONFIG_SYNC_DENIED", "missing_action": "lambda:UpdateFunctionConfiguration", "role": role}
    updated = [call[1] for call in client.calls if call[0] == "update"]
    assert updated == ([] if role == "worker" else [FUNCTIONS["worker"], FUNCTIONS["relay"]])
    assert FUNCTIONS[role] not in updated
    saved = json.loads(report.read_text())
    assert saved["status"] == "failed" and saved["failed_role"] == role and saved["code"] == "CONFIG_SYNC_DENIED"
    assert saved["config"][role] == "unchanged"
    assert [entry["role"] for entry in saved["functions"]] == ([] if role == "worker" else ["worker", "relay"])
    assert_no_configuration_value(report.read_text())


def test_botocore_client_error_is_recognised_as_access_denied(fake_lambda, capsys, tmp_path):
    from botocore.exceptions import ClientError

    output, _, _ = artifact(tmp_path)
    denied = ClientError({"Error": {"Code": "AccessDeniedException", "Message": DENIED_MESSAGE}}, "UpdateFunctionConfiguration")
    client = fake_lambda(FakeLambda(lambda_configurations(worker=previous_execution),
                                    config_failures={FUNCTIONS["worker"]: denied}))
    code, lines = run(capsys, *deploy_argv(output, tmp_path / "deploy-report.json"))
    assert code == 6 and lines == [{"code": "CONFIG_SYNC_DENIED", "missing_action": "lambda:UpdateFunctionConfiguration",
                                    "role": "worker"}]
    assert not [call for call in client.calls if call[0] == "update"]


@pytest.mark.parametrize("failure,state", [("call", "unconfirmed"), ("waiter", "unconfirmed"), ("read_back", "unconfirmed")])
def test_deploy_stops_when_the_configuration_sync_fails_in_the_middle(fake_lambda, capsys, tmp_path, failure, state):
    # Worker and Relay are done; the API sync fails, so the API code is not replaced.
    output, digest, _ = artifact(tmp_path)
    api = FUNCTIONS["api"]
    kwargs = {"call": {"config_failures": {api: FakeClientError("ResourceConflictException", DENIED_MESSAGE)}},
              "waiter": {}, "read_back": {"config_ignored": {api}}}[failure]
    client = fake_lambda(FakeLambda(lambda_configurations(worker=previous_execution, api=previous_execution), **kwargs))
    if failure == "waiter":
        original = client.update_function_configuration

        def update_then_break_the_waiter(**request):
            original(**request)
            if request["FunctionName"] == api:
                client.waiter_failures[api] = True
        client.update_function_configuration = update_then_break_the_waiter
    report = tmp_path / "deploy-report.json"
    code, lines = run(capsys, *deploy_argv(output, report))
    assert code == 3
    expected = ({"code": "CONFIG_SYNC_FAILED", "role": "api", "reason": "read_back_differs"} if failure == "read_back"
                else {"code": "AWS_CALL_FAILED", "role": "api", "call": "UpdateFunctionConfiguration"})
    assert lines[-1] == {**expected, "updated": ["worker", "relay"], "config_synced": ["worker"]}
    assert [call[1] for call in client.calls if call[0] == "update"] == [FUNCTIONS["worker"], FUNCTIONS["relay"]]
    saved = json.loads(report.read_text())
    assert saved["status"] == "failed" and saved["failed_role"] == "api" and saved["code"] == expected["code"]
    assert saved["config"] == {"worker": "synced", "api": state}
    assert [entry["role"] for entry in saved["functions"]] == ["worker", "relay"]
    assert_no_configuration_value(report.read_text())


def test_deploy_reports_a_synced_configuration_whose_code_update_failed(fake_lambda, capsys, tmp_path):
    # The Worker configuration is already v5 when its code upload fails: the report says so for the re-run.
    output, _, _ = artifact(tmp_path)
    client = fake_lambda(FakeLambda(lambda_configurations(worker=previous_execution),
                                    update_failures={FUNCTIONS["worker"]: True}))
    report = tmp_path / "deploy-report.json"
    code, lines = run(capsys, *deploy_argv(output, report))
    assert code == 3
    assert lines[-1] == {"code": "AWS_CALL_FAILED", "role": "worker", "call": "UpdateFunctionCode", "updated": [],
                         "config_synced": ["worker"]}
    saved = json.loads(report.read_text())
    assert saved["config"] == {"worker": "synced"} and saved["functions"] == [] and saved["failed_role"] == "worker"
    assert stored_document(client, "worker")["execution"] == CODE_REGISTRY


@pytest.mark.parametrize("mutate,field,expected", [
    (lambda document: {**document, "role": "worker"}, "role", "api"),
    (lambda document: "{not json", "ARC_JOURNEY_CONFIG", "json_object"),
    (lambda document: {k: v for k, v in document.items() if k != "execution"}, "execution", "object"),
    (lambda document: "", "ARC_JOURNEY_CONFIG", "present"),
], ids=["role", "not_json", "no_execution", "empty"])
def test_deploy_stops_on_a_difference_outside_the_execution_block_before_any_write(fake_lambda, capsys, tmp_path,
                                                                                  mutate, field, expected):
    # The API is deployed last, but its drift stops the run before the Worker is touched.
    output, _, _ = artifact(tmp_path)
    client = fake_lambda(FakeLambda(lambda_configurations(worker=previous_execution, api=mutate)))
    report = tmp_path / "deploy-report.json"
    code, lines = run(capsys, *deploy_argv(output, report))
    assert code == 2 and lines == [{"code": "CONFIG_DRIFT", "role": "api", "field": field, "expected": expected}]
    assert [call[0] for call in client.calls] == ["get", "get", "get"]
    saved = json.loads(report.read_text())
    assert saved["status"] == "failed" and saved["failed_role"] == "api" and saved["code"] == "CONFIG_DRIFT"
    assert saved["functions"] == [] and saved["config"] == {}


def test_deploy_rechecks_the_configuration_right_before_the_sync(fake_lambda, capsys, tmp_path):
    # The API document changes role after the pre-check (someone edits it meanwhile): the API is not written.
    output, _, _ = artifact(tmp_path)
    configurations = lambda_configurations(api=previous_execution)
    client = fake_lambda(FakeLambda(configurations))
    original = client.update_function_code

    def update_then_edit(FunctionName, ZipFile, Publish):
        original(FunctionName=FunctionName, ZipFile=ZipFile, Publish=Publish)
        if FunctionName == FUNCTIONS["relay"]:
            variables = configurations[FUNCTIONS["api"]]["Environment"]["Variables"]
            variables["ARC_JOURNEY_CONFIG"] = json.dumps({**json.loads(variables["ARC_JOURNEY_CONFIG"]), "role": "worker"})
    client.update_function_code = update_then_edit
    report = tmp_path / "deploy-report.json"
    code, lines = run(capsys, *deploy_argv(output, report))
    assert code == 2
    assert lines[-1] == {"code": "CONFIG_DRIFT", "role": "api", "field": "role", "expected": "api",
                         "updated": ["worker", "relay"]}
    assert not [call for call in client.calls if call[0] == "config"]
    assert json.loads(report.read_text())["config"] == {"worker": "unchanged", "api": "unchanged"}


@pytest.mark.parametrize("change", sorted(INVALID_REGISTRIES))
def test_deploy_without_a_usable_registry_stops_before_any_aws_call(fake_lambda, capsys, tmp_path, change):
    output, _, _ = artifact(tmp_path, registry=None)
    if INVALID_REGISTRIES[change] is not None:
        (output / "execution-registry.json").write_text(INVALID_REGISTRIES[change], encoding="utf-8")
    client = fake_lambda(FakeLambda(lambda_configurations()))
    code, lines = run(capsys, *deploy_argv(output, tmp_path / "deploy-report.json"))
    assert code == 3 and lines == [{"code": "REGISTRY_MISSING"}]
    assert client.calls == []


def test_exit_codes_are_fixed():
    assert deployer.EXIT_CODES == {"CONFIG_DRIFT": 2, "REGISTRY_MISSING": 3, "CONFIG_SYNC_FAILED": 3,
                                   "CODE_SHA_MISMATCH": 4, "SMOKE_FAILED": 5, "CONFIG_SYNC_DENIED": 6}
    assert deployer.DeployError("AWS_CALL_FAILED").exit_code == 3


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
    assert FUNCTIONS["api"] not in [call[1] for call in client.calls if call[0] in ("update", "config")]
    saved = json.loads(report.read_text())
    assert saved["status"] == "failed" and [entry["role"] for entry in saved["functions"]] == ["worker"]


@pytest.mark.parametrize("change", ["runtime", "package_type", "missing"])
def test_deploy_refuses_an_unsupported_target_before_the_first_upload(fake_lambda, capsys, tmp_path, change):
    output, _, _ = artifact(tmp_path)
    configurations = lambda_configurations(worker=previous_execution)
    if change == "runtime":
        configurations[FUNCTIONS["api"]]["Runtime"] = "python3.11"
    elif change == "package_type":
        configurations[FUNCTIONS["api"]]["PackageType"] = "Image"
    else:
        del configurations[FUNCTIONS["api"]]
    client = fake_lambda(FakeLambda(configurations))
    code, lines = run(capsys, *deploy_argv(output, tmp_path / "deploy-report.json"))
    assert code == 3 and lines[-1]["code"] in ("FUNCTION_UNSUPPORTED", "AWS_CALL_FAILED") and lines[-1]["role"] == "api"
    assert not [call for call in client.calls if call[0] in ("update", "config")]


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
    deploy_report.write_text(json.dumps({"status": "completed", "zip_sha256": digest,
                                         "config": {"worker": "synced", "api": "fixture-other-value"}, "functions": [
        {"role": "worker", "FunctionName": FUNCTIONS["worker"], "CodeSha256": code_sha, "config": "synced",
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
    assert text.startswith("existing\n## Dev deployment (D137, D140)")
    assert "a" * 40 in text and digest in text and code_sha in text and FUNCTIONS["worker"] in text
    # D140: the state of the execution block per role; an unknown state is never echoed.
    assert "- ARC_JOURNEY_CONFIG execution block: api -, worker synced" in text and "fixture-other-value" not in text
    assert "| role | function | config | CodeSha256 | LastModified | matches ZIP |" in text
    assert f"| worker | {FUNCTIONS['worker']} | synced | {code_sha} |" in text
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
    for subcommand in ("registry", "check-config", "deploy", "smoke", "summary"):
        assert f"scripts/deploy_dev_lambdas.py {subcommand}" in text
    parser = deployer.build_parser()
    assert set(parser._subparsers._group_actions[0].choices) == {"registry", "check-config", "deploy", "smoke", "summary"}
    # D140: the registry is written into the artifact folder and both AWS steps read that same file.
    assert text.count('"$RUNNER_TEMP/artifact/execution-registry.json"') == 3
    assert "${{ runner.temp }}/artifact/execution-registry.json" in text


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


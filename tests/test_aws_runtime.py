"""AWS composition with synthetic settings/SDKs; no real AWS destinations used."""

import base64
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest

from mock_journey.aws_runtime import build_runtime
from mock_journey.aws_settings import AwsSettings
from mock_journey.contracts import PENDING_GOAL_ADAPTER_VERSION, RETAINED_PENDING_GOAL_ADAPTER_VERSION
from mock_journey.errors import JourneyError
from mock_journey.execution_definitions import PROJECTION_VERSION
from tests.mock_storage_support import MemoryS3


def configuration(role="api"):
    sdk = {"connect_timeout": 0.1, "read_timeout": 0.1, "total_max_attempts": 1, "retry_mode": "standard"}
    result = {"schema": 1, "role": role, "account_id": "123456789012", "partition": "aws",
              "environment": "synthetic-dev", "region": "us-east-1",
              "state": {"table_name": "synthetic-journey", "max_conflict_retries": 4}, "sdk": sdk,
              "logs": {"capacity": 20, "max_bytes": 16384, "flush_budget_ms": 40,
                       "response_reserve_ms": 20, "sdk": deepcopy(sdk)}}
    if role != "relay":
        result.update(storage={"stage": "dev", "bucket": "synthetic-private-bucket", "directory": "calculator_result/arc",
                               "input_bytes": 1000000, "artifact_bytes": 8000000},
                      execution={"current_adapter_version": PENDING_GOAL_ADAPTER_VERSION,
                                 "projection_version": PROJECTION_VERSION,
                                 "retained_adapter_versions": [RETAINED_PENDING_GOAL_ADAPTER_VERSION]})
    result[role] = ({"payload_limit": 1400000} if role == "api" else
                    {"lease_seconds": 1, "retry_seconds": 1, "renewal_interval_seconds": 0.05,
                     "renewal_timeout_seconds": 0.1, "processing_reserve_ms": 500} if role == "worker" else
                    {"queue_url": "https://sqs.us-east-1.amazonaws.com/123456789012/synthetic-jobs",
                     "lease_seconds": 10, "retry_seconds": 1, "page_size": 10, "max_pages": 2,
                     "processing_reserve_ms": 500})
    return result


def environment(role="api", config=None):
    value = configuration(role) if config is None else config
    result = {"ARC_MOCK_ENABLED": "true", "ARC_JOURNEY_CONFIG": json.dumps(value)}
    if role == "api":
        result.update(ARC_MOCK_RESUME_KEYS=json.dumps({"v1": base64.b64encode(b"K" * 32).decode()}),
                      ARC_MOCK_RESUME_KEY_VERSION="v1")
    return result


def context(remaining=10000):
    return SimpleNamespace(aws_request_id=str(uuid.uuid4()), get_remaining_time_in_millis=lambda: remaining,
                           invoked_function_arn="arn:aws:lambda:us-east-1:123456789012:function:synthetic")


class FakeSdk:
    def __init__(self, s3=None):
        self.calls, self.closed, self.logs = [], [], []
        self.s3 = s3 or MemoryS3()

    def __call__(self, service, **kwargs):
        self.calls.append((service, kwargs))
        if service == "s3":
            return self.s3
        return SimpleNamespace(close=lambda: self.closed.append(service),
                               put_item=lambda **args: self.logs.append(args),
                               send_message=lambda **args: {"MessageId": "synthetic"})


@pytest.mark.parametrize("role,services", [("api", ["dynamodb", "s3"]), ("worker", ["dynamodb", "s3"]),
                                            ("relay", ["dynamodb", "sqs"])])
def test_real_role_assembly_creates_only_role_clients_and_no_initial_requests(role, services):
    factory = FakeSdk()
    runtime = build_runtime(role, environment(role), client_factory=factory)
    assert [name for name, _ in factory.calls] == services
    assert factory.logs == []
    for _, kwargs in factory.calls:
        cfg = kwargs["config"]
        assert kwargs["region_name"] == "us-east-1"
        assert cfg.connect_timeout == cfg.read_timeout == 0.1
        assert cfg.retries == {"mode": "standard", "total_max_attempts": 1}
        assert cfg.ignore_configured_endpoint_urls is True
    if role == "api":
        assert runtime.target.calculation is not None
        assert runtime.target.auth.keys == {"v1": b"K" * 32}
    else:
        assert not hasattr(runtime.target, "auth")
    if role == "worker":
        registry = runtime.target.adapters
        assert registry.resolve(PENDING_GOAL_ADAPTER_VERSION, PROJECTION_VERSION).can_calculate
        assert not registry.resolve(RETAINED_PENDING_GOAL_ADAPTER_VERSION, PROJECTION_VERSION).can_calculate


@pytest.mark.parametrize("change", ["missing", "unknown", "boolean", "nan", "role", "duplicate", "key", "version",
                                      "retained", "partition", "stage", "bucket", "sdk", "logs"])
def test_invalid_configuration_is_sanitized_before_client_creation(change):
    config = configuration()
    env = environment(config=config)
    if change == "missing":
        config.pop("logs")
    elif change == "unknown":
        config["PRIVATE-MARKER"] = "PRIVATE-MARKER"
    elif change == "boolean":
        config["api"]["payload_limit"] = True
    elif change == "nan":
        config["sdk"]["connect_timeout"] = float("nan")
    elif change == "role":
        config["role"] = "worker"
    elif change == "version":
        config["execution"]["current_adapter_version"] = "unreviewed"
    elif change == "retained":
        config["execution"]["retained_adapter_versions"] = ["unreviewed"]
    elif change == "partition":
        config["partition"] = "aws-cn"
    elif change == "stage":
        config["storage"]["stage"] = "test"
    elif change == "bucket":
        config["storage"]["bucket"] = "PRIVATE-MARKER"
    elif change == "sdk":
        config["sdk"]["total_max_attempts"] = False
    elif change == "logs":
        config["logs"]["capacity"] = 0
    env["ARC_JOURNEY_CONFIG"] = json.dumps(config)
    if change == "duplicate":
        env["ARC_JOURNEY_CONFIG"] = '{"schema":1,"schema":1}'
    if change == "key":
        env["ARC_MOCK_RESUME_KEYS"] = '{"v1":"PRIVATE-MARKER"}'
    calls = []
    with pytest.raises(JourneyError) as error:
        build_runtime("api", env, client_factory=lambda *a, **k: calls.append(a))
    assert not calls and error.value.code == "TEMPORARILY_UNAVAILABLE"
    assert "PRIVATE-MARKER" not in str(error.value)
    assert error.value.__suppress_context__


@pytest.mark.parametrize("key,value", [("AWS_REGION", "eu-west-1"), ("STAGE", "prod"),
                                        ("ARC_MOCK_TABLE_NAME", "other"),
                                        ("AWS_ENDPOINT_URL", "https://PRIVATE-MARKER.invalid"),
                                        ("AWS_ENDPOINT_URL_S3", "https://PRIVATE-MARKER.invalid")])
def test_environment_binding_mismatch_and_endpoint_override_rejected(key, value):
    env = {**environment(), key: value}
    calls = []
    with pytest.raises(JourneyError):
        build_runtime("api", env, client_factory=lambda *a, **k: calls.append(a))
    assert calls == []


@pytest.mark.parametrize("url", ["https://sqs.us-east-1.amazonaws.com/123456789012/jobs.fifo",
                                  "https://sqs.us-east-1.amazonaws.com/111111111111/jobs",
                                  "https://sqs.eu-west-1.amazonaws.com/123456789012/jobs",
                                  "https://sqs.us-east-1.amazonaws.com/123456789012/jobs?secret=PRIVATE-MARKER"])
def test_relay_standard_queue_account_region_binding(url):
    config = configuration("relay")
    config["relay"]["queue_url"] = url
    with pytest.raises(ValueError):
        AwsSettings.parse(json.dumps(config), "relay")


def test_failed_client_creation_closes_earlier_client_and_does_not_echo_error():
    closed = []
    def factory(service, **kwargs):
        if service == "s3":
            raise RuntimeError("PRIVATE-MARKER")
        return SimpleNamespace(close=lambda: closed.append(service))
    with pytest.raises(JourneyError) as error:
        build_runtime("api", environment(), client_factory=factory)
    assert closed == ["dynamodb"] and "PRIVATE-MARKER" not in str(error.value)
    assert build_runtime("api", environment(), client_factory=FakeSdk()).target.calculation


def test_context_resource_mismatch_is_rejected_before_invocation():
    runtime = build_runtime("api", environment(), client_factory=FakeSdk())
    wrong = context()
    wrong.invoked_function_arn = "arn:aws:lambda:us-east-1:111111111111:function:other"
    with pytest.raises(JourneyError):
        with runtime.invocation(wrong):
            pytest.fail("Mismatched invocation ran")


def test_offline_validator_reports_only_status_without_reading_secrets_or_creating_clients(tmp_path, capsys):
    from mock_journey.aws_settings import main
    path = tmp_path / "private-config.json"
    path.write_text(json.dumps(configuration()), encoding="utf-8")
    assert main(["--role", "api", "--config", str(path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result == {"status": "configuration_valid", "role": "api", "aws_access_checked": False, "secrets_checked": False}
    path.write_text('{"PRIVATE-MARKER":null}', encoding="utf-8")
    assert main(["--role", "api", "--config", str(path)]) == 2
    assert "PRIVATE-MARKER" not in capsys.readouterr().out


def test_public_and_worker_runtime_errors_are_sanitized(monkeypatch, capsys):
    import mock_journey.runtime as api_runtime
    import mock_journey.worker_runtime as worker_runtime
    from mock_journey.handler import run as api_run
    from mock_journey.worker import run as worker_run
    def failed():
        raise RuntimeError("PRIVATE-MARKER")
    monkeypatch.setattr(api_runtime, "get_application", failed)
    monkeypatch.setattr(worker_runtime, "get_worker", failed)
    response = api_run({}, context())
    assert response["statusCode"] == 503 and "PRIVATE-MARKER" not in response["body"]
    with pytest.raises(JourneyError) as error:
        worker_run({"Records": []}, context())
    assert error.value.code == "TEMPORARILY_UNAVAILABLE" and "PRIVATE-MARKER" not in str(error.value)
    assert "PRIVATE-MARKER" not in capsys.readouterr().out


def test_public_handler_invocation_logs_cannot_change_a_success_response(monkeypatch):
    from mock_journey.handler import run
    from mock_journey.aws_logs import InvocationLogs
    import mock_journey.runtime as module
    runtime = build_runtime("api", environment(), client_factory=FakeSdk())
    def failed():
        raise RuntimeError("PRIVATE-MARKER")
    operations = InvocationLogs(failed, role="api", settings=runtime.settings.logs, warning=lambda _: None)
    runtime.operations = runtime.target.operations = operations
    def login(body):
        from services.operational_logs import record_event
        record_event("login_succeeded")
        return {"created": True}
    runtime.target.login = login
    monkeypatch.setattr(module, "get_application", lambda: runtime.target)
    response = run({"httpMethod": "POST", "path": "/mock/v1/sessions", "body": "{}"}, context())
    assert response["statusCode"] == 201
    assert operations.status()["unconfirmed"] == 1


def test_runtime_cache_reuses_only_fully_initialized_clients(monkeypatch):
    import mock_journey.aws_runtime as module
    monkeypatch.setattr(module, "_runtimes", {})
    runtime = build_runtime("relay", environment("relay"), client_factory=FakeSdk())
    attempts = []
    def create(*args):
        attempts.append(True)
        if len(attempts) == 1:
            raise JourneyError("TEMPORARILY_UNAVAILABLE")
        return runtime
    monkeypatch.setattr(module, "build_runtime", create)
    with pytest.raises(JourneyError):
        module.get_runtime("relay")
    assert module.get_runtime("relay") is module.get_runtime("relay") is runtime
    assert len(attempts) == 2


def test_worker_admission_defers_remaining_batch_without_executing_it():
    from mock_journey.worker import handle
    calls = []
    times = iter((1000, 100))
    ctx = context()
    ctx.get_remaining_time_in_millis = lambda: next(times)
    worker = SimpleNamespace(processing_reserve_ms=500, process=lambda ident: calls.append(ident) or True)
    ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    event = {"Records": [{"messageId": str(i), "body": json.dumps({"job_id": ident})} for i, ident in enumerate(ids)]}
    assert handle(event, ctx, worker) == {"batchItemFailures": [{"itemIdentifier": "1"}]}
    assert calls == ids[:1]


def test_composed_private_storage_and_real_core_share_the_injected_s3_only():
    """Transport/binding consistency, not an independent scoring oracle or AWS proof."""
    from mock_journey.projection import project_input, typed_identity
    from mock_journey import typed
    factory = FakeSdk()
    api = build_runtime("api", environment(), client_factory=factory).target
    worker = build_runtime("worker", environment("worker"), client_factory=factory).target
    definition = typed.parse_json(api.catalog.definition("mock-compression-only", "adult"))
    body = {"condition": definition["condition"], "vp_event_list": [], "aed_b64_data": b"",
            "cpr_b64_data": (Path(__file__).parent / "dataset/cco_1.bin").read_bytes()}
    projected = project_input(body, definition, api.calculation.schemas[PROJECTION_VERSION])
    binding = {"attempt_id": str(uuid.uuid4()), "epoch": str(uuid.uuid4()), "input_digest": typed_identity(projected),
               "adapter_version": PENDING_GOAL_ADAPTER_VERSION, "projection_version": PROJECTION_VERSION}
    stored = api.calculation.storage.save_input(projected, binding)
    loaded = worker.storage.load_input(stored["manifest_ref"], binding)
    call = {**binding, "job_id": str(uuid.uuid4()), "call_id": str(uuid.uuid4())}
    adapter = worker.adapters.resolve(PENDING_GOAL_ADAPTER_VERSION, PROJECTION_VERSION)
    raw = adapter.calculate(loaded, call, lambda: None)
    verified = adapter.validate_response(raw, projected, call)
    chart = adapter.get_chart(verified, call, lambda: None)
    selected = {**worker.storage.save_chart_candidate(chart.data, chart.source_sha256, call, 1), "revision": 1}
    publication = worker.storage.publish_selected_chart(call["job_id"], loaded.raw_base, call, lambda _: selected)
    assert worker.storage.sign_chart(publication)
    assert verified.observed > 0
    assert {bucket for bucket, key in factory.s3.objects} == {"synthetic-private-bucket"}
    assert all(key.startswith("calculator_result/arc/dev/") for bucket, key in factory.s3.objects)
    assert factory.s3.sign_calls[-1][2] == 300

"""AWS composition with synthetic settings/SDKs; no real AWS destinations used."""

import json
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest

from mock_journey.aws_runtime import build_runtime
from mock_journey.aws_settings import AwsSettings
from mock_journey.contracts import (
    CURRENT_ADAPTER_VERSION, PENDING_GOAL_ADAPTER_VERSION, RETAINED_PENDING_GOAL_ADAPTER_VERSION,
)
from mock_journey.errors import JourneyError
from mock_journey.execution_definitions import PROJECTION_VERSION
from tests.aws_runtime_support import FakeSdk, configuration, context, course_section, environment  # noqa: F401 (re-export)


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
        # The API role is always the course_v2 application; there is no
        # course-less /mock/v1 composition left to fall back to.
        assert runtime.target.course_mode == "course_v2"
        assert runtime.target.course_http is not None
        assert runtime.target.provider.learner.is_dummy is True
        assert runtime.target.calculation is not None
        assert runtime.target.auth.keys == {"v1": b"K" * 32}
    else:
        assert not hasattr(runtime.target, "auth")
        assert not hasattr(runtime.target, "course_http")
        assert not hasattr(runtime.target, "provider")
    if role == "worker":
        registry = runtime.target.adapters
        assert registry.resolve(CURRENT_ADAPTER_VERSION, PROJECTION_VERSION).can_calculate
        # D136: the retained pending-v3 adapter still calculates in-flight attempts.
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


@pytest.mark.parametrize("shape", ["missing", "null"])
@pytest.mark.parametrize("role", ["api", "worker"])
def test_api_and_worker_without_course_are_rejected_before_any_client(role, shape, tmp_path, capsys):
    """API/Worker serve only course_v2: a missing course section is a configuration error."""
    config = configuration(role)
    if shape == "missing":
        config.pop("course")
    else:
        config["course"] = None
    with pytest.raises(ValueError) as parsed:
        AwsSettings.parse(json.dumps(config), role)
    assert str(parsed.value) == "Invalid explicit AWS journey configuration."
    assert parsed.value.__cause__ is None
    calls = []
    with pytest.raises(JourneyError) as error:
        build_runtime(role, environment(role, config), client_factory=lambda *a, **k: calls.append(a))
    assert calls == [] and error.value.code == "TEMPORARILY_UNAVAILABLE"
    assert error.value.__suppress_context__
    from mock_journey.aws_settings import main
    path = tmp_path / "private-config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    assert main(["--role", role, "--config", str(path)]) == 2
    assert json.loads(capsys.readouterr().out) == {"status": "configuration_invalid", "aws_access_checked": False}
    config["course"] = course_section()
    path.write_text(json.dumps(config), encoding="utf-8")
    assert main(["--role", role, "--config", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "configuration_valid"


@pytest.mark.parametrize("with_course", [False, True])
@pytest.mark.parametrize("stage", ["beta", "production"])
@pytest.mark.parametrize("role", ["api", "worker"])
def test_beta_and_production_api_worker_fail_closed_with_or_without_course(role, stage, with_course):
    """Until G-RELEASE there is no non-Dev course provider: neither shape may assemble."""
    config = configuration(role)
    config["storage"]["stage"] = stage
    if not with_course:
        config.pop("course")
    with pytest.raises(ValueError) as parsed:
        AwsSettings.parse(json.dumps(config), role)
    assert str(parsed.value) == "Invalid explicit AWS journey configuration."
    assert parsed.value.__cause__ is None
    calls = []
    with pytest.raises(JourneyError) as error:
        build_runtime(role, environment(role, config), client_factory=lambda *a, **k: calls.append(a))
    assert calls == [] and error.value.code == "TEMPORARILY_UNAVAILABLE"


def test_relay_is_assembled_without_course_and_still_rejects_one(tmp_path, capsys):
    config = configuration("relay")
    assert "course" not in config
    assert AwsSettings.parse(json.dumps(config), "relay").course is None
    factory = FakeSdk()
    runtime = build_runtime("relay", environment("relay", config), client_factory=factory)
    assert [name for name, _ in factory.calls] == ["dynamodb", "sqs"]
    assert not hasattr(runtime.target, "course_http")
    config["course"] = course_section()
    with pytest.raises(ValueError):
        AwsSettings.parse(json.dumps(config), "relay")
    calls = []
    with pytest.raises(JourneyError):
        build_runtime("relay", environment("relay", config), client_factory=lambda *a, **k: calls.append(a))
    assert calls == []


def test_dummy_course_provider_is_constructed_for_the_api_role_only(monkeypatch):
    """Worker validates the course section offline but never builds a serving provider."""
    import mock_journey.dev_course as dev_course
    original = dev_course.DummyDevCourseProvider
    built = []

    class Counting(original):
        def __init__(self, *args, **kwargs):
            built.append(True)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(dev_course, "DummyDevCourseProvider", Counting)
    counts = {}
    for role in ("api", "worker", "relay"):
        built.clear()
        settings_only = AwsSettings.parse(json.dumps(configuration(role)), role)
        parsed = len(built)
        built.clear()
        runtime = build_runtime(role, environment(role), client_factory=FakeSdk())
        counts[role] = len(built) - parsed
        if role == "api":
            assert type(runtime.target.provider) is Counting
            assert runtime.target.provider.learner.is_dummy is True
        assert settings_only.course == runtime.settings.course
    # Parsing already validates the complete catalog for API and Worker;
    # only the API adds the one provider it serves requests with.
    assert counts == {"api": 1, "worker": 0, "relay": 0}


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
    """A failing invocation-log writer never changes a real v2 login or rejection response.

    The API is the real AWS composition (course_v2 HTTP, state, auth) on the
    test-only in-memory DynamoDB table; only the log store fails.
    """
    from mock_journey.handler import run
    from mock_journey.aws_logs import InvocationLogs
    import mock_journey.runtime as module
    from tests.journey_support import DUMMY_LOGIN, JourneyStore
    store = JourneyStore.memory()
    sdk = FakeSdk()

    def factory(service, **kwargs):
        sdk(service, **kwargs)
        return store.client if service == "dynamodb" else sdk.s3

    config = configuration()
    config["state"]["table_name"] = store.table
    runtime = build_runtime("api", environment(config=config), client_factory=factory)
    def failed():
        raise RuntimeError("PRIVATE-MARKER")
    operations = InvocationLogs(failed, role="api", settings=runtime.settings.logs, warning=lambda _: None)
    runtime.operations = runtime.target.operations = operations
    monkeypatch.setattr(module, "get_application", lambda: runtime.target)
    response = run({"httpMethod": "POST", "path": "/api/v2/sessions/", "headers": {"Content-Type": "application/json"},
                    "body": json.dumps(DUMMY_LOGIN)}, context())
    assert response["statusCode"] == 201, response["body"]
    body = json.loads(response["body"])
    assert body["success"] is True and body["data"]["accessToken"]
    assert "PRIVATE-MARKER" not in response["body"]
    assert operations.status()["unconfirmed"] == 1
    # The committed session is the real one the response announced.
    token = body["data"]["accessToken"]
    assert runtime.target.auth.authenticate(token).session_id == body["data"]["sessionId"]
    rejected = run({"httpMethod": "POST", "path": "/api/v2/sessions/", "headers": {"Content-Type": "application/json"},
                    "body": "{}"}, context())
    assert rejected["statusCode"] == 400
    assert json.loads(rejected["body"])["error"]["code"] == "INVALID_REQUEST"
    assert "PRIVATE-MARKER" not in rejected["body"]
    # status() describes the latest invocation: its one login_failed record.
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
    # The execution definition the AWS API actually serves for this Dummy course.
    definition = typed.parse_json(json.dumps(next(
        row["execution"] for row in api.provider.mapping_document["mappings"].values()
        if (row["program_id"], row["target"]) == ("mock-compression-only", "adult"))))
    body = {"condition": definition["condition"], "vp_event_list": [], "aed_b64_data": b"",
            "cpr_b64_data": (Path(__file__).parent / "dataset/cco_1.bin").read_bytes()}
    projected = project_input(body, definition, api.calculation.schemas[PROJECTION_VERSION])
    binding = {"attempt_id": str(uuid.uuid4()), "epoch": str(uuid.uuid4()), "input_digest": typed_identity(projected),
               "adapter_version": CURRENT_ADAPTER_VERSION, "projection_version": PROJECTION_VERSION}
    stored = api.calculation.storage.save_input(projected, binding)
    loaded = worker.storage.load_input(stored["manifest_ref"], binding)
    call = {**binding, "job_id": str(uuid.uuid4()), "call_id": str(uuid.uuid4())}
    adapter = worker.adapters.resolve(CURRENT_ADAPTER_VERSION, PROJECTION_VERSION)
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

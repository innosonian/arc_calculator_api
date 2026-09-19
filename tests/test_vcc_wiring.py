"""W5 wiring: assembly, course_v2 dispatch, optional legacy fields, G-ARC send 0."""

from types import SimpleNamespace
from datetime import datetime, timezone
import json
import socket
from pathlib import Path

import pytest

from mock_journey.assembly import ExecutionCatalog, build_application, build_course_application, build_relay, build_worker
from mock_journey.catalog import Catalog, PROGRAMS, TARGETS, slot_key
from mock_journey.course_contracts import APP_ROUTES, CONTRACT_VERSION, POLICY_VERSION, CourseBinding
from mock_journey.course_errors import CourseError
from mock_journey.course_http import CourseHttp
from mock_journey.course_provider import UnavailableCourseProvider
from mock_journey.course_settings import fixture_course_settings
from mock_journey.course_state import DynamoCourseRepository
from mock_journey.course_submission import DisabledArcGateway, CourseCompletionPlan
from mock_journey.course_wiring import COURSE_MODE, CourseApplication, bind_course_http
from mock_journey.errors import JourneyError
from mock_journey.handler import handle
from mock_journey.jobs import DynamoJobRepository
from mock_journey.projection import ProjectionSchema
from mock_journey.settings import StateSettings, StorageSettings, ApiSettings, WorkerSettings, RelaySettings
from mock_journey.state import DynamoStateRepository
from mock_journey.typed import digest
from tests.mock_storage_support import MemoryLegacyBindings, MemoryS3
from tests.test_mock_state import AUTH, DEFINITION, SESSION, TEMPLATE, USER, attempt, item, repository, snapshot
from tests.vcc_support import OLD_ROUTES, dummy_learner, event, fixture_hash, load_fixture, mapping_document


ROOT = Path(__file__).resolve().parents[1]
CONTEXT = SimpleNamespace(aws_request_id="vcc-wire-request")
CLOCK = lambda: 1_800_000_000


def calculation_http(calculation, read_attempt):
    journey = SimpleNamespace(
        require_calculation=lambda: calculation,
        auth=SimpleNamespace(authenticate=lambda *a, **k: AUTH),
        state=SimpleNamespace(get_attempt=read_attempt),
    )
    return bind_course_http(
        journey, SimpleNamespace(), fixture_course_settings(), clock=CLOCK,
        uuid_factory=lambda: "61000000-0000-4000-8000-000000000001",
        provider=None, repository=None,
    )


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_pending_calculation_read_is_not_promoted_by_later_completion(method):
    attempt_id = "61000000-0000-4000-8000-000000000002"
    pending = {"attempt_id": attempt_id, "state": "queued", "wait_expired": False,
               "status_path": f"/mock/v1/attempts/{attempt_id}"}
    later = {"attempt_id": attempt_id, "state": "evaluated", "evaluation": {},
             "progress_application": {"applied": True}}
    calculation = SimpleNamespace(result=lambda *a: (202, pending), submit=lambda *a: (202, pending))
    http = calculation_http(calculation, lambda *a: later)
    response = http.dispatch(event(method, f"/api/v2/attempts/{attempt_id}/calculation/", token="test"))
    assert response["statusCode"] == 202
    data = json.loads(response["body"])["data"]
    assert data["calculationStatus"] == "pending"
    assert data["calculation"] is None
    assert data["evaluation"] is None
    assert data["progressApplication"] is None


@pytest.mark.parametrize("url,expires,status", [
    (None, None, 200),
    (None, "2027-01-15T08:05:00Z", 503),
    ("https://example.invalid/chart", None, 503),
])
def test_chart_absence_is_a_verified_pair_not_a_storage_failure(url, expires, status):
    attempt_id = "61000000-0000-4000-8000-000000000002"
    calculation = SimpleNamespace(chart_link=lambda *a: {"chart_dataset_url": url, "expires_at": expires})
    http = calculation_http(calculation, lambda *a: pytest.fail("unexpected attempt read"))
    response = http.dispatch(event("GET", f"/api/v2/attempts/{attempt_id}/chart-link/", token="test"))
    assert response["statusCode"] == status
    body = json.loads(response["body"])
    if status == 200:
        assert body["data"] == {"url": None, "expiresAt": None}
    else:
        assert body["error"]["code"] == "UPSTREAM_CONTRACT_MISMATCH"


def definitions():
    return {slot_key(program[0], target): {
        "condition": {"target": target}, "calculation_profile": {
            "Custom": {"PassThreshold": 80.0, "CertificateAdult": False}},
        "profile_version": "test-profile", "adapter_version": "test-adapter", "projection_version": "test-projection",
    } for program in PROGRAMS for target in TARGETS}


def schemas():
    return {"test-projection": ProjectionSchema("test-projection", {"CompressionDepth": {"value": "scalar"}})}


def adapter(version="test-adapter", projection="test-projection"):
    def forbidden(*args, **kwargs):
        pytest.fail("Role construction called the internal calculator.")
    return SimpleNamespace(version=version, projection_version=projection,
                           calculate=forbidden, validate_response=forbidden, get_chart=forbidden)


def configuration():
    objects = MemoryS3()
    bindings = MemoryLegacyBindings(objects)
    state = StateSettings("local-table", 8)
    storage = StorageSettings("development", bindings.bucket, bindings.directory, 1000000, 2000000)
    return objects, bindings, ApiSettings(state, storage, "local-assembly", 2000000)


class NoClientCalls:
    def __getattr__(self, name):
        pytest.fail("Construction accessed an SDK client operation.")


def course_http_stub():
    settings = fixture_course_settings()
    service = SimpleNamespace(
        list_courses=lambda *a, **k: pytest.fail("unexpected"),
        refresh_for_session=lambda auth: pytest.fail("unexpected"),
    )
    return CourseHttp(
        service, settings, clock=CLOCK, uuid_factory=lambda: "61000000-0000-4000-8000-000000000001",
        authenticate=lambda *a, **k: pytest.fail("auth"),
        login=lambda *a, **k: pytest.fail("login"),
    )


def test_w0_w4_share_contract_version_and_fixture_hashes():
    from mock_journey import course_policy, course_service, course_submission, course_provider, course_fixture
    assert CONTRACT_VERSION == "vcc-internal-v1"
    assert course_policy.POLICY_VERSION == POLICY_VERSION
    assert course_service.CourseService.__name__ == "CourseService"
    assert course_submission.SUBMISSION_POLICY_VERSION == "vcc-submission-policy-v1"
    assert course_provider.UnavailableCourseProvider.__name__ == "UnavailableCourseProvider"
    assert course_fixture.FixtureCourseProvider.__name__ == "FixtureCourseProvider"
    bundle = load_fixture("course_bundle.json")
    mapping = load_fixture("execution_mapping.json")
    assert bundle["contract_version"] == CONTRACT_VERSION
    assert mapping["contract_version"] == CONTRACT_VERSION
    # Frozen W0 files, verified unchanged before the hardening implementation.
    expected = {
        "course_bundle.json": "ba86f74a516509befdd268859326a5d857d6381f17e6768538276a2096f24600",
        "execution_mapping.json": "74130ac29ee35bad41f3ab25e5c88efcb784dd60897c7b93a029866c2436d545",
        "typed_id_vectors.json": "d1e30e415f9f26db398e7ff0e4ee9ef834472b8266bfe9748901819d3d43643f",
        "wire_cases.json": "9e9ef92d91c18eb54db1a3db72ab67c510d96bd622592f2dcaecd0361399ecb0",
    }
    assert {name: fixture_hash(name) for name in expected} == expected


def test_build_course_application_injects_provider_without_sdk_or_arc(monkeypatch):
    objects, bindings, settings = configuration()
    client = NoClientCalls()
    execution = ExecutionCatalog(definitions(), schemas())
    keys = {"v1": b"test-only-key-material-32-bytes!!" + b"!"}
    opened = []

    def connect(self, address):
        opened.append(address)
        pytest.fail("course assembly opened a network socket")

    monkeypatch.setattr(socket.socket, "connect", connect)
    app = build_course_application(
        settings, dynamodb_client=client, s3_client=client, legacy_bindings=bindings,
        resume_keys=keys, current_key_version="v1", execution=execution,
        provider=UnavailableCourseProvider(), course_settings=fixture_course_settings(),
        clock=CLOCK, mapping_document=mapping_document(), dummy_learner=dummy_learner(),
    )
    assert isinstance(app, CourseApplication)
    assert app.course_mode == COURSE_MODE
    assert app.catalog.slot_keys == Catalog().slot_keys
    assert type(app.gateway) is DisabledArcGateway
    receipt = app.gateway.submit({"status": "disabled"})
    assert receipt.status == "disabled" and receipt.receipt_id is None
    assert opened == []
    assert objects.objects == {}


def test_default_builders_remain_mock_v1_and_do_not_share_course_mode():
    objects, bindings, settings = configuration()
    client = NoClientCalls()
    execution = ExecutionCatalog(definitions(), schemas())
    keys = {"v1": b"test-only-key-material-32-bytes!!" + b"!"}
    service = build_application(
        settings, dynamodb_client=client, s3_client=client, legacy_bindings=bindings,
        resume_keys=keys, current_key_version="v1", execution=execution, clock=CLOCK,
    )
    worker = build_worker(
        WorkerSettings(settings.state, settings.storage, 60, 5),
        dynamodb_client=client, s3_client=client, legacy_bindings=bindings,
        adapters=[adapter()], required_bindings=execution.required_bindings, clock=CLOCK,
    )
    relay = build_relay(
        RelaySettings(settings.state, "https://sqs.example.invalid/local", 30, 5, 10, 2),
        dynamodb_client=client, sqs_client=client, clock=CLOCK,
    )
    assert getattr(service, "course_mode", None) is None
    assert getattr(worker, "course_mode", None) is None
    assert getattr(relay, "course_mode", None) is None
    assert isinstance(worker.completion_plan, CourseCompletionPlan)
    assert worker.course_recovery is not None


def test_aws_fake_assembly_stays_on_mock_builder():
    from tests.test_aws_runtime import FakeSdk, environment
    from mock_journey.aws_runtime import build_runtime
    factory = FakeSdk()
    runtime = build_runtime("api", environment("api"), client_factory=factory)
    assert getattr(runtime.target, "course_mode", None) is None
    assert not hasattr(runtime.target, "course_http")
    assert [name for name, _ in factory.calls] == ["dynamodb", "s3"]


def test_course_v2_old_routes_are_404_without_redirect():
    http = course_http_stub()
    service = SimpleNamespace(course_mode=COURSE_MODE, course_http=http, operations=None)
    for method, path in OLD_ROUTES:
        response = handle(event(method, path, body={} if method in ("POST", "PUT") else None), CONTEXT, service)
        assert response["statusCode"] == 404, (method, path, response)
        body = json.loads(response["body"])
        assert body["success"] is False
        assert body["error"]["code"] == "NOT_FOUND"
        assert "Location" not in response["headers"]


def test_course_v2_manifest_matches_app_routes():
    manifest = json.loads((ROOT / "docs/implementation_execution/P4C_ROUTE_ROLE_MANIFEST.json").read_text())
    listed = {(row["method"], row["path"]) for row in manifest["course_v2"]["routes"]}
    expected = {(spec.method, spec.path) for spec in APP_ROUTES}
    assert listed == expected
    assert manifest["course_v2"]["old_routes_status"] == 404
    assert manifest["course_v2"]["cpr_analysis_alias"] is False
    assert manifest["course_v2"]["external_transmit"] == 0
    assert manifest["control_body_limit_bytes"] == 16384


def test_optional_course_binding_is_not_required_on_legacy_create():
    repo, client = repository([("read", snapshot(SESSION, USER, None)), ("write", {})])
    result = repo.create_attempt(AUTH, "request-a", "request-digest", TEMPLATE)
    assert "course_binding" not in result
    assert result["definition_json"] == DEFINITION


def test_optional_course_binding_is_stored_when_typed():
    binding = CourseBinding(
        digest(["scope"]), digest(["place"]), "training", "a" * 64, "v1", "epoch-a", POLICY_VERSION,
    )
    template = {**TEMPLATE, "course_binding": binding}
    repo, client = repository([("read", snapshot(SESSION, USER, None)), ("write", {})])
    result = repo.create_attempt(AUTH, "request-a", "request-digest", template)
    assert result["course_binding"]["start_role"] == "training"
    assert result["course_binding"]["policy_version"] == POLICY_VERSION
    item_action = next(action["Put"]["Item"] for action in client.calls[1][1]["TransactItems"] if "Put" in action and action["Put"]["Item"]["PK"]["S"].startswith("ATTEMPT#"))
    assert "course_binding" in item_action


def test_repository_exposes_load_inventory_and_jobs_accept_completion_plan():
    assert callable(getattr(DynamoCourseRepository, "load_inventory"))
    assert callable(getattr(DynamoCourseRepository, "load_inventory_for_session"))
    assert "completion_plan" in DynamoJobRepository.finalize.__code__.co_varnames
    assert callable(getattr(DynamoJobRepository, "close_course_terminal"))
    assert callable(getattr(DynamoJobRepository, "reopen_course_recovery"))
    plan = CourseCompletionPlan()
    assert callable(plan.build)


def test_handler_without_course_mode_keeps_mock_login_contract():
    calls = []

    class Service:
        def __init__(self):
            self.auth = SimpleNamespace(authenticate=lambda *a, **k: pytest.fail("auth"))
            self.operations = None

        def login(self, body):
            calls.append(body)
            return {"session_token": "t"}

    response = handle(event("POST", "/mock/v1/sessions", body={"login_id": "test@test.com", "password": "2222"}),
                      CONTEXT, Service())
    assert response["statusCode"] == 201
    assert calls == [{"login_id": "test@test.com", "password": "2222"}]

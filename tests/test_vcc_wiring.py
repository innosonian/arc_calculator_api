"""W5 wiring: assembly, course_v2 dispatch, optional legacy fields, G-ARC send 0.

Also binds docs/implementation_execution/P4C_ROUTE_ROLE_MANIFEST.json (v2) to
the source: routes, fixed error tables and the handler/v2 envelopes,
SESSION_SECONDS, the control-body limit and the role entrypoints (moved from the
removed tests/test_mock_route_contract.py, D103).
"""

from types import SimpleNamespace
from datetime import datetime, timezone
import json
import socket
from pathlib import Path

import pytest

from mock_journey import assembly
from mock_journey.assembly import ExecutionCatalog, build_course_application, build_relay, build_worker
from mock_journey.auth import SESSION_SECONDS
from mock_journey.course_contracts import (
    APP_ROUTES, CONTRACT_VERSION, HOOK_NAMES, POLICY_VERSION, AttemptRecord, CalculationRecord, CourseHooks,
    SessionRecord,
)
from mock_journey.course_errors import _COURSE_ERRORS, CourseError
from mock_journey.course_http import CourseHttp
from mock_journey.course_provider import UnavailableCourseProvider
from mock_journey.course_settings import fixture_course_settings
from mock_journey.course_state import DynamoCourseRepository
from mock_journey.course_submission import DisabledArcGateway, CourseCompletionPlan
from mock_journey.course_wiring import COURSE_MODE, CourseApplication, bind_course_http
from mock_journey.errors import _ERRORS, JourneyError
from mock_journey.handler import handle
from mock_journey.jobs import DynamoJobRepository
from mock_journey.service import JourneyService
from mock_journey.settings import WorkerSettings, RelaySettings
from tests.assembly_support import NoClientCalls, adapter, configuration, definitions, schemas
from tests.course_hooks_support import hooks_with
from tests.vcc_support import OLD_ROUTES, dummy_learner, event, fixture_hash, load_fixture, mapping_document


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((ROOT / "docs/implementation_execution/P4C_ROUTE_ROLE_MANIFEST.json").read_text())
CONTEXT = SimpleNamespace(aws_request_id="vcc-wire-request")
CLOCK = lambda: 1_800_000_000
AUTH = SimpleNamespace(session_id="61000000-0000-4000-8000-00000000000a", principal="dummy-tester",
                       revision=0, expires_at=1_800_086_400)


def calculation_http(calculation, read_attempt):
    journey = SimpleNamespace(
        calculation=calculation,
        auth=SimpleNamespace(authenticate=lambda *a, **k: AUTH),
        state=SimpleNamespace(get_attempt=read_attempt),
    )
    return bind_course_http(
        journey, SimpleNamespace(), fixture_course_settings(), clock=CLOCK,
        uuid_factory=lambda: "61000000-0000-4000-8000-000000000001",
    )


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_pending_calculation_read_is_not_promoted_by_later_completion(method):
    attempt_id = "61000000-0000-4000-8000-000000000002"
    pending = {"attempt_id": attempt_id, "state": "queued"}
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


def course_http_stub():
    settings = fixture_course_settings()
    service = SimpleNamespace(
        list_courses=lambda *a, **k: pytest.fail("unexpected"),
        refresh_for_session=lambda auth: pytest.fail("unexpected"),
    )
    return CourseHttp(
        service, settings, clock=CLOCK, uuid_factory=lambda: "61000000-0000-4000-8000-000000000001",
        hooks=hooks_with(authenticate=lambda *a, **k: pytest.fail("auth"),
                         login=lambda *a, **k: pytest.fail("login")),
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
        "wire_cases.json": "9104f5a7e1153e87f16ccf2a570c3f114c3079423a751d4d5764058aa58a9d94",
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
    # Only the course parts and the shared auth/state/calculation/operations remain.
    assert set(vars(app)) == {"course_http", "course_service", "provider", "repository", "gateway",
                              "auth", "state", "calculation", "operations"}
    assert type(app.course_http) is CourseHttp
    assert type(app.gateway) is DisabledArcGateway
    receipt = app.gateway.submit({"status": "disabled"})
    assert receipt.status == "disabled" and receipt.receipt_id is None
    assert opened == []
    assert objects.objects == {}


def test_course_application_is_the_only_public_api_builder_and_other_roles_have_no_course_mode():
    objects, bindings, settings = configuration()
    client = NoClientCalls()
    execution = ExecutionCatalog(definitions(), schemas())
    assert not hasattr(assembly, "build_application")
    public = {name for name in vars(assembly) if name.startswith("build_")}
    assert public == {"build_course_application", "build_worker", "build_relay"}
    worker = build_worker(
        WorkerSettings(settings.state, settings.storage, 60, 5),
        dynamodb_client=client, s3_client=client, legacy_bindings=bindings,
        adapters=[adapter()], required_bindings=execution.required_bindings, clock=CLOCK,
    )
    relay = build_relay(
        RelaySettings(settings.state, "https://sqs.example.invalid/local", 30, 5, 10, 2),
        dynamodb_client=client, sqs_client=client, clock=CLOCK,
    )
    assert getattr(worker, "course_mode", None) is None
    assert getattr(relay, "course_mode", None) is None
    assert isinstance(worker.completion_plan, CourseCompletionPlan)
    assert worker.course_recovery is not None


def test_aws_fake_assembly_always_uses_course_builder():
    from tests.aws_runtime_support import FakeSdk, configuration, environment
    from mock_journey.aws_runtime import build_runtime
    factory = FakeSdk()
    runtime = build_runtime("api", environment("api"), client_factory=factory)
    assert runtime.target.course_mode == COURSE_MODE
    assert type(runtime.target.course_http) is CourseHttp
    assert [name for name, _ in factory.calls] == ["dynamodb", "s3"]
    # No course-less (/mock/v1-only) API composition remains to fall back to.
    config = configuration("api")
    config.pop("course")
    calls = []
    with pytest.raises(JourneyError):
        build_runtime("api", environment("api", config), client_factory=lambda *a, **k: calls.append(a))
    assert calls == []


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
    manifest = MANIFEST
    listed = [(row["method"], row["path"]) for row in manifest["course_v2"]["routes"]]
    expected = [(spec.method, spec.path) for spec in APP_ROUTES]
    assert listed == expected
    assert [row["id"] for row in manifest["course_v2"]["routes"]] == [spec.route_id for spec in APP_ROUTES]
    # routes[].query is the RouteSpec allowlist (absent when the route takes no query).
    assert [tuple(row.get("query", ())) for row in manifest["course_v2"]["routes"]] == [
        spec.query_allowed for spec in APP_ROUTES]
    assert manifest["course_v2"]["methods"] == ["GET", "POST", "PUT", "DELETE"]
    assert set(manifest["course_v2"]["methods"]) == {spec.method for spec in APP_ROUTES}
    # The manifest names which of its fields are bound to source here and which only describe.
    scope = manifest["field_scope"]
    assert scope["bound_by"] == "tests/test_vcc_wiring.py"
    assert "course_v2.routes[].query" in scope["bound"] and "course_v2.methods" in scope["bound"]
    assert not set(scope["bound"]) & set(scope["descriptive"])
    assert manifest["schema_version"] == "arc-mock-route-role-v2"
    assert manifest["course_v2"]["mode"] == "only"
    assert manifest["course_v2"]["old_routes_status"] == 404
    assert manifest["course_v2"]["cpr_analysis_alias"] is False
    assert manifest["course_v2"]["external_transmit"] == 0
    assert manifest["course_v2"]["factory"] == (
        f"{build_course_application.__module__}.{build_course_application.__name__}")
    assert manifest["course_v2"]["api_dispatch"] == f"{CourseHttp.__module__}.{CourseHttp.__qualname__}.dispatch"
    # No v1 route inventory remains in the manifest.
    assert not {"routes", "query_parameters", "attempt_path_parameter", "common_response_headers"} & set(manifest)
    assert "/mock/v1" not in json.dumps({key: value for key, value in manifest.items() if key != "course_v2"})
    old = {(method, path) for method, path in OLD_ROUTES}
    assert not old & set(listed)


def test_course_hooks_require_every_hook_to_be_callable_at_assembly():
    given = {name: (lambda *a, **k: None) for name in HOOK_NAMES}
    assert type(CourseHooks(**given)) is CourseHooks
    assert len(HOOK_NAMES) == 12
    for name in HOOK_NAMES:
        with pytest.raises(CourseError) as raised:
            CourseHooks(**{**given, name: None})
        assert raised.value.code == "TEMPORARILY_UNAVAILABLE", name
    with pytest.raises(TypeError):
        CourseHooks(**{key: value for key, value in given.items() if key != "chart_link"})
    hooks = CourseHooks(**given)
    with pytest.raises(Exception):
        hooks.login = lambda *a: None  # frozen


def test_production_wiring_returns_records_not_dicts():
    from mock_journey.course_wiring import _attempt_record, _calculation_record, _login_hook, _session_check
    attempt = {"attempt_id": "61000000-0000-4000-8000-000000000002", "state": "created", "created_at": 1_800_000_000,
               "definition_json": json.dumps({"condition": {"mode": "training"}}),
               "course_binding": {"start_role": "training", "definition_hash": "b" * 64},
               "course_id": 101, "enrollment_id": 501, "course_item_link_id": 1001, "role": "unused"}
    record = _attempt_record(attempt)
    assert record == AttemptRecord(
        attempt_id="61000000-0000-4000-8000-000000000002", state="created", created_at="2027-01-15T08:00:00Z",
        condition={"mode": "training"}, course_id=101, enrollment_id=501, course_item_link_id=1001,
        definition_hash="b" * 64, role="training", legacy=False)
    # The role comes from the binding only; a top-level "role" is never read (C-11).
    assert _attempt_record({**attempt, "course_binding": {"definition_hash": "b" * 64}}).role is None
    legacy = _attempt_record({"attempt_id": attempt["attempt_id"], "state": "created", "created_at": 5})
    assert legacy == AttemptRecord(attempt_id=attempt["attempt_id"], state="created",
                                   created_at="1970-01-01T00:00:05Z", condition=None, legacy=True)
    assert type(_calculation_record({"attempt_id": attempt["attempt_id"], "state": "queued"}, None)) is CalculationRecord
    journey = SimpleNamespace(
        login_command=lambda login_id, password: ({"session_id": AUTH.session_id, "expires_at": 1_800_086_400}, "tok"),
        auth=SimpleNamespace(authenticate=lambda token: AUTH), check_session=lambda auth: None,
    )
    assert _login_hook(journey)("test@test.com", "2222") == SessionRecord(
        session_id=AUTH.session_id, expires_at="2027-01-16T08:00:00Z", auth=AUTH, access_token="tok",
        user_name="Test User")
    assert _session_check(journey)(AUTH) == SessionRecord(session_id=AUTH.session_id, expires_at="2027-01-16T08:00:00Z")


class _CallSpy:
    """Any attribute is another spy; calling one is recorded and fails (nothing may be reached)."""

    def __init__(self, calls, name):
        self._calls, self._name = calls, name

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return _CallSpy(self._calls, f"{self._name}.{name}")

    def __call__(self, *args, **kwargs):
        self._calls.append(self._name)
        raise AssertionError(f"{self._name} was reached before authentication")


_PATH_VALUES = {"courseId": "101", "courseItemLinkId": "1001", "attemptId": "61000000-0000-4000-8000-000000000002"}
_QUERY_VALUES = {"enrollmentId": "501", "page": "1", "pageSize": "1"}
_PROTECTED = [spec for spec in APP_ROUTES if spec.auth_required]


def test_login_is_the_only_route_without_authentication():
    assert [spec.route_id for spec in APP_ROUTES if not spec.auth_required] == ["login"]
    assert [row["id"] for row in MANIFEST["course_v2"]["routes"] if row["id"] != "login"] == [
        spec.route_id for spec in _PROTECTED]


@pytest.mark.parametrize("credential", [None, "Basic c2VjcmV0", "Bearer "], ids=["absent", "not_bearer", "empty"])
@pytest.mark.parametrize("body", [object(), "%%%", None], ids=["not_a_body", "not_json", "none"])
@pytest.mark.parametrize("spec", _PROTECTED, ids=[spec.route_id for spec in _PROTECTED])
def test_each_protected_route_stops_before_any_hook_service_or_body_access(spec, body, credential):
    """Moved from the removed v1 route contract test (D103), for every protected /api/v2 route.

    The assembled hooks (bind_course_http over a spied journey and course
    service) are never reached without a bearer, and the body
    is not parsed first: a valid query, no usable Authorization and a body that
    would be an input error still answer 401 SESSION_REQUIRED.
    """
    calls = []
    journey = _CallSpy(calls, "journey")
    http = bind_course_http(
        journey, _CallSpy(calls, "course_service"), fixture_course_settings(), clock=CLOCK,
        uuid_factory=lambda: "61000000-0000-4000-8000-000000000001",
    )
    path = spec.path
    for name, value in _PATH_VALUES.items():
        path = path.replace("{" + name + "}", value)
    assert "{" not in path
    query = {key: _QUERY_VALUES[key] for key in spec.query_allowed}
    headers = {"Content-Type": "application/json"}
    if credential is not None:
        headers["Authorization"] = credential
    value = {"httpMethod": spec.method, "path": path, "headers": headers,
             "queryStringParameters": query or None, "body": body}
    service = SimpleNamespace(course_mode=COURSE_MODE, course_http=http, operations=None)
    response = handle(value, CONTEXT, service)
    assert response["statusCode"] == 401, response
    assert json.loads(response["body"])["error"]["code"] == "SESSION_REQUIRED"
    assert calls == []


def test_manifest_limits_and_session_seconds_are_bound_to_source():
    assert MANIFEST["session_seconds"] == SESSION_SECONDS == 86400
    limit = fixture_course_settings().max_control_body_bytes
    assert MANIFEST["control_body_limit_bytes"] == MANIFEST["course_v2"]["control_body_limit_bytes"] == limit == 16384
    assert MANIFEST["chart_link_seconds"] == 300


def test_manifest_measurement_content_type_states_the_headers_value_rule():
    # D103 port: a headers value is judged too (not left to the parser); the
    # behavior itself is pinned by tests/test_v2_measurement_content_type.py.
    text = MANIFEST["course_v2"]["measurement_content_type"]
    assert "from headers or multiValueHeaders must be a nonempty string without a comma, CR or LF" in text
    assert "left to the parser" not in text


def test_manifest_chart_link_seconds_is_the_signed_and_reported_lifetime():
    """chart_link_seconds is bound to behavior, not only to a literal.

    GET chart-link signs the published chart for that many seconds and reports
    expiresAt = now + that many seconds (calculation.chart_link and
    storage.sign_chart each hold the value).
    """
    from tests.journey_support import JourneyStore, V2Journey, dummy_course
    seconds = MANIFEST["chart_link_seconds"]
    h = V2Journey(JourneyStore.memory())
    session = h.login()
    course = dummy_course("mock-compression-only", "adult")
    started = h.start(session.token, course, course.practice_link_id)
    h.upload(session.token, started["attemptId"], started["condition"])
    assert h.work(started["attemptId"]) is True
    h.result(session.token, started["attemptId"])
    signed = len(h.objects.sign_calls)
    link = h.chart_link(session.token, started["attemptId"])
    assert link["url"] is not None
    assert [call[-1] for call in h.objects.sign_calls[signed:]] == [seconds]
    expires = datetime.fromtimestamp(int(h.clock()) + seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert link["expiresAt"] == expires


def test_manifest_entrypoints_are_the_actual_role_functions():
    from mock_journey import dispatch, handler, worker
    assert MANIFEST["api_entrypoint"] == f"{handler.run.__module__}.{handler.run.__name__}"
    assert MANIFEST["handler_function"] == f"{handler.handle.__module__}.{handler.handle.__name__}"
    roles = {role["role"]: role for role in MANIFEST["roles"]}
    assert {name: role["entrypoint"] for name, role in roles.items()} == {
        "api": "mock_journey.handler.run", "worker": "mock_journey.worker.run", "relay": "mock_journey.dispatch.run"}
    assert {name: role["injected_entrypoint"] for name, role in roles.items()} == {
        "api": "mock_journey.handler.handle", "worker": "mock_journey.worker.handle",
        "relay": "mock_journey.dispatch.handle_stream"}
    assert callable(worker.run) and callable(worker.handle) and callable(dispatch.run) and callable(dispatch.handle_stream)
    typed_commands = ("login_command", "check_session", "reauthorize_command", "cancel_command")
    assert all(callable(getattr(JourneyService, name)) for name in typed_commands)
    api_dependencies = " ".join(roles["api"]["dependencies"])
    assert all(name in api_dependencies for name in typed_commands + ("CourseHttp", "DummyDevCourseProvider"))
    for removed in ("login", "session", "programs", "create_attempt", "get_attempt", "attempt_view",
                    "require_calculation", "reauthorize", "cancel"):
        assert not hasattr(JourneyService, removed), removed


def test_manifest_error_tables_are_the_fixed_source_tables():
    assert MANIFEST["errors"] == {code: {"status": status, "message": message}
                                  for code, (status, message) in _ERRORS.items()}
    assert MANIFEST["course_v2"]["course_errors"] == {code: {"status": status, "message": message}
                                                      for code, (status, message) in _COURSE_ERRORS.items()}
    assert MANIFEST["legacy_history_error_codes"] == ["PROGRAM_ALREADY_COMPLETED"]
    assert set(MANIFEST["legacy_history_error_codes"]) <= set(_ERRORS)


class _RaisingHttp:
    def __init__(self, error):
        self.error = error

    def dispatch(self, event):
        raise self.error


@pytest.mark.parametrize("name", sorted(_ERRORS))
def test_manifest_error_mapping_is_the_actual_handler_envelope(name):
    contract = MANIFEST["errors"][name]
    service = SimpleNamespace(course_mode=COURSE_MODE, course_http=_RaisingHttp(JourneyError(name)), operations=None)
    response = handle(event("GET", "/api/v2/session/", token="t"), CONTEXT, service)
    assert response["statusCode"] == contract["status"]
    assert response["headers"] == MANIFEST["boot_failure_response"]["headers"]
    assert json.loads(response["body"]) == {"error": {
        "code": name, "message": contract["message"], "request_id": CONTEXT.aws_request_id,
    }}


@pytest.mark.parametrize("name", sorted(set(_ERRORS) | set(_COURSE_ERRORS)))
def test_manifest_error_mapping_is_the_actual_v2_envelope(name):
    contract = {**MANIFEST["errors"], **MANIFEST["course_v2"]["course_errors"]}[name]

    def reader(auth):
        raise (CourseError(name) if name in _COURSE_ERRORS else JourneyError(name))

    http = CourseHttp(
        SimpleNamespace(), fixture_course_settings(), clock=CLOCK,
        uuid_factory=lambda: "61000000-0000-4000-8000-000000000001",
        hooks=hooks_with(authenticate=lambda *a, **k: AUTH, session_reader=reader),
    )
    service = SimpleNamespace(course_mode=COURSE_MODE, course_http=http, operations=None)
    response = handle(event("GET", "/api/v2/session/", token="t"), CONTEXT, service)
    assert response["statusCode"] == contract["status"]
    body = json.loads(response["body"])
    assert body == {"success": False, "error": {"code": name, "message": contract["message"], "details": None},
                    "timestamp": "2027-01-15T08:00:00Z"}
    assert response["headers"] == {"Content-Type": "application/json", "Cache-Control": "no-store",
                                   "X-Request-Id": "61000000-0000-4000-8000-000000000001"}


def test_manifest_routes_and_errors_are_in_the_app_api_document():
    document = (ROOT / "docs/APP_API.md").read_text()
    for row in MANIFEST["course_v2"]["routes"]:
        assert f'| `{row["method"]} {row["path"]}` |' in document, row
    for name, contract in {**MANIFEST["errors"], **MANIFEST["course_v2"]["course_errors"]}.items():
        assert f'| `{contract["status"]}` | `{name}` | {contract["message"]} |' in document, name


def test_repository_exposes_load_inventory_and_jobs_accept_completion_plan():
    assert callable(getattr(DynamoCourseRepository, "load_inventory"))
    assert callable(getattr(DynamoCourseRepository, "load_inventory_for_session"))
    assert "completion_plan" in DynamoJobRepository.finalize.__code__.co_varnames
    assert callable(getattr(DynamoJobRepository, "close_course_terminal"))
    assert callable(getattr(DynamoJobRepository, "reopen_course_recovery"))
    plan = CourseCompletionPlan()
    assert callable(plan.build)


@pytest.mark.parametrize("service", [
    SimpleNamespace(operations=None),
    SimpleNamespace(course_mode=COURSE_MODE, course_http=None, operations=None),
    SimpleNamespace(course_mode="mock_v1", course_http=SimpleNamespace(dispatch=lambda e: pytest.fail("routed")),
                    operations=None),
], ids=["no_course_mode", "no_course_http", "other_mode"])
@pytest.mark.parametrize("method,path", [("POST", "/api/v2/sessions/"), ("POST", "/mock/v1/sessions"),
                                         ("GET", "/healthz")])
def test_handler_without_an_assembled_course_application_is_the_fixed_503(service, method, path):
    """No v1 fallback remains: the boot-failure envelope, and no login or routing is attempted."""
    body = {"loginId": "test@test.com", "password": "2222"} if method == "POST" else None
    response = handle(event(method, path, body=body), CONTEXT, service)
    expected = MANIFEST["boot_failure_response"]
    assert response["statusCode"] == expected["status"] == 503
    assert response["headers"] == expected["headers"]
    body = json.loads(response["body"])
    assert body == {"error": {
        "code": expected["code"], "message": "The service is temporarily unavailable.",
        "request_id": CONTEXT.aws_request_id,
    }}
    # The manifest names the nested shape: one top-level "error" object and its keys, in order.
    assert list(body) == list(expected["body"]) == ["error"]
    assert list(body["error"]) == expected["body"]["error"]

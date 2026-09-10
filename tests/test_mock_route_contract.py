"""Dispatch and public-inventory checks, not substitute state/calculation tests."""

import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from mock_journey import handler
from mock_journey.auth import SESSION_SECONDS
from mock_journey.errors import JourneyError, _ERRORS


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((ROOT / "docs/implementation_execution/P4C_ROUTE_ROLE_MANIFEST.json").read_text())
ROUTES = MANIFEST["routes"]
ATTEMPT = "a1234567-1234-4234-9234-123456789abc"
TOKEN = "opaque-route-test-token"
AUTH = object()
CONTEXT = SimpleNamespace(aws_request_id="route-request")
RAW = b'{ "zero": -0.0, "integer": 1, "decimal": 1.0, "nullable": null, "nested": {"ok": false} }'


class DispatchSpy:
    """Record concrete arguments after real handler parsing/auth extraction."""

    def __init__(self, route, status):
        self.route, self.status = route, status
        self.calls = []
        self.auth = SimpleNamespace(authenticate=self.authenticate)
        self.state = SimpleNamespace(logout=self.logout)
        self.calculation = SimpleNamespace(submit=self.submit, result=self.result, chart_link=self.chart_link)
        self.value = {"route": route["id"], "marker": [1, 1.0, None, False]}
        self.pending = {"attempt_id": ATTEMPT, "state": "queued", "wait_expired": False,
                        "status_path": f"/mock/v1/attempts/{ATTEMPT}"}

    def authenticate(self, token, *, allow_logout_receipt=False):
        self.calls.append(("auth.authenticate", token, allow_logout_receipt))
        if token != TOKEN:
            raise JourneyError("SESSION_REQUIRED")
        return AUTH

    def login(self, body):
        self.calls.append(("service.login", body))
        return self.value

    def session(self, auth):
        self.calls.append(("service.session", auth))
        return self.value

    def logout(self, auth):
        self.calls.append(("service.state.logout", auth))

    def programs(self, auth):
        self.calls.append(("service.programs", auth))
        return self.value

    def create_attempt(self, auth, body):
        self.calls.append(("service.create_attempt", auth, body))
        return self.status, self.value

    def get_attempt(self, auth, attempt_id):
        self.calls.append(("service.get_attempt", auth, attempt_id))
        return self.value

    def reauthorize(self, auth, attempt_id, body):
        self.calls.append(("service.reauthorize", auth, attempt_id, body))
        return self.value

    def cancel(self, auth, attempt_id, body):
        self.calls.append(("service.cancel", auth, attempt_id, body))

    def require_calculation(self):
        self.calls.append(("service.require_calculation",))
        return self.calculation

    def submit(self, auth, attempt_id, event):
        self.calls.append(("service.calculation.submit", auth, attempt_id, event))
        return self.status, RAW if self.status == 200 else self.pending

    def result(self, auth, attempt_id):
        self.calls.append(("service.calculation.result", auth, attempt_id))
        return self.status, RAW if self.status == 200 else self.pending

    def chart_link(self, auth, attempt_id):
        self.calls.append(("service.calculation.chart_link", auth, attempt_id))
        return self.value


def event_for(route):
    body = {field: "route-test-value" for field in route.get("required_string_fields", [])}
    headers = {"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"}
    if route.get("attempt_id_header"):
        headers["X-Attempt-ID"] = ATTEMPT
    return {
        "httpMethod": route["method"], "path": route["path"].replace("{attempt_id}", ATTEMPT),
        "headers": headers,
        "body": json.dumps(body) if route["request_body"] == "control_json" else "not JSON: untouched input",
        "isBase64Encoded": False,
    }, body


CASES = [(route, status) for route in ROUTES for status in route["success_statuses"]]


@pytest.mark.parametrize("route,status", CASES, ids=[f'{r["id"]}-{s}' for r, s in CASES])
def test_manifest_route_dispatch_arguments_status_and_serialization(route, status):
    service = DispatchSpy(route, status)
    event, parsed_body = event_for(route)
    response = handler.handle(event, CONTEXT, service)
    assert response["statusCode"] == status
    assert response["headers"] == MANIFEST["common_response_headers"]
    expected = [] if route["id"] == "login" else [("auth.authenticate", TOKEN, route["id"] == "logout")]
    if route["dispatch"].startswith("service.calculation."):
        expected.append(("service.require_calculation",))
    arguments = [] if route["id"] == "login" else [AUTH]
    if "{attempt_id}" in route["path"] or route.get("attempt_id_header"):
        arguments.append(ATTEMPT)
    if route["request_body"] == "control_json":
        arguments.append(parsed_body)
    elif route["dispatch"] == "service.calculation.submit":
        arguments.append(event)
    expected.append((route["dispatch"], *arguments))
    assert service.calls == expected
    if route["dispatch"] == "service.calculation.submit":
        assert service.calls[-1][-1] is event  # no body/envelope reconstruction by the router
    if status == 204:
        assert response["body"] == ""
    elif route["response"] == "stored_legacy_json_or_pending":
        if status == 200:
            decoded = json.loads(response["body"])
            assert decoded.pop("submit_arc") == {"status": "disabled", "ok": False, "error": "arc_contract_pending"}
            assert json.dumps(decoded) == json.dumps(json.loads(RAW))
        else:
            assert json.loads(response["body"]) == service.pending
            assert json.loads(response["body"])["wait_expired"] is False
    else:
        assert json.loads(response["body"]) == service.value


@pytest.mark.parametrize("route", [r for r in ROUTES if r["id"] != "login"], ids=lambda r: r["id"])
def test_each_protected_manifest_route_stops_before_command_or_measurement_access(route):
    event, _ = event_for(route)
    event["headers"] = {}
    event["body"] = object()  # parsing this first would become an input error
    service = DispatchSpy(route, route["success_statuses"][0])
    response = handler.handle(event, CONTEXT, service)
    assert response["statusCode"] == 401
    assert json.loads(response["body"])["error"]["code"] == "SESSION_REQUIRED"
    assert service.calls == []


def test_manifest_exhausts_concrete_paths_and_standard_method_dispatch():
    assert len(ROUTES) == 12 and len({r["id"] for r in ROUTES}) == 12
    inventory = {(r["method"], r["path"]): r for r in ROUTES}
    assert len(inventory) == 12
    paths = {r["path"] for r in ROUTES}
    # A newly added literal path/operation must also appear in the manifest,
    # while this matrix catches new methods on any currently declared path.
    tree = ast.parse(Path(handler.__file__).read_text())
    literal_paths = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)
                     and type(n.value) is str and (n.value.startswith("/mock/") or n.value == "/cpr-analysis")
                     and "(" not in n.value}
    assert literal_paths == {p for p in paths if "{attempt_id}" not in p}
    operations = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name) and node.left.id == "operation":
            operations.update(n.value for n in node.comparators
                              if isinstance(n, ast.Constant) and type(n.value) is str)
    assert operations == {p.split("{attempt_id}/", 1)[1] for p in paths if "{attempt_id}/" in p}
    for path in sorted(paths):
        for method in ("GET", "POST", "DELETE", "PUT", "PATCH", "OPTIONS", "HEAD", "TRACE", "CONNECT"):
            if (method, path) in inventory:
                continue  # success argument paths are independently parameterized above
            probe = {"id": "probe", "path": path, "method": method, "request_body": "ignored"}
            event, _ = event_for(probe)
            service = DispatchSpy(probe, 200)
            response = handler.handle(event, CONTEXT, service)
            assert response["statusCode"] == 404, (method, path)
            assert all(call[0] == "auth.authenticate" for call in service.calls)


@pytest.mark.parametrize("path", [
    "/mock/v1/missing", "/mock/v1/session/", "/mock/v1/attempts/not-a-uuid",
    f"/mock/v1/attempts/{ATTEMPT.upper()}", f"/mock/v1/attempts/{ATTEMPT}/missing",
])
def test_unknown_path_shapes_are_not_redirected_to_a_manifest_route(path):
    route = {"id": "probe", "method": "GET", "path": path, "request_body": "ignored"}
    event, _ = event_for(route)
    service = DispatchSpy(route, 200)
    assert handler.handle(event, CONTEXT, service)["statusCode"] == 404
    assert service.calls == []


@pytest.mark.parametrize("name,contract", MANIFEST["errors"].items())
def test_manifest_fixed_error_mapping_is_the_actual_handler_envelope(name, contract):
    route = next(r for r in ROUTES if r["id"] == "session")
    service = DispatchSpy(route, 200)
    def fail(_auth):
        raise JourneyError(name)
    service.session = fail
    event, _ = event_for(route)
    response = handler.handle(event, CONTEXT, service)
    assert response["statusCode"] == contract["status"]
    assert response["headers"] == MANIFEST["common_response_headers"]
    assert json.loads(response["body"]) == {"error": {
        "code": name, "message": contract["message"], "request_id": CONTEXT.aws_request_id,
    }}


@pytest.mark.parametrize("body,status,code", [
    ({}, 400, "INVALID_REQUEST"),
    ({"resume_credential": ""}, 400, "INVALID_REQUEST"),
    ({"resume_credential": 2222}, 400, "INVALID_REQUEST"),
    ({"resume_credential": "wrong", "extra": "value"}, 400, "INVALID_REQUEST"),
    ({"resume_credential": "wrong"}, 404, "NOT_FOUND"),
])
def test_reauthorization_distinguishes_control_shape_from_invalid_proof(body, status, code):
    from mock_journey.auth import AuthManager
    from mock_journey.service import JourneyService

    reads = []
    def stored_attempt(ident):
        reads.append(ident)
        return {"principal": "dummy-tester", "resume_digest": "0" * 64}
    real_proof_verifier = AuthManager(None, "route-only-environment", {"v1": b"K" * 32}, "v1")
    auth = SimpleNamespace(authenticate=lambda *args, **kwargs: SimpleNamespace(principal="dummy-tester"),
                           verify_resume=real_proof_verifier.verify_resume)
    state = SimpleNamespace(get_attempt_for_reauthorization=stored_attempt)
    service = JourneyService(state, auth, None)
    route = next(r for r in ROUTES if r["id"] == "reauthorize")
    event, _ = event_for(route)
    event["body"] = json.dumps(body)
    response = handler.handle(event, CONTEXT, service)
    assert response["statusCode"] == status
    assert json.loads(response["body"])["error"]["code"] == code
    assert reads == ([] if status == 400 else [ATTEMPT])


def test_inventory_constants_errors_entrypoints_and_document_stay_bound_to_source():
    assert set(MANIFEST["errors"]) == set(_ERRORS)
    assert MANIFEST["session_seconds"] == SESSION_SECONDS
    assert MANIFEST["control_body_limit_bytes"] == handler._CONTROL_BODY_LIMIT
    from mock_journey import dispatch, worker
    assert MANIFEST["api_entrypoint"] == f"{handler.run.__module__}.{handler.run.__name__}"
    entries = {role["role"]: role["entrypoint"] for role in MANIFEST["roles"]}
    assert entries == {"api": "mock_journey.handler.run", "worker": "mock_journey.worker.run",
                       "relay": "mock_journey.dispatch.run"}
    assert callable(worker.run) and callable(dispatch.run)
    # Verify documentation contains every public route and fixed error. Runtime
    # state/transaction semantics remain covered by the earlier focused suites.
    document = (ROOT / "docs/ARC_MOCK_IMPLEMENTED_API_CONTRACT_KO.md").read_text()
    for route in ROUTES:
        assert f'| {route["method"]} | `{route["path"]}` |' in document
    for name, contract in MANIFEST["errors"].items():
        assert f'| {contract["status"]} | `{name}` | {contract["message"]} |' in document
    assert "wait_expired" in document and "300초" in document and "24시간" in document

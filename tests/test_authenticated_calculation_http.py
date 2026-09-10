"""Adversarial authenticated dispatch; no sockets, AWS, or running HTTP server."""

from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

import lambda_handler
from mock_journey import handler, typed
from mock_journey.auth import AuthManager
from mock_journey.calculation import CalculationService
from mock_journey.catalog import Catalog
from mock_journey.errors import JourneyError
from mock_journey.projection import ProjectionSchema, typed_identity
from mock_journey.service import JourneyService
from mock_journey.state import DynamoStateRepository
from tests._synth import comp_session, condition_json, multipart_event


ATTEMPT = "a1234567-1234-4234-9234-123456789abc"
OTHER_ATTEMPT = "b1234567-1234-4234-9234-123456789abc"
ATTEMPT_PATH = f"/mock/v1/attempts/{ATTEMPT}/calculation"
CONDITION = json.loads(condition_json(training_type="compression_only"))
CONTEXT = SimpleNamespace(aws_request_id="a1234567-1234-4234-9234-123456789abc")
SECRET = "P3_PRIVATE_MARKER_DO_NOT_LOG"


class State:
    """Memory rows with real auth/ownership guards, not a transaction emulator."""

    def __init__(self, now):
        self.now, self.sessions, self.attempts, self.reads = now, {}, {}, []

    def create_session(self, session, slots):
        self.sessions[session["session_id"]] = deepcopy(session)

    def get_session(self, ident):
        return deepcopy(self.sessions.get(ident))

    def get_attempt(self, auth, ident):
        self.reads.append(ident)
        session, attempt = self.get_session(auth.session_id), self.attempts.get(ident)
        DynamoStateRepository._check_session(session, auth, self.now[0])
        DynamoStateRepository._check_attempt(attempt, auth)
        return deepcopy(attempt)


class Storage:
    def __init__(self):
        self.saved, self.reads = [], []
        self.before_save = lambda: None
        self.snapshot = b'{"value":1,"decimal":1.0,"nullable":null,"submit_hstm":{"ok":true}}'

    def save_input(self, projected, binding):
        self.before_save()
        self.saved.append((deepcopy(projected), deepcopy(binding)))
        return {"input_digest": typed_identity(projected),
                "manifest_ref": {"bucket": "test-only", "key": "accepted-input"}}

    def read_final(self, reference, binding, publication):
        self.reads.append((deepcopy(reference), deepcopy(binding), deepcopy(publication)))
        return self.snapshot


class Jobs:
    def __init__(self, state):
        self.state, self.accepted, self.rows = state, [], {}

    def accept_input(self, auth, ident, digest, reference, **kwargs):
        # Tests verify the application reaches an acceptance-time recheck.
        # DynamoDB's real conditional commit is exercised separately.
        self.state.get_attempt(auth, ident)
        self.accepted.append((ident, digest, deepcopy(reference)))
        self.state.attempts[ident].update(state="queued", input_digest=digest, job_id=kwargs["job_id"])

    def get_job(self, ident):
        return deepcopy(self.rows.get(ident))


@pytest.fixture
def world():
    now = [1000]
    state = State(now)
    auth = AuthManager(state, "security-test", {"v1": b"T" * 32}, "v1", clock=lambda: now[0])
    session, token = auth.login("test@test.com", "2222", Catalog().slot_keys)
    other_session, other_token = auth.login("test@test.com", "2222", Catalog().slot_keys)
    definition = {
        "condition": deepcopy(CONDITION), "calculation_profile": {},
        "goal": {"kind": "compressions", "required": 60}, "catalog_version": "mock-catalog-v1",
        "profile_version": "test-only-profile", "adapter_version": "test-only-internal",
        "projection_version": "test-only-projection",
    }
    state.attempts[ATTEMPT] = {
        "attempt_id": ATTEMPT, "principal": session["principal"],
        "bound_session_id": session["session_id"], "epoch": "test-epoch", "state": "created",
        "definition_json": json.dumps(definition),
    }
    storage, jobs = Storage(), Jobs(state)
    calculation = CalculationService(state, jobs, storage,
                                     {"test-only-projection": ProjectionSchema("test-only-projection", {})},
                                     payload_limit=100_000, clock=lambda: now[0])
    service = JourneyService(state, auth, Catalog(), calculation)
    return SimpleNamespace(service=service, state=state, storage=storage, jobs=jobs, calculation=calculation,
                           now=now, session=session, token=token, other_session=other_session, other_token=other_token)


def event(world, path="/cpr-analysis", *, authenticated=True, method="POST"):
    value = multipart_event({"rawHexBPfile": comp_session(60), "condition": json.dumps(CONDITION)})
    value.update(path=path, httpMethod=method)
    if method == "GET":
        value.pop("body")
    if authenticated:
        value["headers"]["Authorization"] = "Bearer " + world.token
    if path == "/cpr-analysis":
        value["headers"]["X-Attempt-ID"] = ATTEMPT
    return value


def call(world, value):
    return handler.handle(value, CONTEXT, world.service)


def assert_error(response, status, code):
    assert response["statusCode"] == status
    body = json.loads(response["body"])
    assert body["error"]["code"] == code
    assert SECRET not in response["body"]


@pytest.mark.parametrize("path", ["/cpr-analysis", ATTEMPT_PATH])
def test_missing_bearer_stops_before_body_or_attempt_access(world, path):
    value = event(world, path, authenticated=False)
    value["body"] = object()
    assert_error(call(world, value), 401, "SESSION_REQUIRED")
    assert world.state.reads == [] and world.storage.saved == [] and world.jobs.accepted == []


@pytest.mark.parametrize("authorization,multi", [
    ("Basic token", None), ("Bearer ", None), (["Bearer token"], None),
    ("Bearer token, Bearer token", None),
    ("Bearer token", ["Bearer token", "Bearer token"]),
    ("Bearer token", ["Bearer other"]),
])
def test_ambiguous_authorization_never_reaches_a_calculation(world, authorization, multi):
    value = event(world)
    value["headers"]["Authorization"] = authorization
    if multi is not None:
        value["multiValueHeaders"] = {"Authorization": multi}
    assert_error(call(world, value), 401, "SESSION_REQUIRED")
    assert world.state.reads == [] and world.jobs.accepted == []


@pytest.mark.parametrize("attack", ["body", "authorizer_claim", "alternate_header", "worker_event"])
def test_credentials_and_worker_markers_outside_bearer_do_not_authorize(world, attack):
    value = event(world, authenticated=False)
    if attack == "body":
        value["body"] = json.dumps({"access_token": world.token, "session_token": world.token,
                                    "attempt_id": ATTEMPT, "password": "2222"})
    elif attack == "authorizer_claim":
        value["requestContext"] = {"authorizer": {"principalId": world.session["principal"],
                                                  "session_token": world.token}}
    elif attack == "alternate_header":
        value["headers"]["X-User-Token"] = world.token
    else:
        value["Records"] = [{"body": json.dumps({"job_id": ATTEMPT})}]
        value["job_id"] = ATTEMPT
    assert_error(call(world, value), 401, "SESSION_REQUIRED")
    assert world.storage.saved == [] and world.jobs.accepted == []


@pytest.mark.parametrize("query_field", ["queryStringParameters", "multiValueQueryStringParameters"])
def test_query_values_cannot_supply_credentials_or_attempt_selection(world, query_field):
    value = event(world, authenticated=False)
    value[query_field] = {"session_token": [world.token] if query_field.startswith("multi") else world.token}
    assert_error(call(world, value), 400, "INVALID_REQUEST")
    assert world.state.reads == [] and world.jobs.accepted == []


@pytest.mark.parametrize("header", [None, "", True, 123, [], ATTEMPT.upper(), " " + ATTEMPT,
                                     ATTEMPT + " ", ATTEMPT + "," + ATTEMPT, "{" + ATTEMPT + "}",
                                     ATTEMPT + "\r\nX-Injected: yes"])
def test_malformed_or_missing_attempt_header_cannot_select_a_record(world, header):
    value = event(world)
    if header is None:
        value["headers"].pop("X-Attempt-ID")
    else:
        value["headers"]["X-Attempt-ID"] = header
    assert_error(call(world, value), 400, "INVALID_REQUEST")
    assert world.state.reads == [] and world.storage.saved == []


@pytest.mark.parametrize("attack", ["duplicate_case", "duplicate_multi", "different_multi", "two_multi_names", "empty_multi"])
def test_attempt_header_duplicates_and_conflicting_proxy_representations_are_rejected(world, attack):
    value = event(world)
    if attack == "duplicate_case":
        value["headers"]["x-attempt-id"] = ATTEMPT
    elif attack == "duplicate_multi":
        value["multiValueHeaders"] = {"X-Attempt-ID": [ATTEMPT, ATTEMPT]}
    elif attack == "different_multi":
        value["multiValueHeaders"] = {"X-Attempt-ID": [OTHER_ATTEMPT]}
    elif attack == "two_multi_names":
        value["multiValueHeaders"] = {"X-Attempt-ID": [ATTEMPT], "x-attempt-id": [ATTEMPT]}
    else:
        value["multiValueHeaders"] = {"X-Attempt-ID": []}
    assert_error(call(world, value), 400, "INVALID_REQUEST")
    assert world.storage.saved == []


def test_matching_single_and_multi_headers_keep_the_existing_input_bytes(world):
    value = event(world)
    value["multiValueHeaders"] = {"x-attempt-id": [ATTEMPT], "authorization": ["Bearer " + world.token]}
    response = call(world, value)
    assert response["statusCode"] == 202
    assert world.storage.saved[0][0].cpr_bytes == comp_session(60)
    assert world.storage.saved[0][0].payload["calculation_input"]["condition"] == CONDITION


def test_multi_value_only_content_type_preserves_multipart_bytes_and_does_not_mutate_event(world):
    value = event(world)
    content_type = value["headers"].pop("Content-Type")
    value["multiValueHeaders"] = {"content-type": [content_type]}
    before = deepcopy(value)
    assert call(world, value)["statusCode"] == 202
    assert value == before
    assert world.storage.saved[0][0].cpr_bytes == comp_session(60)


@pytest.mark.parametrize("attack", ["conflicting", "duplicated", "casing"])
def test_content_type_ambiguity_is_rejected_before_the_measurement_parser(world, attack, monkeypatch):
    import mock_journey.legacy_bridge as bridge

    def forbidden(*args, **kwargs):
        pytest.fail("Ambiguous content type reached the measurement parser.")

    monkeypatch.setattr(bridge, "parse_measurement", forbidden)
    value = event(world)
    content_type = value["headers"]["Content-Type"]
    if attack == "conflicting":
        value["multiValueHeaders"] = {"content-type": ["application/json"]}
    elif attack == "duplicated":
        value["multiValueHeaders"] = {"content-type": [content_type, content_type]}
    else:
        value["headers"]["content-type"] = content_type
    assert_error(call(world, value), 400, "INVALID_REQUEST")
    assert world.storage.saved == [] and world.jobs.accepted == []


def test_path_and_header_disagreement_never_chooses_one_identifier(world):
    value = event(world, ATTEMPT_PATH)
    value["headers"]["X-Attempt-ID"] = OTHER_ATTEMPT
    assert_error(call(world, value), 400, "INVALID_REQUEST")
    assert world.state.reads == []


@pytest.mark.parametrize("path", ["/cpr-analysis", ATTEMPT_PATH])
@pytest.mark.parametrize("state", ["expired", "revoked"])
def test_stale_tokens_are_rejected_on_both_calculation_paths(world, path, state):
    row = world.state.sessions[world.session["session_id"]]
    if state == "expired":
        world.now[0] = row["expires_at"]
    else:
        row.update(status="revoked", revision=1, logout_epoch="previous-logout")
    value = event(world, path)
    value["body"] = object()
    assert_error(call(world, value), 401 if state == "expired" else 403,
                 "SESSION_EXPIRED" if state == "expired" else "SESSION_REVOKED")
    assert world.state.reads == [] and world.jobs.accepted == []


@pytest.mark.parametrize("path", ["/cpr-analysis", ATTEMPT_PATH])
def test_same_dummy_other_session_cannot_parse_or_submit_this_attempt(world, path, monkeypatch):
    import mock_journey.legacy_bridge as bridge

    def forbidden(*args, **kwargs):
        pytest.fail("Foreign attempt reached measurement parsing.")

    monkeypatch.setattr(bridge, "parse_measurement", forbidden)
    value = event(world, path)
    value["headers"]["Authorization"] = "Bearer " + world.other_token
    value["body"] = object()
    assert_error(call(world, value), 404, "NOT_FOUND")
    assert world.storage.saved == [] and world.jobs.accepted == []


def test_two_paths_share_one_accepted_attempt_and_typed_input_identity(world):
    first = call(world, event(world))
    second = call(world, event(world, ATTEMPT_PATH))
    assert first["statusCode"] == second["statusCode"] == 202
    assert json.loads(first["body"]) == json.loads(second["body"])
    assert len(world.jobs.accepted) == len(world.storage.saved) == 1
    assert json.loads(first["body"])["wait_expired"] is False


@pytest.mark.parametrize("path", ["/cpr-analysis", ATTEMPT_PATH])
def test_profile_type_change_is_not_accepted_as_the_same_condition(world, path):
    changed = {**CONDITION, "is_2rescuers": 0}
    value = multipart_event({"rawHexBPfile": comp_session(60), "condition": json.dumps(changed)})
    original = event(world, path)
    original["body"] = value["body"]
    assert_error(call(world, original), 409, "PROFILE_MISMATCH")
    assert world.storage.saved == [] and world.jobs.accepted == []


def test_alias_keeps_safe_legacy_input_error_while_attempt_keeps_mock_error(world):
    malformed = multipart_event({"condition": json.dumps(CONDITION)})
    alias = event(world)
    alias["body"] = malformed["body"]
    old = event(world, ATTEMPT_PATH)
    old["body"] = malformed["body"]
    response = call(world, alias)
    assert response["statusCode"] == 400
    assert json.loads(response["body"]) == {"type": "client_error", "message": "CPR file is required."}
    assert_error(call(world, old), 422, "MEASUREMENT_INPUT_INVALID")
    assert world.jobs.accepted == []


def test_session_expiry_during_input_save_does_not_report_a_successful_acceptance(world):
    world.storage.before_save = lambda: world.now.__setitem__(0, world.session["expires_at"])
    assert_error(call(world, event(world)), 401, "SESSION_EXPIRED")
    assert len(world.storage.saved) == 1
    assert world.jobs.accepted == []


@pytest.mark.parametrize("stage", ["test", "local", "dev", "beta", "prod"])
def test_public_lambda_entrypoint_has_no_environment_or_context_auth_bypass(world, stage, monkeypatch):
    import main
    import mock_journey.runtime as runtime

    def forbidden(*args, **kwargs):
        pytest.fail("Public entrypoint bypassed the authenticated attempt service.")

    monkeypatch.setenv("STAGE", stage)
    monkeypatch.setenv("ARC_MOCK_ENABLED", "true")
    monkeypatch.setattr(runtime, "get_application", lambda: world.service)
    monkeypatch.setattr(lambda_handler, "_run_trusted_calculation", forbidden)
    monkeypatch.setattr(main, "run_calculator", forbidden)
    value = event(world, authenticated=False)
    value["body"] = object()
    assert_error(lambda_handler.run(value, None), 401, "SESSION_REQUIRED")


def test_public_lambda_missing_runtime_is_fail_closed_without_sdk_or_legacy_fallback(world, monkeypatch):
    import mock_journey.runtime as runtime
    monkeypatch.setattr(runtime, "_application", None)
    monkeypatch.delenv("ARC_MOCK_ENABLED", raising=False)
    monkeypatch.setenv("STAGE", "test")
    # Global test fixture rejects every actual AWS client construction.
    assert_error(lambda_handler.run(event(world), None), 503, "TEMPORARILY_UNAVAILABLE")


def test_public_lambda_accepts_a_valid_authenticated_attempt_via_durable_service(world, monkeypatch):
    import mock_journey.runtime as runtime

    def forbidden(*args, **kwargs):
        pytest.fail("Public request reached unauthenticated compatibility helper.")

    monkeypatch.setattr(runtime, "get_application", lambda: world.service)
    monkeypatch.setattr(lambda_handler, "_run_trusted_calculation", forbidden)
    result = lambda_handler.run(event(world), CONTEXT)
    assert result["statusCode"] == 202
    assert json.loads(result["body"])["attempt_id"] == ATTEMPT
    assert len(world.storage.saved) == len(world.jobs.accepted) == 1


def test_measurement_exception_never_echoes_a_non_allowlisted_legacy_message(world, monkeypatch, capsys):
    import mock_journey.legacy_bridge as bridge

    def malicious_message(*args, **kwargs):
        raise bridge.MeasurementInputError(SECRET)

    monkeypatch.setattr(bridge, "parse_measurement", malicious_message)
    result = call(world, event(world))
    assert result["statusCode"] == 400
    assert json.loads(result["body"]) == {"type": "client_error", "message": "Invalid request data."}
    captured = capsys.readouterr()
    assert SECRET not in result["body"] + captured.out + captured.err
    assert world.storage.saved == [] and world.jobs.accepted == []


def test_unexpected_parser_exception_is_sanitized_and_never_accepted(world, monkeypatch, capsys):
    import mock_journey.legacy_bridge as bridge

    def failed(*args, **kwargs):
        raise RuntimeError(SECRET)

    monkeypatch.setattr(bridge, "parse_measurement", failed)
    result = call(world, event(world))
    assert_error(result, 503, "TEMPORARILY_UNAVAILABLE")
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err
    assert world.storage.saved == [] and world.jobs.accepted == []


def test_stored_result_get_is_read_only_and_does_not_accept_stale_submission_success(world, monkeypatch):
    attempt = world.state.attempts[ATTEMPT]
    attempt.update(state="evaluated", job_id="completed-job")
    world.jobs.rows["completed-job"] = {
        "state": "done", "attempt_id": ATTEMPT, "epoch": "test-epoch", "input_digest": "a" * 64,
        "adapter_version": "test-only-internal", "projection_version": "test-only-projection",
        "job_id": "completed-job", "call_id": "completed-call", "final_ref": {"key": "final"},
        "chart_publication": {"kind": "no_chart"},
    }

    def forbidden(*args, **kwargs):
        pytest.fail("Read-only result request submitted measurement again.")

    monkeypatch.setattr(world.calculation, "submit", forbidden)
    before = deepcopy(world.state.attempts)
    response = call(world, event(world, ATTEMPT_PATH, method="GET"))
    assert response["statusCode"] == 200
    result = json.loads(response["body"])
    assert result.pop("submit_arc") == {"status": "disabled", "ok": False, "error": "arc_contract_pending"}
    assert "submit_hstm" not in result
    assert type(result["value"]) is int and type(result["decimal"]) is float and result["nullable"] is None
    assert world.state.attempts == before and world.jobs.accepted == [] and world.storage.saved == []
    assert len(world.storage.reads) == 1

"""Authentication, public boundaries and secret non-disclosure; no AWS calls."""

from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from mock_journey.auth import AuthManager, extract_bearer
from mock_journey.catalog import Catalog, PROGRAMS, TARGETS
from mock_journey.errors import JourneyError
from mock_journey.handler import handle, run
from mock_journey.service import JourneyService


class SessionStore:
    def __init__(self):
        self.sessions = {}

    def create_session(self, session, slots):
        self.sessions[session["session_id"]] = deepcopy(session)

    def get_session(self, ident):
        return deepcopy(self.sessions.get(ident))


@pytest.fixture
def auth_setup():
    now = [1000]
    store = SessionStore()
    auth = AuthManager(store, "test-environment", {"v1": b"K" * 32}, "v1", clock=lambda: now[0])
    return store, auth, now


def expect(code, call):
    with pytest.raises(JourneyError) as failure:
        call()
    assert failure.value.code == code


@pytest.mark.parametrize("login,password", [
    ("TEST@test.com", "2222"), ("test@test.com ", "2222"), ("test@test.com", "2222 "),
    ("test@test.com", 2222), (None, "2222"), ("test@test.com", "wrong"),
])
def test_only_exact_dummy_credentials_create_a_session(auth_setup, login, password):
    store, auth, _ = auth_setup
    expect("LOGIN_FAILED", lambda: auth.login(login, password, Catalog().slot_keys))
    assert not store.sessions


def test_concurrent_identity_has_distinct_tokens_and_no_plaintext_persistence(auth_setup):
    store, auth, now = auth_setup
    first, token1 = auth.login("test@test.com", "2222", Catalog().slot_keys)
    second, token2 = auth.login("test@test.com", "2222", Catalog().slot_keys)
    assert first["principal"] == second["principal"]
    assert token1 != token2 and first["session_id"] != second["session_id"]
    assert first["expires_at"] - now[0] == 86400
    assert auth.authenticate(token1).session_id == first["session_id"]
    serialized = json.dumps(store.sessions)
    assert token1 not in serialized and token2 not in serialized and '"password"' not in serialized
    # A real UUID alone, or an attacker secret paired with that UUID, proves nothing.
    expect("SESSION_REQUIRED", lambda: auth.authenticate(first["session_id"]))
    bad = token1[:-1] + ("X" if token1[-1] != "X" else "Y")
    expect("SESSION_REQUIRED", lambda: auth.authenticate(bad))
    now[0] = first["expires_at"]
    expect("SESSION_EXPIRED", lambda: auth.authenticate(token1))
    assert store.sessions[first["session_id"]]["status"] == "active"


def test_logout_receipt_is_the_only_revoked_token_exception(auth_setup):
    store, auth, now = auth_setup
    session, token = auth.login("test@test.com", "2222", ())
    record = store.sessions[session["session_id"]]
    record.update(status="revoked", revision=1)
    expect("SESSION_REVOKED", lambda: auth.authenticate(token, allow_logout_receipt=True))
    record["logout_epoch"] = "reset-was-committed"
    now[0] = session["expires_at"] + 1
    expect("SESSION_REVOKED", lambda: auth.authenticate(token))
    assert auth.authenticate(token, allow_logout_receipt=True).revision == 1


@pytest.mark.parametrize("event", [
    {}, {"headers": {"Authorization": "Bearer "}},
    {"headers": {"Authorization": "Bearer token", "authorization": "Bearer token"}},
    {"multiValueHeaders": {"Authorization": ["Bearer token", "Bearer token"]}},
    {"headers": {"Authorization": "Bearer first"}, "multiValueHeaders": {"authorization": ["Bearer second"]}},
    {"headers": {"Authorization": "Bearer token extra"}},
    {"headers": {"Authorization": "Basic token"}},
    {"headers": {"Authorization": ["Bearer token"]}},
])
def test_ambiguous_or_missing_bearer_is_rejected(event):
    expect("SESSION_REQUIRED", lambda: extract_bearer(event))


def test_proxy_single_and_multi_header_representations_must_agree():
    assert extract_bearer({"headers": {"AUTHORIZATION": "bearer opaque"},
                           "multiValueHeaders": {"Authorization": ["bearer opaque"]}}) == "opaque"


def test_resume_proof_is_creator_attempt_environment_and_key_bound(auth_setup):
    _, auth, _ = auth_setup
    attempt = auth.prepare_resume({"principal": "tester", "attempt_id": "attempt-a", "creator_session_id": "creator"})
    proof = auth.resume_credential(attempt)
    assert proof not in json.dumps(attempt)
    assert auth.verify_resume(attempt, "tester", proof) == attempt["resume_digest"]
    for field in ("attempt_id", "principal", "creator_session_id", "resume_nonce"):
        altered = {**attempt, field: "different"}
        expect("NOT_FOUND", lambda: auth.verify_resume(altered, "tester", proof))
    another = AuthManager(None, "other-environment", auth.keys, "v1")
    expect("NOT_FOUND", lambda: another.verify_resume(attempt, "tester", proof))
    rotated = AuthManager(None, auth.environment, {"v1": b"K" * 32, "v2": b"J" * 32}, "v2")
    assert rotated.resume_credential(attempt) == proof
    del rotated.keys["v1"]
    expect("TEMPORARILY_UNAVAILABLE", lambda: rotated.resume_credential(attempt))
    expect("NOT_FOUND", lambda: rotated.verify_resume(attempt, "tester", "wrong-proof"))
    expect("TEMPORARILY_UNAVAILABLE", lambda: rotated.verify_resume(attempt, "tester", proof))


def event(method, path, body=None, token=None):
    result = {"httpMethod": method, "path": path, "headers": {}}
    if body is not None:
        result["body"] = body if isinstance(body, str) else json.dumps(body)
    if token:
        result["headers"]["Authorization"] = "Bearer " + token
    return result


def test_http_login_types_headers_and_authentication_precede_control_body_parsing(auth_setup):
    state, auth, _ = auth_setup
    service = JourneyService(state, auth, Catalog())
    ctx = SimpleNamespace(aws_request_id="request-a")
    response = handle(event("POST", "/mock/v1/sessions", {"login_id": "test@test.com", "password": "2222"}), ctx, service)
    assert response["statusCode"] == 201 and response["headers"]["Cache-Control"] == "no-store"
    body = json.loads(response["body"])
    assert type(body["expires_in"]) is int and body["expires_in"] == 86400
    assert "password" not in body and "token_hash" not in body
    malformed = handle(event("POST", "/mock/v1/attempts", "{not-json"), ctx, service)
    assert malformed["statusCode"] == 401
    malformed = handle(event("POST", "/mock/v1/attempts", "{not-json", body["session_token"]), ctx, service)
    assert malformed["statusCode"] == 400


@pytest.mark.parametrize("body", [
    '{"login_id":"test@test.com","password":"2222","password":"2222"}',
    '{"login_id":"test@test.com","password":NaN}',
    {"login_id": "test@test.com", "password": 2222},
    {"login_id": "test@test.com", "password": "2222", "token": "PRIVATE-MARKER"},
    "x" * 17000,
    {"login_id": "\ud800", "password": "2222"},
])
def test_new_control_schema_rejects_ambiguous_or_excess_input(auth_setup, body):
    state, auth, _ = auth_setup
    result = handle(event("POST", "/mock/v1/sessions", body), None, JourneyService(state, auth, Catalog()))
    assert result["statusCode"] == 400
    assert "PRIVATE-MARKER" not in result["body"] and not state.sessions


def test_errors_do_not_expose_credentials_or_raw_traceback(auth_setup, capsys):
    state, auth, _ = auth_setup
    def fail(*args):
        raise RuntimeError("PRIVATE-SESSION-MARKER")
    state.create_session = fail
    result = handle(event("POST", "/mock/v1/sessions", {"login_id": "test@test.com", "password": "2222"}),
                    None, JourneyService(state, auth, Catalog()))
    assert result["statusCode"] == 503
    assert "PRIVATE-SESSION-MARKER" not in result["body"] + capsys.readouterr().out


def test_runtime_is_disabled_without_explicit_configuration(monkeypatch):
    import mock_journey.runtime as runtime
    monkeypatch.setattr(runtime, "_application", None)
    monkeypatch.delenv("ARC_MOCK_ENABLED", raising=False)
    # Global conftest forbids creating any AWS client: fail-closed must precede that.
    response = run(event("POST", "/mock/v1/sessions", {"login_id": "test@test.com", "password": "2222"}), None)
    assert response["statusCode"] == 503


def test_excessive_json_nesting_is_an_input_error_before_any_command(auth_setup):
    state, auth, _ = auth_setup
    service = JourneyService(state, auth, Catalog())
    deep_value = "[" * 3000 + "0" + "]" * 3000
    body = '{"login_id":' + deep_value + ',"password":"2222"}'
    assert len(body.encode()) < 16384
    result = handle(event("POST", "/mock/v1/sessions", body), None, service)
    assert result["statusCode"] == 400
    assert json.loads(result["body"])["error"]["code"] == "INVALID_REQUEST"
    assert not state.sessions
    _, token = auth.login("test@test.com", "2222", Catalog().slot_keys)
    result = handle(event("POST", "/mock/v1/attempts", body, token), None, service)
    assert result["statusCode"] == 400
    assert json.loads(result["body"])["error"]["code"] == "INVALID_REQUEST"
    # SessionStore has no create/get-created command. Reaching it would be503.
    assert len(state.sessions) == 1
    assert handle(event("POST", "/mock/v1/attempts", body), None, service)["statusCode"] == 401


def test_catalog_all_fifteen_slots_and_no_unverified_runtime_definition():
    catalog = Catalog()
    slots = {key: {"completed": False, "open_attempts": 0} for key in catalog.slot_keys}
    slots["mock-cpr:infant"] = {"completed": True, "open_attempts": 0}
    view = catalog.programs_view({"epoch": "E1", "revision": 5, "slots": slots})
    assert len(catalog.slot_keys) == 15 and len(view["programs"]) == 5
    assert [p["goal"]["required"] for p in view["programs"]] == [3, 60, 8, 8, 10]
    assert view["programs"][0]["progress_by_target"]["infant"] == "completed"
    for program in PROGRAMS:
        for target in TARGETS:
            expect("CALCULATOR_CONTRACT_MISMATCH", lambda: catalog.definition(program[0], target))


def test_attempt_public_view_preserves_json_numeric_types_and_excludes_internal_secrets():
    definition = {"condition": {}, "calculation_profile": {"integer": 80, "float": 80.0, "null": None},
                  "goal": {"kind": "cycles", "required": 3}, "catalog_version": "v1", "profile_version": "v1"}
    attempt = {"attempt_id": "a", "state": "created", "program_id": "mock-cpr", "target": "adult",
               "epoch": "E1", "profile_name": "tester", "definition_json": json.dumps(definition),
               "resume_digest": "PRIVATE-DIGEST", "resume_nonce": "PRIVATE-NONCE", "bound_session_id": "PRIVATE-SESSION"}
    result = JourneyService.attempt_view(attempt)
    profile = result["calculation_profile"]
    assert type(profile["integer"]) is int and type(profile["float"]) is float and profile["null"] is None
    assert "missing" not in profile and "PRIVATE-" not in json.dumps(result)

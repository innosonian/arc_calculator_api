"""Authentication, public boundaries and secret non-disclosure; no AWS calls.

AuthManager is checked directly. The HTTP cases run on the public /api/v2
login and attempt routes (the /mock/v1 routes and their Bearer parser were
removed, D103); Authorization header ambiguity on v2 is covered in
tests/test_v2_calculation_boundary.py.
"""

from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from mock_journey.auth import AuthManager
from mock_journey.catalog import Catalog, PROGRAMS, TARGETS, definition_keys
from mock_journey.errors import JourneyError
from mock_journey.handler import run
from tests.journey_support import JourneyStore, V2Journey


class SessionStore:
    def __init__(self):
        self.sessions = {}

    def create_session(self, session):
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
    expect("LOGIN_FAILED", lambda: auth.login(login, password))
    assert not store.sessions


def test_concurrent_identity_has_distinct_tokens_and_no_plaintext_persistence(auth_setup):
    store, auth, now = auth_setup
    first, token1 = auth.login("test@test.com", "2222")
    second, token2 = auth.login("test@test.com", "2222")
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
    session, token = auth.login("test@test.com", "2222")
    record = store.sessions[session["session_id"]]
    record.update(status="revoked", revision=1)
    expect("SESSION_REVOKED", lambda: auth.authenticate(token, allow_logout_receipt=True))
    record["logout_epoch"] = "reset-was-committed"
    now[0] = session["expires_at"] + 1
    expect("SESSION_REVOKED", lambda: auth.authenticate(token))
    assert auth.authenticate(token, allow_logout_receipt=True).revision == 1


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


def test_catalog_has_fifteen_definition_keys_and_no_unverified_runtime_definition():
    catalog = Catalog()
    assert len(definition_keys()) == 15
    for program in PROGRAMS:
        for target in TARGETS:
            expect("CALCULATOR_CONTRACT_MISMATCH", lambda: catalog.definition(program[0], target))


# -- /api/v2 HTTP boundary -----------------------------------------------------------------

LOGIN = {"loginId": "test@test.com", "password": "2222"}


@pytest.fixture
def h():
    return V2Journey(JourneyStore.memory())


def event(method, path, body=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    result = {"httpMethod": method, "path": path, "headers": headers}
    if body is not None:
        result["body"] = body if isinstance(body, str) else json.dumps(body)
    return result


def call(h, value):
    reply = h.call(value["httpMethod"], value["path"], event=value)
    return reply.status, reply.body, json.dumps(reply.raw)


def sessions(h):
    return [row for row in h.store.rows() if row["PK"].startswith(("SESSION#", "USER#"))]


def test_http_login_types_headers_and_authentication_precede_control_body_parsing(h):
    reply = h.call("POST", "/api/v2/sessions/", event=event("POST", "/api/v2/sessions/", LOGIN))
    assert reply.status == 201 and reply.headers["Cache-Control"] == "no-store"
    raw = json.dumps(reply.raw)
    data = reply.data
    assert type(data["accessToken"]) is str and type(data["expiresAt"]) is str
    assert "password" not in raw and "token_hash" not in raw and "2222" not in raw
    status, body, _ = call(h, event("POST", "/api/v2/attempts/", "{not-json"))
    assert (status, body["error"]["code"]) == (401, "SESSION_REQUIRED")
    status, body, _ = call(h, event("POST", "/api/v2/attempts/", "{not-json", data["accessToken"]))
    assert (status, body["error"]["code"]) == (400, "INVALID_REQUEST")


@pytest.mark.parametrize("body,status", [
    ('{"loginId":"test@test.com","password":"2222","password":"2222"}', 400),
    ('{"loginId":"test@test.com","password":NaN}', 400),
    ({"loginId": "test@test.com", "password": 2222}, 400),
    ({"loginId": "test@test.com", "password": "2222", "token": "PRIVATE-MARKER"}, 400),
    ({"loginId": "\ud800", "password": "2222"}, 400),
    ("x" * 17000, 413),
], ids=["duplicate_key", "nan", "number", "extra_field", "surrogate", "oversized"])
def test_control_schema_rejects_ambiguous_or_excess_login_input(h, body, status):
    got, parsed, raw = call(h, event("POST", "/api/v2/sessions/", body))
    assert got == status
    assert parsed["error"]["code"] == ("PAYLOAD_TOO_LARGE" if status == 413 else "INVALID_REQUEST")
    assert "PRIVATE-MARKER" not in raw and sessions(h) == []


def test_errors_do_not_expose_credentials_or_raw_traceback(h, capsys):
    def fail(*args):
        raise RuntimeError("PRIVATE-SESSION-MARKER")

    h.api.state.create_session = fail
    status, body, raw = call(h, event("POST", "/api/v2/sessions/", LOGIN))
    assert (status, body["error"]["code"]) == (503, "TEMPORARILY_UNAVAILABLE")
    captured = capsys.readouterr()
    assert "PRIVATE-SESSION-MARKER" not in raw + captured.out + captured.err
    assert sessions(h) == []


def test_runtime_is_disabled_without_explicit_configuration(monkeypatch):
    import mock_journey.runtime as runtime
    monkeypatch.setattr(runtime, "_application", None)
    monkeypatch.delenv("ARC_MOCK_ENABLED", raising=False)
    # Global conftest forbids creating any AWS client: fail-closed must precede that.
    response = run(event("POST", "/api/v2/sessions/", LOGIN), SimpleNamespace(aws_request_id="request-a"))
    assert response["statusCode"] == 503
    assert json.loads(response["body"])["error"]["code"] == "TEMPORARILY_UNAVAILABLE"


def test_excessive_json_nesting_is_an_input_error_before_any_command(h):
    deep_value = "[" * 3000 + "0" + "]" * 3000
    body = '{"loginId":' + deep_value + ',"password":"2222"}'
    assert len(body.encode()) < 16384
    status, parsed, _ = call(h, event("POST", "/api/v2/sessions/", body))
    assert (status, parsed["error"]["code"]) == (400, "INVALID_REQUEST")
    assert sessions(h) == []
    token = h.login().token
    rows = h.store.rows()
    status, parsed, _ = call(h, event("POST", "/api/v2/attempts/", body, token))
    assert (status, parsed["error"]["code"]) == (400, "INVALID_REQUEST")
    assert h.store.rows() == rows
    status, parsed, _ = call(h, event("POST", "/api/v2/attempts/", body))
    assert (status, parsed["error"]["code"]) == (401, "SESSION_REQUIRED")

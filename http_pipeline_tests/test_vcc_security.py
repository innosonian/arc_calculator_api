"""Auth, sanitization, and cancel mapping on course_v2 HTTP."""

import json
import hashlib
import secrets
from types import SimpleNamespace
import uuid

from mock_journey.handler import handle
from tests.vcc_support import event
from integration_tests.test_vcc_state_dynamodb import application, login
from tests.vcc_runtime_support import runtime, start_attempt


CONTEXT = SimpleNamespace(aws_request_id="vcc-sec")


def test_foreign_session_is_not_found(dynamodb_client, dynamodb_table):
    env = runtime(dynamodb_client, dynamodb_table)
    attempt = start_attempt(env, 1003)
    session_id = str(uuid.uuid4())
    other_token = f"s1.{session_id}.{secrets.token_urlsafe(32)}"
    env.app.state.create_session({"session_id": session_id, "principal": env.auth.principal,
        "token_hash": hashlib.sha256(other_token.encode()).hexdigest(), "issued_at": env.now[0],
        "expires_at": env.now[0]+86400, "status": "active", "revision": 0}, env.app.catalog.slot_keys)
    listed = handle(event("GET", "/api/v2/courses/progress/", token=other_token, query={"page": "1", "pageSize": "10"}),
                    CONTEXT, env.app)
    assert listed["statusCode"] == 200
    for suffix in ("", "calculation/", "chart-link/"):
        response = handle(event("GET", f"/api/v2/attempts/{attempt['attempt_id']}/{suffix}", token=other_token),
                          CONTEXT, env.app)
        assert response["statusCode"] == 404
        assert json.loads(response["body"])["error"]["code"] == "NOT_FOUND"
    assert env.app.state.get_attempt(env.auth, attempt["attempt_id"])["state"] == "created"


def test_invalid_token_is_sanitized(dynamodb_client, dynamodb_table):
    app = application(dynamodb_client, dynamodb_table)
    rejected = handle(event("GET", "/api/v2/session/", token="s1.00000000-0000-4000-8000-000000000099.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"),
                      CONTEXT, app)
    assert rejected["statusCode"] == 401
    body = json.loads(rejected["body"])
    assert "aaaaaaaa" not in json.dumps(body)


def test_cancel_reason_mapping_rejects_unknown(dynamodb_client, dynamodb_table):
    env = runtime(dynamodb_client, dynamodb_table)
    attempt = start_attempt(env, 1003)
    response = handle(event(
        "POST", f"/api/v2/attempts/{attempt['attempt_id']}/cancel/",
        token=env.token, body={"reason": "network_error"},
    ), CONTEXT, env.app)
    assert response["statusCode"] == 400
    payload = json.loads(response["body"])
    assert payload["error"]["code"] == "INVALID_REQUEST"
    assert "network_error" not in payload["error"]["message"]
    assert env.app.state.get_attempt(env.auth, attempt["attempt_id"]) == attempt

"""HTTP commands through actual DynamoDB, using test-only execution definitions."""

import json
from types import SimpleNamespace

import pytest

from mock_journey.auth import AuthManager
from mock_journey.catalog import Catalog
from mock_journey.handler import handle
from mock_journey.service import JourneyService
from mock_journey.state import DynamoStateRepository


class TestDefinitions:
    """Deliberately incomplete wire fixture: never installed in runtime."""
    __test__ = False

    def get_definition(self, program_id, target):
        return {"condition": {"target": target}, "calculation_profile": {"integer": 80, "float": 80.0, "nullable": None},
                "profile_version": "test-only-p1", "adapter_version": "test-only-a1", "projection_version": "test-only-v1"}


@pytest.fixture
def http_service(dynamodb_client, dynamodb_table):
    now = [1000]
    state = DynamoStateRepository(dynamodb_client, dynamodb_table, clock=lambda: now[0])
    auth = AuthManager(state, "local-test", {"key1": b"T" * 32}, "key1", clock=lambda: now[0])
    return JourneyService(state, auth, Catalog(TestDefinitions())), now


def call(service, method, path, body=None, token=None):
    event = {"httpMethod": method, "path": "/mock/v1/" + path, "headers": {}}
    if body is not None:
        event["body"] = json.dumps(body)
    if token:
        event["headers"]["Authorization"] = "Bearer " + token
    result = handle(event, SimpleNamespace(aws_request_id="local-request"), service)
    return result["statusCode"], json.loads(result["body"]) if result["body"] else None


def login(service):
    status, result = call(service, "POST", "sessions", {"login_id": "test@test.com", "password": "2222"})
    assert status == 201
    return result["session_token"]


BODY = {"client_request_id": "client-one", "catalog_version": "mock-catalog-v1", "program_id": "mock-cpr", "target": "infant"}


def test_real_http_replay_logout_receipt_shared_epoch_and_cross_session_privacy(http_service):
    service, _ = http_service
    token_a, token_b = login(service), login(service)
    status, attempt = call(service, "POST", "attempts", BODY, token_a)
    assert status == 201
    path = "attempts/" + attempt["attempt_id"]
    # A provider outage/change after commit must not make the acknowledged
    # identity unrecoverable after response loss.
    service.catalog.execution_definitions = None
    service.catalog.version = "later-catalog-not-used-for-replay"
    status, replay = call(service, "POST", "attempts", BODY, token_a)
    assert status == 200 and replay == attempt
    status, conflict = call(service, "POST", "attempts", {**BODY, "target": "adult"}, token_a)
    assert status == 409 and conflict["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert call(service, "GET", path, token=token_b)[0] == 404
    status, before = call(service, "GET", "programs", token=token_b)
    assert status == 200 and before["programs"][0]["active_attempts_by_target"]["infant"] == 1
    assert call(service, "DELETE", "session", token=token_a)[0] == 204
    status, after = call(service, "GET", "programs", token=token_b)
    assert status == 200 and after["progress_epoch"] != before["progress_epoch"]
    assert after["programs"][0]["active_attempts_by_target"]["infant"] == 0
    assert call(service, "DELETE", "session", token=token_a)[0] == 204
    assert call(service, "GET", "programs", token=token_b)[1] == after
    assert call(service, "GET", "session", token=token_a)[0] == 403
    # Valid proof rebinds old epoch without reviving progress.
    status, rebound = call(service, "POST", path + "/reauthorize", {"resume_credential": attempt["resume_credential"]}, token_b)
    assert status == 200 and rebound["progress_epoch"] == before["progress_epoch"]
    assert call(service, "POST", path + "/cancel", {"reason": "manikin_disconnected"}, token_b)[0] == 204
    assert call(service, "POST", path + "/cancel", {"reason": "manikin_disconnected"}, token_b)[0] == 204
    assert call(service, "GET", "programs", token=token_b)[1] == after


def test_expiry_requires_proof_and_keeps_attempt_types_and_identity(http_service):
    service, now = http_service
    old = login(service)
    status, attempt = call(service, "POST", "attempts", BODY, old)
    assert status == 201
    path = "attempts/" + attempt["attempt_id"]
    now[0] += 86400
    assert call(service, "GET", path, token=old)[0] == 401
    new = login(service)
    status, progress = call(service, "GET", "programs", token=new)
    assert status == 200 and progress["progress_epoch"] == attempt["progress_epoch"]
    assert call(service, "GET", path, token=new)[0] == 404
    assert call(service, "POST", path + "/reauthorize", {"resume_credential": "wrong"}, new)[0] == 404
    status, rebound = call(service, "POST", path + "/reauthorize", {"resume_credential": attempt["resume_credential"]}, new)
    assert status == 200
    assert rebound == {key: value for key, value in attempt.items() if key != "resume_credential"}
    profile = rebound["calculation_profile"]
    assert type(profile["integer"]) is int and type(profile["float"]) is float and profile["nullable"] is None
    assert call(service, "GET", path, token=new)[1] == rebound

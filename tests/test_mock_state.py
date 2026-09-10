"""Control-flow checks; real condition semantics are tested in DynamoDB Local."""

from copy import deepcopy
import json

from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError
import pytest

from mock_journey.errors import JourneyError
from mock_journey.models import AuthContext
from mock_journey.state import DynamoStateRepository


SERIALIZER = TypeSerializer()
SESSION = {
    "PK": "SESSION#session-a", "SK": "AUTH", "session_id": "session-a", "principal": "dummy",
    "token_hash": "hash-only", "issued_at": 10, "expires_at": 86410, "status": "active", "revision": 0,
}
AUTH = AuthContext("session-a", "dummy", 0, 86410)
USER = {
    "PK": "USER#dummy", "SK": "STATE", "principal": "dummy", "epoch": "epoch-a", "revision": 0,
    "slots": {"mock-cpr:adult": {
        "completed": False, "completed_by_attempt": None, "completed_at": None, "open_attempts": 0,
    }},
}
DEFINITION = '{"condition":{"target":"adult"},"calculation_profile":{"integer":80,"float":80.0,"null":null}}'
TEMPLATE = {
    "attempt_id": "attempt-a", "principal": "dummy", "creator_session_id": "session-a",
    "bound_session_id": "session-a", "program_id": "mock-cpr", "target": "adult",
    "profile_name": "tester", "definition_json": DEFINITION,
    "resume_nonce": "nonce", "resume_key_version": "v1", "resume_digest": "digest-only",
}


def item(value):
    return {key: SERIALIZER.serialize(nested) for key, nested in value.items()}


def snapshot(*values):
    return {"Responses": [{"Item": item(value)} if value else {} for value in values]}


def conflict(code="ConditionalCheckFailed"):
    return ClientError({
        "Error": {"Code": "TransactionCanceledException", "Message": "PRIVATE-CONFLICT-MARKER"},
        "CancellationReasons": [{"Code": code}],
    }, "TransactWriteItems")


class ScriptedClient:
    """Assert finite SDK calls without implementing DynamoDB expressions."""

    def __init__(self, calls):
        self.expected = list(calls)
        self.calls = []

    def _call(self, operation, kwargs):
        self.calls.append((operation, deepcopy(kwargs)))
        if not self.expected:
            pytest.fail(f"Unexpected {operation} call")
        expected, response = self.expected.pop(0)
        assert operation == expected
        if isinstance(response, BaseException):
            raise response
        return response

    def get_item(self, **kwargs):
        return self._call("get", kwargs)

    def put_item(self, **kwargs):
        return self._call("put", kwargs)

    def transact_get_items(self, **kwargs):
        return self._call("read", kwargs)

    def transact_write_items(self, **kwargs):
        return self._call("write", kwargs)


def repository(calls, **kwargs):
    client = ScriptedClient(calls)
    return DynamoStateRepository(client, "state-test", clock=lambda: 20, **kwargs), client


def assert_code(code, operation):
    with pytest.raises(JourneyError) as error:
        operation()
    assert error.value.code == code
    return error.value


def attempt(**changes):
    return {
        "PK": "ATTEMPT#attempt-a", "SK": "META", **TEMPLATE,
        "epoch": "epoch-a", "created_at": 20, "state": "created", "revision": 0,
        "active_counted": True, "evaluation": None, "progress_application": None, **changes,
    }


def test_session_lookup_is_strong_and_does_not_return_decimal_controls():
    repo, client = repository([("get", {"Item": item(SESSION)})])
    stored = repo.get_session("session-a")
    assert type(stored["issued_at"]) is int
    assert stored == SESSION
    assert client.calls[0][1]["ConsistentRead"] is True


@pytest.mark.parametrize("stored,code", [
    (None, "SESSION_REQUIRED"),
    ({**SESSION, "status": "revoked"}, "SESSION_REVOKED"),
    ({**SESSION, "expires_at": 20}, "SESSION_EXPIRED"),
    ({**SESSION, "revision": 1}, "SESSION_REQUIRED"),
    ({**SESSION, "principal": "other"}, "SESSION_REQUIRED"),
])
def test_progress_snapshot_rechecks_authentication(stored, code):
    repo, client = repository([("read", snapshot(stored, USER))])
    assert_code(code, lambda: repo.get_progress(AUTH))
    assert len(client.calls) == 1


def test_create_preserves_definition_string_and_uses_one_action_per_item():
    repo, client = repository([("read", snapshot(SESSION, USER, None)), ("write", {})])
    result = repo.create_attempt(AUTH, "request-a", "request-digest", TEMPLATE)
    assert result["definition_json"] == DEFINITION
    profile = json.loads(result["definition_json"])["calculation_profile"]
    assert type(profile["integer"]) is int and type(profile["float"]) is float
    assert result["epoch"] == USER["epoch"] and result["active_counted"] is True
    actions = client.calls[1][1]["TransactItems"]
    keys = []
    for action in actions:
        body = next(iter(action.values()))
        record = body.get("Key", body.get("Item"))
        keys.append((record["PK"]["S"], record["SK"]["S"]))
    assert len(keys) == len(set(keys)) == 4
    assert {key[0].split("#", 1)[0] for key in keys} == {"SESSION", "USER", "ATTEMPT"}
    assert "PRIVATE" not in repr(actions)


def test_create_duplicate_uses_stored_attempt_without_any_write():
    stored = attempt(attempt_id="winner", PK="ATTEMPT#winner")
    repo, client = repository([
        ("read", snapshot(SESSION, USER, {"request_digest": "same", "attempt_id": "winner"})),
        ("read", snapshot(SESSION, stored)),
    ])
    assert repo.create_attempt(AUTH, "request-a", "same", TEMPLATE)["attempt_id"] == "winner"
    assert all(operation == "read" for operation, _ in client.calls)


def test_conflicting_request_id_is_not_a_new_attempt():
    repo, client = repository([("read", snapshot(
        SESSION, USER, {"request_digest": "other", "attempt_id": "winner"},
    ))])
    assert_code("IDEMPOTENCY_CONFLICT", lambda: repo.create_attempt(AUTH, "request-a", "new", TEMPLATE))
    assert len(client.calls) == 1


def test_early_creation_replay_returns_frozen_definition_with_fresh_bound_auth():
    stored = attempt()
    repo, client = repository([
        ("read", snapshot(SESSION, {"request_digest": "same", "attempt_id": "attempt-a"})),
        ("read", snapshot(SESSION, stored)),
    ])
    result = repo.get_created_attempt(AUTH, "request-a", "same")
    assert result["definition_json"] == DEFINITION
    assert len(client.calls) == 2 and all(kind == "read" for kind, _ in client.calls)


def test_early_creation_replay_rejects_changed_input_before_provider_access():
    repo, client = repository([
        ("read", snapshot(SESSION, {"request_digest": "old", "attempt_id": "attempt-a"})),
    ])
    assert_code("IDEMPOTENCY_CONFLICT", lambda: repo.get_created_attempt(AUTH, "request-a", "new"))
    assert len(client.calls) == 1


def test_early_creation_replay_does_not_bypass_revocation_between_reads():
    repo, client = repository([
        ("read", snapshot(SESSION, {"request_digest": "same", "attempt_id": "attempt-a"})),
        ("read", snapshot({**SESSION, "status": "revoked"}, attempt())),
    ])
    assert_code("SESSION_REVOKED", lambda: repo.get_created_attempt(AUTH, "request-a", "same"))


def test_early_creation_replay_miss_rechecks_current_auth():
    repo, client = repository([("read", snapshot({**SESSION, "expires_at": 20}, None))])
    assert_code("SESSION_EXPIRED", lambda: repo.get_created_attempt(AUTH, "request-a", "same"))


def test_conditional_contention_has_a_bounded_retry_budget():
    calls = [("read", snapshot(SESSION, USER, None)), ("write", conflict())] * 8
    repo, client = repository(calls)
    error = assert_code("TEMPORARILY_UNAVAILABLE", lambda: repo.create_attempt(AUTH, "request-a", "digest", TEMPLATE))
    assert len(client.calls) == 16
    assert "PRIVATE" not in str(error)


def test_read_and_write_conflicts_share_one_command_retry_budget():
    calls = [("read", conflict("TransactionConflict"))] * 7
    calls += [("read", snapshot(SESSION, USER, None)), ("write", conflict())]
    repo, client = repository(calls)
    assert_code("TEMPORARILY_UNAVAILABLE", lambda: repo.create_attempt(AUTH, "request-a", "digest", TEMPLATE))
    assert len(client.calls) == 9


def test_read_conflict_can_recover_without_converting_authorization_errors_to_success():
    repo, client = repository([
        ("read", conflict("TransactionConflict")),
        ("read", snapshot({**SESSION, "status": "revoked"}, USER)),
    ])
    assert_code("SESSION_REVOKED", lambda: repo.get_progress(AUTH))
    assert len(client.calls) == 2


@pytest.mark.parametrize("code", ["ValidationError", "ProvisionedThroughputExceeded", "ItemCollectionSizeLimitExceeded"])
def test_nonconditional_transaction_errors_are_not_retried_as_contention(code):
    repo, client = repository([("read", snapshot(SESSION, USER, None)), ("write", conflict(code))])
    assert_code("TEMPORARILY_UNAVAILABLE", lambda: repo.create_attempt(AUTH, "request-a", "digest", TEMPLATE))
    assert len(client.calls) == 2


def test_expiry_is_checked_again_after_a_transaction_conflict():
    client = ScriptedClient([
        ("read", snapshot(SESSION, USER, None)), ("write", conflict()),
        ("read", snapshot(SESSION, USER, None)),
    ])
    ticks = iter([20, 20, 86410])
    repo = DynamoStateRepository(client, "state-test", clock=lambda: next(ticks))
    assert_code("SESSION_EXPIRED", lambda: repo.create_attempt(AUTH, "request-a", "digest", TEMPLATE))
    assert len(client.calls) == 3


def test_logout_receipt_replay_does_not_reset_later_progress():
    revoked = {**SESSION, "status": "revoked", "revision": 1, "logout_epoch": "epoch-new"}
    repo, client = repository([("read", snapshot(revoked, USER))])
    assert repo.logout(AUTH) is None
    assert [operation for operation, _ in client.calls] == ["read"]


def test_old_epoch_cancel_only_checks_the_new_progress_map():
    stored = attempt(epoch="old-epoch")
    repo, client = repository([("read", snapshot(SESSION, stored, USER)), ("write", {})])
    repo.cancel_attempt(AUTH, "attempt-a", "user_cancelled")
    actions = client.calls[1][1]["TransactItems"]
    user_action = [a for a in actions if next(iter(a.values())).get("Key", {}).get("PK", {}).get("S") == "USER#dummy"]
    assert len(user_action) == 1 and "ConditionCheck" in user_action[0]


def test_another_bound_session_cannot_access_the_attempt():
    repo, client = repository([("read", snapshot(SESSION, attempt(bound_session_id="session-b")))])
    assert_code("NOT_FOUND", lambda: repo.get_attempt(AUTH, "attempt-a"))


def test_cancelled_attempt_is_not_resurrected_by_reauthorization():
    repo, client = repository([("read", snapshot(SESSION, attempt(state="cancelled")))])
    assert_code("INVALID_STATE", lambda: repo.reauthorize_attempt(AUTH, "attempt-a", "digest-only"))
    assert len(client.calls) == 1


def test_missing_bound_session_is_not_assumed_expired():
    repo, client = repository([
        ("read", snapshot(SESSION, attempt(bound_session_id="session-old"))), ("get", {}),
    ])
    assert_code("TEMPORARILY_UNAVAILABLE", lambda: repo.reauthorize_attempt(AUTH, "attempt-a", "digest-only"))
    assert len(client.calls) == 2


def test_session_creation_never_copies_extra_secret_fields_to_dynamo():
    repo, client = repository([("put", {}), ("write", {})])
    repo.create_session({**SESSION, "session_token": "PRIVATE-BEARER", "password": "PRIVATE-PASSWORD"}, ["mock-cpr:adult"])
    assert "PRIVATE" not in repr(client.calls)
    assert client.calls[0][1]["ConditionExpression"] == "attribute_not_exists(PK)"


def test_state_rejects_noninteger_numbers_without_coercing_profile_json():
    repo, client = repository([("get", {"Item": {**item(SESSION), "revision": {"N": "1.5"}}})])
    assert_code("TEMPORARILY_UNAVAILABLE", lambda: repo.get_session("session-a"))

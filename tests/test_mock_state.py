"""Control-flow checks; real condition semantics are tested in DynamoDB Local.

The removed v1 attempt creation (create_attempt/get_created_attempt, D103) is
gone; the retry-budget and recheck properties it carried are asserted on the
logout transaction, which has the same read/condition/retry structure. USER
rows are checked both without slots (every row created now, Q10) and with the
slots of a stored legacy row (R1-R3).
"""

from copy import deepcopy

from botocore.exceptions import ClientError
import pytest

from mock_journey.errors import JourneyError
from mock_journey.models import AuthContext
from mock_journey.state import DynamoStateRepository, _decode
from tests.mock_state_support import ScriptedClient, item, snapshot  # noqa: F401 (re-export)


SESSION = {
    "PK": "SESSION#session-a", "SK": "AUTH", "session_id": "session-a", "principal": "dummy",
    "token_hash": "hash-only", "issued_at": 10, "expires_at": 86410, "status": "active", "revision": 0,
}
AUTH = AuthContext("session-a", "dummy", 0, 86410)
# A stored legacy (pre-D103) USER row with slots.
USER = {
    "PK": "USER#dummy", "SK": "STATE", "principal": "dummy", "epoch": "epoch-a", "revision": 0,
    "slots": {"mock-cpr:adult": {
        "completed": False, "completed_by_attempt": None, "completed_at": None, "open_attempts": 0,
    }},
}
# A USER row as create_session writes it now: no slots.
NEW_USER = {key: value for key, value in USER.items() if key != "slots"}
DEFINITION = '{"condition":{"target":"adult"},"calculation_profile":{"integer":80,"float":80.0,"null":null}}'
# The stored fields of a legacy (pre-D103) attempt row.
TEMPLATE = {
    "attempt_id": "attempt-a", "principal": "dummy", "creator_session_id": "session-a",
    "bound_session_id": "session-a", "program_id": "mock-cpr", "target": "adult",
    "profile_name": "tester", "definition_json": DEFINITION,
    "resume_nonce": "nonce", "resume_key_version": "v1", "resume_digest": "digest-only",
}


def conflict(code="ConditionalCheckFailed"):
    return ClientError({
        "Error": {"Code": "TransactionCanceledException", "Message": "PRIVATE-CONFLICT-MARKER"},
        "CancellationReasons": [{"Code": code}],
    }, "TransactWriteItems")


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


def written_user(client):
    """The USER row put by the last transaction write."""
    actions = client.calls[-1][1]["TransactItems"]
    puts = [a["Put"]["Item"] for a in actions if "Put" in a and a["Put"]["Item"]["PK"]["S"] == "USER#dummy"]
    assert len(puts) == 1
    return _decode(puts[0])


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
def test_session_user_snapshot_rechecks_authentication(stored, code):
    repo, client = repository([("read", snapshot(stored, USER))])
    assert_code(code, lambda: repo.read_session_user(AUTH))
    assert len(client.calls) == 1


@pytest.mark.parametrize("user", [NEW_USER, USER], ids=["without_slots", "legacy_slots"])
def test_session_user_needs_principal_epoch_and_revision_only(user):
    repo, client = repository([("read", snapshot(SESSION, user))])
    assert repo.read_session_user(AUTH) == user
    assert [operation for operation, _ in client.calls] == ["read"]


@pytest.mark.parametrize("slots", [None, [], "slots", {"mock-cpr:adult": None}],
                         ids=["null", "list", "string", "malformed_slot"])
def test_session_user_ignores_a_malformed_legacy_slots_value(slots):
    """R1: a slots value is neither required nor validated outside the legacy paths.

    Before D103 such a row was 503 here. The legacy paths that use slots still
    refuse it: logout (test_logout_refuses_a_malformed_legacy_slots_value_without_writing),
    legacy cancel and legacy finalize/failure (_slot, tests/test_job_restart_limit.py).
    """
    user = {**NEW_USER, "slots": slots}
    repo, client = repository([("read", snapshot(SESSION, user))])
    assert repo.read_session_user(AUTH) == user
    assert [operation for operation, _ in client.calls] == ["read"]


@pytest.mark.parametrize("broken", [
    None, {**NEW_USER, "principal": "other"}, {**NEW_USER, "revision": "0"}, {**NEW_USER, "epoch": ""},
    {key: value for key, value in NEW_USER.items() if key != "epoch"},
], ids=["missing", "principal", "revision_type", "empty_epoch", "no_epoch"])
def test_session_user_rejects_an_unusable_user_row(broken):
    repo, client = repository([("read", snapshot(SESSION, broken))])
    assert_code("TEMPORARILY_UNAVAILABLE", lambda: repo.read_session_user(AUTH))


def test_conditional_contention_has_a_bounded_retry_budget():
    calls = [("read", snapshot(SESSION, USER)), ("write", conflict())] * 8
    repo, client = repository(calls)
    error = assert_code("TEMPORARILY_UNAVAILABLE", lambda: repo.logout(AUTH))
    assert len(client.calls) == 16
    assert "PRIVATE" not in str(error)


def test_read_and_write_conflicts_share_one_command_retry_budget():
    calls = [("read", conflict("TransactionConflict"))] * 7
    calls += [("read", snapshot(SESSION, USER)), ("write", conflict())]
    repo, client = repository(calls)
    assert_code("TEMPORARILY_UNAVAILABLE", lambda: repo.logout(AUTH))
    assert len(client.calls) == 9


def test_read_conflict_can_recover_without_converting_authorization_errors_to_success():
    repo, client = repository([
        ("read", conflict("TransactionConflict")),
        ("read", snapshot({**SESSION, "status": "revoked"}, USER)),
    ])
    assert_code("SESSION_REVOKED", lambda: repo.read_session_user(AUTH))
    assert len(client.calls) == 2


@pytest.mark.parametrize("code", ["ValidationError", "ProvisionedThroughputExceeded", "ItemCollectionSizeLimitExceeded"])
def test_nonconditional_transaction_errors_are_not_retried_as_contention(code):
    repo, client = repository([("read", snapshot(SESSION, USER)), ("write", conflict(code))])
    assert_code("TEMPORARILY_UNAVAILABLE", lambda: repo.logout(AUTH))
    assert len(client.calls) == 2


def test_expiry_is_checked_again_after_a_transaction_conflict():
    client = ScriptedClient([
        ("read", snapshot(SESSION, USER)), ("write", conflict()),
        ("read", snapshot(SESSION, USER)),
    ])
    ticks = iter([20, 20, 86410])
    repo = DynamoStateRepository(client, "state-test", clock=lambda: next(ticks))
    assert_code("SESSION_EXPIRED", lambda: repo.logout(AUTH))
    assert len(client.calls) == 3


def test_logout_receipt_replay_does_not_reset_later_progress():
    revoked = {**SESSION, "status": "revoked", "revision": 1, "logout_epoch": "epoch-new"}
    repo, client = repository([("read", snapshot(revoked, USER))])
    assert repo.logout(AUTH) is None
    assert [operation for operation, _ in client.calls] == ["read"]


def test_logout_of_a_row_without_slots_never_adds_slots():
    repo, client = repository([("read", snapshot(SESSION, NEW_USER)), ("write", {})])
    repo.logout(AUTH)
    user = written_user(client)
    assert "slots" not in user
    assert user["epoch"] != NEW_USER["epoch"] and user["revision"] == NEW_USER["revision"] + 1
    unchanged = ("PK", "SK", "principal")
    assert {key: user[key] for key in unchanged} == {key: NEW_USER[key] for key in unchanged}
    assert set(user) == set(NEW_USER) | {"updated_at"}


def test_logout_of_a_legacy_row_resets_its_slots_as_before():
    legacy = deepcopy(USER)
    legacy["slots"]["mock-cpr:adult"] = {"completed": True, "completed_by_attempt": "attempt-a",
                                        "completed_at": 15, "open_attempts": 2}
    legacy["slots"]["mock-cpr:child"] = {"completed": False, "completed_by_attempt": None,
                                        "completed_at": None, "open_attempts": 1}
    legacy["legacy_field"] = "kept"
    repo, client = repository([("read", snapshot(SESSION, legacy)), ("write", {})])
    repo.logout(AUTH)
    user = written_user(client)
    reset = {"completed": False, "completed_by_attempt": None, "completed_at": None, "open_attempts": 0}
    assert user["slots"] == {"mock-cpr:adult": reset, "mock-cpr:child": reset}
    assert user["legacy_field"] == "kept"


@pytest.mark.parametrize("slots", [None, [], "slots"], ids=["null", "list", "string"])
def test_logout_refuses_a_malformed_legacy_slots_value_without_writing(slots):
    repo, client = repository([("read", snapshot(SESSION, {**NEW_USER, "slots": slots}))])
    assert_code("TEMPORARILY_UNAVAILABLE", lambda: repo.logout(AUTH))
    assert [operation for operation, _ in client.calls] == ["read"]


def test_old_epoch_cancel_only_checks_the_new_progress_map():
    stored = attempt(epoch="old-epoch")
    repo, client = repository([("read", snapshot(SESSION, stored, USER)), ("write", {})])
    repo.cancel_attempt(AUTH, "attempt-a", "user_cancelled")
    actions = client.calls[1][1]["TransactItems"]
    user_action = [a for a in actions if next(iter(a.values())).get("Key", {}).get("PK", {}).get("S") == "USER#dummy"]
    assert len(user_action) == 1 and "ConditionCheck" in user_action[0]


def test_current_epoch_legacy_cancel_releases_only_its_slot_count():
    counted = deepcopy(USER)
    counted["slots"]["mock-cpr:adult"]["open_attempts"] = 1
    repo, client = repository([("read", snapshot(SESSION, attempt(), counted)), ("write", {})])
    repo.cancel_attempt(AUTH, "attempt-a", "user_stopped")
    user = written_user(client)
    assert user["slots"] == USER["slots"] and user["revision"] == counted["revision"] + 1


@pytest.mark.parametrize("user", [NEW_USER, {**NEW_USER, "slots": None}, {**NEW_USER, "slots": []},
                                  {**NEW_USER, "slots": "slots"}, {**NEW_USER, "slots": {}}],
                         ids=["absent", "null", "list", "string", "no_such_slot"])
def test_legacy_cancel_on_a_row_without_slots_is_unavailable_and_adds_no_slot(user):
    repo, client = repository([("read", snapshot(SESSION, attempt(), user))])
    assert_code("TEMPORARILY_UNAVAILABLE", lambda: repo.cancel_attempt(AUTH, "attempt-a", "user_stopped"))
    assert [operation for operation, _ in client.calls] == ["read"]


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
    repo.create_session({**SESSION, "session_token": "PRIVATE-BEARER", "password": "PRIVATE-PASSWORD"})
    assert "PRIVATE" not in repr(client.calls)
    assert client.calls[0][1]["ConditionExpression"] == "attribute_not_exists(PK)"


def test_session_creation_writes_a_new_user_row_without_slots():
    repo, client = repository([("put", {}), ("write", {})])
    repo.create_session(SESSION)
    user = _decode(client.calls[0][1]["Item"])
    assert user == {"PK": "USER#dummy", "SK": "STATE", "principal": "dummy", "epoch": user["epoch"],
                    "revision": 0, "updated_at": 20}
    assert type(user["epoch"]) is str and user["epoch"]
    assert client.calls[0][1]["ConditionExpression"] == "attribute_not_exists(PK)"
    assert _decode(client.calls[1][1]["TransactItems"][0]["Put"]["Item"]) == SESSION


def test_state_rejects_noninteger_numbers_without_coercing_profile_json():
    repo, client = repository([("get", {"Item": {**item(SESSION), "revision": {"N": "1.5"}}})])
    assert_code("TEMPORARILY_UNAVAILABLE", lambda: repo.get_session("session-a"))

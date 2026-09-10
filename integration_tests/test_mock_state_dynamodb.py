"""Conditional transactions against DynamoDBLocal, never an in-memory state fake."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
from threading import Barrier, Lock
import uuid

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ReadTimeoutError
import pytest

from mock_journey.errors import JourneyError
from mock_journey.models import AuthContext
from mock_journey.state import DynamoStateRepository


PRINCIPAL = "local-dummy-principal"
SLOTS = [
    f"{program}:{target}"
    for program in ("mock-cpr", "mock-compression-only", "mock-ventilation-only", "mock-two-rescuer-cpr", "mock-two-rescuer-aed")
    for target in ("adult", "child", "infant")
]
SLOT = "mock-cpr:adult"
SERIALIZER = TypeSerializer()
DESERIALIZER = TypeDeserializer()


def _encode(values):
    return {key: SERIALIZER.serialize(value) for key, value in values.items()}


def _decode(values):
    return {key: DESERIALIZER.deserialize(value) for key, value in values.items()}


class Clock:
    def __init__(self):
        self.now = 1_800_000_000

    def __call__(self):
        return self.now


class OneTransactionInterruption:
    """Run a race once, then delegate every condition to real DynamoDBLocal."""

    def __init__(self, client, *, before=None, lose_response=False):
        self.client = client
        self.before = before
        self.lose_response = lose_response
        self.used = False
        self.lock = Lock()

    def __getattr__(self, name):
        return getattr(self.client, name)

    def transact_write_items(self, **kwargs):
        with self.lock:
            first = not self.used
            self.used = True
        if first and self.before is not None:
            self.before()
        response = self.client.transact_write_items(**kwargs)
        if first and self.lose_response:
            raise ReadTimeoutError(endpoint_url="http://127.0.0.1/local-response-loss")
        return response


class World:
    def __init__(self, client, table):
        self.client = client
        self.table = table
        self.clock = Clock()
        self.repository = DynamoStateRepository(client, table, clock=self.clock)

    def repo(self, client):
        return DynamoStateRepository(client, self.table, clock=self.clock)

    def session(self, *, principal=PRINCIPAL, expires_at=None):
        session = {
            "session_id": str(uuid.uuid4()),
            "principal": principal,
            "token_hash": hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
            "issued_at": self.clock(),
            "expires_at": self.clock() + 86400 if expires_at is None else expires_at,
            "status": "active",
            "revision": 1,
        }
        self.repository.create_session(session, SLOTS)
        return AuthContext(session["session_id"], principal, 1, session["expires_at"])

    def template(self, auth):
        return {
            "attempt_id": str(uuid.uuid4()),
            "principal": auth.principal,
            "creator_session_id": auth.session_id,
            "bound_session_id": auth.session_id,
            "program_id": "mock-cpr",
            "target": "adult",
            "profile_name": "tester",
            # Synthetic interface fixture, not a configured runtime definition.
            "definition_json": json.dumps({
                "condition": {"target": "adult"}, "goal": {"kind": "cycles", "required": 3},
                "calculation_profile": {"unchanged_types": [None, False, 80, 80.0, "80"]},
                "catalog_version": "local-test", "profile_version": "local-test",
                "adapter_version": "local-test", "projection_version": "local-test",
            }),
            "resume_nonce": uuid.uuid4().hex,
            "resume_key_version": "local-test",
            "resume_digest": hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
        }

    def create(self, auth, *, repository=None, request_id=None, digest="same-create-input", template=None):
        return (repository or self.repository).create_attempt(
            auth, request_id or str(uuid.uuid4()), digest, template or self.template(auth)
        )

    def item(self, pk, sk):
        response = self.client.get_item(TableName=self.table, Key=_encode({"PK": pk, "SK": sk}), ConsistentRead=True)
        return _decode(response["Item"]) if "Item" in response else None

    def progress(self):
        return self.item("USER#" + PRINCIPAL, "STATE")

    def mutate_session(self, auth, **values):
        key = {"PK": "SESSION#" + auth.session_id, "SK": "AUTH"}
        item = self.item(key["PK"], key["SK"])
        item.update(values)
        item["revision"] += 1
        self.client.put_item(TableName=self.table, Item=_encode(item))

    def complete_slot_for_race(self):
        self.client.update_item(
            TableName=self.table, Key=_encode({"PK": "USER#" + PRINCIPAL, "SK": "STATE"}),
            UpdateExpression="SET #slots.#slot.#done = :yes, #revision = #revision + :one",
            ExpressionAttributeNames={"#slots": "slots", "#slot": SLOT, "#done": "completed", "#revision": "revision"},
            ExpressionAttributeValues=_encode({":yes": True, ":one": 1}),
        )

    def all_items(self):
        rows = []
        request = {"TableName": self.table, "ConsistentRead": True}
        while True:
            response = self.client.scan(**request)
            rows.extend(_decode(item) for item in response["Items"])
            if "LastEvaluatedKey" not in response:
                return rows
            request["ExclusiveStartKey"] = response["LastEvaluatedKey"]


@pytest.fixture
def world(dynamodb_client, dynamodb_table):
    return World(dynamodb_client, dynamodb_table)


def _parallel(*calls):
    with ThreadPoolExecutor(max_workers=len(calls)) as executor:
        futures = [executor.submit(call) for call in calls]
        answers = []
        for future in futures:
            try:
                answers.append(future.result(timeout=20))
            except JourneyError as error:
                answers.append(error)
        return answers


def test_same_slot_concurrent_attempts_both_succeed_without_an_exclusive_lock(world):
    auth1, auth2 = world.session(), world.session()
    barrier = Barrier(2)
    repositories = [world.repo(OneTransactionInterruption(world.client, before=lambda: barrier.wait(timeout=10))) for _ in range(2)]
    attempts = _parallel(
        lambda: world.create(auth1, repository=repositories[0]),
        lambda: world.create(auth2, repository=repositories[1]),
    )
    assert not any(isinstance(item, JourneyError) for item in attempts), attempts
    assert attempts[0]["attempt_id"] != attempts[1]["attempt_id"]
    assert world.progress()["slots"][SLOT]["open_attempts"] == 2


def test_completion_inserted_after_create_read_blocks_the_transaction_without_partial_writes(world):
    auth = world.session()
    repository = world.repo(OneTransactionInterruption(world.client, before=world.complete_slot_for_race))
    with pytest.raises(JourneyError) as raised:
        world.create(auth, repository=repository)
    assert raised.value.code == "PROGRAM_ALREADY_COMPLETED"
    assert world.progress()["slots"][SLOT]["open_attempts"] == 0
    assert not [item for item in world.all_items() if item["PK"].startswith("ATTEMPT#") or item["SK"].startswith("CREATE#")]


def test_same_create_request_race_persists_exactly_one_attempt(world):
    auth = world.session()
    request_id = str(uuid.uuid4())
    barrier = Barrier(2)
    repositories = [world.repo(OneTransactionInterruption(world.client, before=lambda: barrier.wait(timeout=10))) for _ in range(2)]
    attempts = _parallel(*[
        lambda repository=repository: world.create(auth, repository=repository, request_id=request_id)
        for repository in repositories
    ])
    assert not any(isinstance(item, JourneyError) for item in attempts), attempts
    assert attempts[0]["attempt_id"] == attempts[1]["attempt_id"]
    assert world.progress()["slots"][SLOT]["open_attempts"] == 1
    with pytest.raises(JourneyError) as raised:
        world.create(auth, request_id=request_id, digest="different-input")
    assert raised.value.code == "IDEMPOTENCY_CONFLICT"


@pytest.mark.parametrize("change, expected", [("revoke", "SESSION_REVOKED"), ("expire", "SESSION_EXPIRED")])
def test_session_change_after_auth_read_rejects_new_attempt_atomically(world, change, expected):
    auth = world.session()

    def interfere():
        if change == "revoke":
            world.mutate_session(auth, status="revoked")
        else:
            world.mutate_session(auth, expires_at=world.clock())

    repository = world.repo(OneTransactionInterruption(world.client, before=interfere))
    with pytest.raises(JourneyError) as raised:
        world.create(auth, repository=repository)
    assert raised.value.code == expected
    assert world.progress()["slots"][SLOT]["open_attempts"] == 0
    assert not [item for item in world.all_items() if item["PK"].startswith("ATTEMPT#")]


def test_conflict_retry_checks_a_fresh_clock_instead_of_reusing_the_initial_auth_time(world):
    auth = world.session()

    def interfere():
        world.client.update_item(
            TableName=world.table,
            Key=_encode({"PK": "USER#" + PRINCIPAL, "SK": "STATE"}),
            UpdateExpression="SET #revision = #revision + :one",
            ExpressionAttributeNames={"#revision": "revision"},
            ExpressionAttributeValues=_encode({":one": 1}),
        )
        world.clock.now = auth.expires_at

    repository = world.repo(OneTransactionInterruption(world.client, before=interfere))
    with pytest.raises(JourneyError) as raised:
        world.create(auth, repository=repository)
    assert raised.value.code == "SESSION_EXPIRED"
    assert world.progress()["slots"][SLOT]["open_attempts"] == 0
    assert not [item for item in world.all_items() if item["PK"].startswith("ATTEMPT#")]


def test_logout_replay_does_not_reset_other_session_new_progress(world):
    old_session, surviving_session = world.session(), world.session()
    initial_epoch = world.progress()["epoch"]
    world.create(old_session)
    world.repository.logout(old_session)
    reset_epoch = world.progress()["epoch"]
    assert reset_epoch != initial_epoch
    new_attempt = world.create(surviving_session)
    world.repository.logout(old_session)
    assert world.progress()["epoch"] == reset_epoch
    assert world.progress()["slots"][SLOT]["open_attempts"] == 1
    assert new_attempt["epoch"] == reset_epoch
    assert world.repository.get_progress(surviving_session)["epoch"] == reset_epoch


def test_two_different_concurrent_logouts_each_reset_once_and_keep_third_session_active(world):
    auth1, auth2, survivor = world.session(), world.session(), world.session()
    initial = world.progress()
    barrier = Barrier(2)
    repositories = [world.repo(OneTransactionInterruption(world.client, before=lambda: barrier.wait(timeout=10))) for _ in range(2)]
    answers = _parallel(lambda: repositories[0].logout(auth1), lambda: repositories[1].logout(auth2))
    assert answers == [None, None]
    session1 = world.repository.get_session(auth1.session_id)
    session2 = world.repository.get_session(auth2.session_id)
    assert session1["status"] == session2["status"] == "revoked"
    assert session1["logout_epoch"] != session2["logout_epoch"]
    assert world.progress()["revision"] == initial["revision"] + 2
    assert world.repository.get_progress(survivor)["epoch"] == world.progress()["epoch"]


def test_create_racing_logout_never_mixes_attempt_epoch_and_current_open_count(world):
    logout_auth, creator = world.session(), world.session()
    barrier = Barrier(2)
    create_repo = world.repo(OneTransactionInterruption(world.client, before=lambda: barrier.wait(timeout=10)))
    logout_repo = world.repo(OneTransactionInterruption(world.client, before=lambda: barrier.wait(timeout=10)))
    attempt, logout_answer = _parallel(
        lambda: world.create(creator, repository=create_repo),
        lambda: logout_repo.logout(logout_auth),
    )
    assert isinstance(attempt, dict), attempt
    assert logout_answer is None
    current = world.progress()
    assert current["slots"][SLOT]["open_attempts"] == (1 if attempt["epoch"] == current["epoch"] else 0)
    assert world.repository.get_session(logout_auth.session_id)["status"] == "revoked"


def test_logout_commit_response_loss_does_not_erase_progress_on_retry(world):
    logout_auth, creator = world.session(), world.session()
    repository = world.repo(OneTransactionInterruption(world.client, lose_response=True))
    try:
        repository.logout(logout_auth)
    except JourneyError as error:
        assert error.code == "TEMPORARILY_UNAVAILABLE"
    world.create(creator)
    before_retry = deepcopy(world.progress())
    world.repository.logout(logout_auth)
    assert world.progress() == before_retry


def test_bound_session_is_required_even_when_the_principal_is_shared(world):
    creator, other_session = world.session(), world.session()
    attempt = world.create(creator)
    for operation in (
        lambda: world.repository.get_attempt(other_session, attempt["attempt_id"]),
        lambda: world.repository.cancel_attempt(other_session, attempt["attempt_id"], "user_cancelled"),
    ):
        with pytest.raises(JourneyError) as raised:
            operation()
        assert raised.value.code == "NOT_FOUND"
    assert world.repository.get_attempt(creator, attempt["attempt_id"])["state"] == "created"
    assert world.progress()["slots"][SLOT]["open_attempts"] == 1


def test_old_epoch_cancel_does_not_decrement_new_epoch_count(world):
    old_session, surviving_session = world.session(), world.session()
    old_attempt = world.create(surviving_session)
    world.repository.logout(old_session)
    world.create(surviving_session)
    before = deepcopy(world.progress())
    world.repository.cancel_attempt(surviving_session, old_attempt["attempt_id"], "user_cancelled")
    world.repository.cancel_attempt(surviving_session, old_attempt["attempt_id"], "user_cancelled")
    assert world.progress() == before
    assert world.repository.get_attempt(surviving_session, old_attempt["attempt_id"])["state"] == "cancelled"


def test_current_epoch_cancel_is_idempotent_and_cannot_be_reauthorized(world):
    auth = world.session()
    attempt = world.create(auth)
    world.repository.cancel_attempt(auth, attempt["attempt_id"], "user_cancelled")
    world.repository.cancel_attempt(auth, attempt["attempt_id"], "user_cancelled")
    assert world.progress()["slots"][SLOT]["open_attempts"] == 0
    world.clock.now = auth.expires_at
    new_auth = world.session()
    with pytest.raises(JourneyError) as raised:
        world.repository.reauthorize_attempt(new_auth, attempt["attempt_id"], attempt["resume_digest"])
    assert raised.value.code == "INVALID_STATE"


def test_new_session_revocation_during_resume_cannot_take_ownership(world):
    old_auth = world.session()
    attempt = world.create(old_auth)
    world.clock.now = old_auth.expires_at
    new_auth = world.session()
    repository = world.repo(OneTransactionInterruption(
        world.client, before=lambda: world.mutate_session(new_auth, status="revoked")
    ))
    with pytest.raises(JourneyError) as raised:
        repository.reauthorize_attempt(new_auth, attempt["attempt_id"], attempt["resume_digest"])
    assert raised.value.code == "SESSION_REVOKED"
    stored = world.repository.get_attempt_for_reauthorization(attempt["attempt_id"])
    assert stored["bound_session_id"] == old_auth.session_id
    assert stored["revision"] == attempt["revision"]


def test_active_old_session_cannot_be_taken_over_and_expired_resume_keeps_creator_and_epoch(world):
    old_auth, another_auth = world.session(), world.session()
    attempt = world.create(old_auth)
    with pytest.raises(JourneyError) as raised:
        world.repository.reauthorize_attempt(another_auth, attempt["attempt_id"], attempt["resume_digest"])
    assert raised.value.code == "INVALID_STATE"
    world.clock.now = old_auth.expires_at
    new_auth = world.session()
    rebound = world.repository.reauthorize_attempt(new_auth, attempt["attempt_id"], attempt["resume_digest"])
    assert rebound["bound_session_id"] == new_auth.session_id
    for field in ("creator_session_id", "epoch", "definition_json", "resume_digest", "resume_nonce"):
        assert rebound[field] == attempt[field]
    assert world.repository.reauthorize_attempt(new_auth, attempt["attempt_id"], attempt["resume_digest"])["revision"] == rebound["revision"]


def test_two_new_sessions_cannot_both_take_over_the_same_expired_attempt(world):
    old_auth = world.session()
    attempt = world.create(old_auth)
    world.clock.now = old_auth.expires_at
    new_auth1, new_auth2 = world.session(), world.session()
    barrier = Barrier(2)
    repositories = [world.repo(OneTransactionInterruption(world.client, before=lambda: barrier.wait(timeout=10))) for _ in range(2)]
    answers = _parallel(*[
        lambda repository=repository, auth=auth: repository.reauthorize_attempt(auth, attempt["attempt_id"], attempt["resume_digest"])
        for repository, auth in zip(repositories, [new_auth1, new_auth2])
    ])
    successes = [answer for answer in answers if isinstance(answer, dict)]
    failures = [answer for answer in answers if isinstance(answer, JourneyError)]
    assert len(successes) == len(failures) == 1, answers
    assert failures[0].code == "INVALID_STATE"
    assert world.repository.get_attempt_for_reauthorization(attempt["attempt_id"])["bound_session_id"] == successes[0]["bound_session_id"]


def test_commit_response_loss_followed_by_same_create_does_not_duplicate(world):
    auth = world.session()
    request_id = str(uuid.uuid4())
    repository = world.repo(OneTransactionInterruption(world.client, lose_response=True))
    try:
        first = world.create(auth, repository=repository, request_id=request_id)
    except JourneyError as error:
        assert error.code == "TEMPORARILY_UNAVAILABLE"
        first = None
    recovered = world.create(auth, request_id=request_id)
    if first is not None:
        assert recovered["attempt_id"] == first["attempt_id"]
    assert world.progress()["slots"][SLOT]["open_attempts"] == 1
    assert len([item for item in world.all_items() if item["PK"].startswith("ATTEMPT#")]) == 1


def test_profile_json_string_preserves_null_bool_integer_float_and_string(world):
    auth = world.session()
    template = world.template(auth)
    attempt = world.create(auth, template=template)
    stored = world.repository.get_attempt(auth, attempt["attempt_id"])
    assert stored["definition_json"] == template["definition_json"]
    values = json.loads(stored["definition_json"])["calculation_profile"]["unchanged_types"]
    assert [type(value) for value in values] == [type(None), bool, int, float, str]

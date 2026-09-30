"""Q16 v2 session expiry commit guard through real DynamoDB Local conditions."""

import uuid

import pytest

from mock_journey.course_policy import CoursePolicy
from mock_journey.course_settings import fixture_course_settings
from mock_journey.course_state import DynamoCourseRepository
from mock_journey.storage_keys import session_key
from tests.course_store_fakes import InMemoryBlobStore
from mock_journey.state import DynamoCourseStore, DynamoStateRepository, _decode, _encode
from tests.course_guards_support import (
    COMMIT_OPERATIONS, EXPIRES_AT, NOW, Context,
    assert_expiry_between_read_and_commit_is_session_expired, assert_unexpired_session_still_commits,
)
from tests.vcc_state_support import make_bundle, seed_auth


@pytest.fixture
def expiry_context(dynamodb_client, dynamodb_table):
    class SeedableCourseStore(DynamoCourseStore):
        def seed(self, item):
            dynamodb_client.put_item(TableName=dynamodb_table, Item=_encode(item))

    store = SeedableCourseStore(DynamoStateRepository(dynamodb_client, dynamodb_table, clock=lambda: NOW))
    settings = fixture_course_settings()
    repo = DynamoCourseRepository(
        store, settings, CoursePolicy(settings), InMemoryBlobStore(),
        clock=lambda: NOW, uuid_factory=lambda: str(uuid.uuid4()),
    )
    auth = seed_auth(store, expires_at=EXPIRES_AT)
    session = session_key(auth.session_id)

    def snapshot():
        rows, start = [], None
        while True:
            page = dynamodb_client.scan(TableName=dynamodb_table, ConsistentRead=True,
                                        **({"ExclusiveStartKey": start} if start else {}))
            rows.extend(_decode(item) for item in page.get("Items", []))
            start = page.get("LastEvaluatedKey")
            if not start:
                break
        return sorted((row for row in rows if (row["PK"], row["SK"]) != (session["PK"], session["SK"])),
                      key=lambda row: (row["PK"], row["SK"]))

    return Context(repo, store, auth, make_bundle(), snapshot)


@pytest.mark.parametrize("name", sorted(COMMIT_OPERATIONS))
def test_session_expiring_before_commit_fails_condition_and_is_session_expired(expiry_context, name):
    assert_expiry_between_read_and_commit_is_session_expired(expiry_context, name)


@pytest.mark.parametrize("name", ["begin_inventory", "apply_refresh", "start_final", "report"])
def test_session_still_valid_at_commit_time_commits(expiry_context, name):
    assert_unexpired_session_still_commits(expiry_context, name)


def test_expiry_condition_is_evaluated_by_dynamodb(expiry_context):
    store, auth = expiry_context.store, expiry_context.auth
    check = {"op": "condition_check", "key": session_key(auth.session_id),
             "if_match": {"status": "active", "revision": auth.revision, "principal": auth.principal}}
    assert store.transact([{**check, "if_greater": {"expires_at": EXPIRES_AT - 1}}]) is True
    assert store.transact([{**check, "if_greater": {"expires_at": EXPIRES_AT}}]) is False
    assert store.transact([{**check, "if_greater": {"expires_at": EXPIRES_AT + 1}}]) is False

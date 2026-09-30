"""Legacy (/mock/v1-era) session/attempt row seeds for job-store tests. Never imported by runtime code.

The /mock/v1 create route is removed (D103), but rows it wrote stay readable
and computable: USER with ``slots``, SESSION/AUTH, and created ATTEMPT/META
without ``course_binding``. tests/fixtures/legacy_mock_v1_rows/ holds one
captured copy of each. Job-store, Relay and Worker tests need many legacy
attempts under fresh identifiers, so these helpers clone the captured rows as
templates and change only identifiers, times and the synthetic definition:

* ``seed_legacy_session`` writes the fixture SESSION/AUTH shape (revision 0,
  as captured) and, when the principal has none, the fixture USER shape with
  every captured slot reset.
* ``seed_legacy_attempt`` writes the fixture created-ATTEMPT shape and counts it
  in the USER slot in one conditional transaction with the removed create's
  conditions and USER effects (active_counted=True, slot open_attempts+1, USER
  revision+1). It does not reproduce the create's SESSION#.../CREATE#...
  idempotency receipt row (no retained code reads it, design R5), so row-count
  checks must not expect one.
* ``check_fixture_shape`` asserts a seeded row has exactly the captured fields.

Writes use only put_item/transact_write_items (Put/ConditionCheck), so the
seeds work on DynamoDB Local and on tests/memory_dynamodb.MemoryDynamoDB.

``Clock`` and ``OneTransactionInterruption`` are the race tools the job-store
tests used from integration_tests/test_mock_state_dynamodb.py; they live in
tests/dynamodb_doubles.py and are re-exported here.
"""

from copy import deepcopy
import hashlib
import uuid

from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError

from mock_journey.models import AuthContext
from tests.dynamodb_doubles import Clock, OneTransactionInterruption  # noqa: F401 (re-export)
from tests.journey_support import HARNESS_ENVIRONMENT, HARNESS_KEY_VERSION, HARNESS_RESUME_KEYS, decode_item
from tests.legacy_rows_support import load_legacy_fixture


_SERIALIZER = TypeSerializer()


def encode_row(row):
    return {key: _SERIALIZER.serialize(value) for key, value in row.items()}


def fixture_templates():
    """Decoded captured rows: {"user", "session", "created_attempt"} (fresh copies)."""
    meta, rows, _ = load_legacy_fixture()
    decoded = [decode_item(item) for item in rows]
    created = meta["attempts"]["created"]
    return {
        "user": next(row for row in decoded if row["PK"].startswith("USER#")),
        "session": next(row for row in decoded if row["PK"].startswith("SESSION#") and row["SK"] == "AUTH"),
        "created_attempt": next(row for row in decoded if row["PK"] == f"ATTEMPT#{created}"),
    }


def check_fixture_shape(row, kind):
    """A seeded row carries exactly the captured row's fields (no more, no fewer)."""
    template = fixture_templates()[kind]
    assert set(row) == set(template), (kind, sorted(set(row) ^ set(template)))
    for field, value in template.items():
        if value is not None and row[field] is not None:
            assert type(row[field]) is type(value), (kind, field)


def _get(client, table, pk, sk):
    found = client.get_item(TableName=table, Key=encode_row({"PK": pk, "SK": sk}), ConsistentRead=True).get("Item")
    return decode_item(found) if found is not None else None


def _put_user_once(client, table, user):
    try:
        client.put_item(TableName=table, Item=encode_row(user), ConditionExpression="attribute_not_exists(PK)")
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise


def seed_legacy_session(client, table, *, clock, principal, expires_at=None, token_hash=None):
    """Write a legacy SESSION/AUTH row (and the legacy USER row once); return its AuthContext."""
    templates = fixture_templates()
    now = int(clock())
    user = templates["user"]
    user.update(PK=f"USER#{principal}", principal=principal, epoch=str(uuid.uuid4()), revision=0, updated_at=now,
                slots={key: {"completed": False, "completed_by_attempt": None, "completed_at": None,
                             "open_attempts": 0} for key in user["slots"]})
    check_fixture_shape(user, "user")
    _put_user_once(client, table, user)  # One USER row per principal, as the removed create_session kept it.
    session_id = str(uuid.uuid4())
    session = templates["session"]
    session.update(PK=f"SESSION#{session_id}", session_id=session_id, principal=principal,
                   token_hash=token_hash or hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
                   issued_at=now, expires_at=now + 86400 if expires_at is None else expires_at,
                   status="active", revision=0)
    check_fixture_shape(session, "session")
    client.put_item(TableName=table, Item=encode_row(session), ConditionExpression="attribute_not_exists(PK)")
    return AuthContext(session_id, principal, 0, session["expires_at"])


def seed_legacy_attempt(client, table, auth, *, clock, program_id, target, definition_json,
                        profile_name="tester", environment=HARNESS_ENVIRONMENT):
    """Write one created legacy ATTEMPT counted in its USER slot; return the stored row.

    The transaction is conditioned like the removed create: active session,
    unchanged USER revision/epoch and an incomplete slot. Resume fields are
    derived with the synthetic harness key for ``environment``.
    """
    from mock_journey.auth import AuthManager

    now = int(clock())
    user = _get(client, table, f"USER#{auth.principal}", "STATE")
    slot_key = f"{program_id}:{target}"
    assert user is not None and slot_key in user["slots"], "seed_legacy_session first"
    assert user["slots"][slot_key]["completed"] is False, "a completed legacy slot refused new attempts"
    attempt_id = str(uuid.uuid4())
    attempt = fixture_templates()["created_attempt"]
    attempt.update(
        PK=f"ATTEMPT#{attempt_id}", attempt_id=attempt_id, principal=auth.principal,
        creator_session_id=auth.session_id, bound_session_id=auth.session_id,
        program_id=program_id, target=target, profile_name=profile_name, definition_json=definition_json,
        epoch=user["epoch"], created_at=now, state="created", revision=0, active_counted=True,
        evaluation=None, progress_application=None,
    )
    manager = AuthManager(None, environment, HARNESS_RESUME_KEYS, HARNESS_KEY_VERSION)
    attempt.update({key: value for key, value in manager.prepare_resume(attempt).items()
                    if key in ("resume_nonce", "resume_key_version", "resume_digest")})
    check_fixture_shape(attempt, "created_attempt")
    counted = deepcopy(user)
    counted["slots"][slot_key]["open_attempts"] += 1
    counted.update(revision=user["revision"] + 1, updated_at=now)
    client.transact_write_items(TransactItems=[
        {"ConditionCheck": {
            "TableName": table, "Key": encode_row({"PK": f"SESSION#{auth.session_id}", "SK": "AUTH"}),
            "ConditionExpression": "#s = :active AND #r = :revision AND #p = :principal AND #e > :now",
            "ExpressionAttributeNames": {"#s": "status", "#r": "revision", "#p": "principal", "#e": "expires_at"},
            "ExpressionAttributeValues": encode_row({":active": "active", ":revision": auth.revision,
                                                     ":principal": auth.principal, ":now": now}),
        }},
        {"Put": {"TableName": table, "Item": encode_row(counted),
                 "ConditionExpression": "#r = :revision AND #e = :epoch",
                 "ExpressionAttributeNames": {"#r": "revision", "#e": "epoch"},
                 "ExpressionAttributeValues": encode_row({":revision": user["revision"], ":epoch": user["epoch"]})}},
        {"Put": {"TableName": table, "Item": encode_row(attempt), "ConditionExpression": "attribute_not_exists(PK)"}},
    ])
    return attempt


def seed_session_without_slots(client, table, *, clock, principal, token_hash=None, expires_at=None):
    """A current (post-/mock/v1) SESSION/AUTH and, once per principal, a USER row without slots.

    Design Q10: new USER rows carry only principal/epoch/revision/updated_at.
    """
    now = int(clock())
    user = {"PK": f"USER#{principal}", "SK": "STATE", "principal": principal, "epoch": str(uuid.uuid4()),
            "revision": 0, "updated_at": now}
    _put_user_once(client, table, user)
    session_id = str(uuid.uuid4())
    session = {"PK": f"SESSION#{session_id}", "SK": "AUTH", "session_id": session_id, "principal": principal,
               "token_hash": token_hash or hashlib.sha256(uuid.uuid4().bytes).hexdigest(), "issued_at": now,
               "expires_at": now + 86400 if expires_at is None else expires_at, "status": "active", "revision": 0}
    client.put_item(TableName=table, Item=encode_row(session), ConditionExpression="attribute_not_exists(PK)")
    return AuthContext(session_id, principal, 0, session["expires_at"])

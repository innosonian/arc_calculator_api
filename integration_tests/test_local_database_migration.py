"""Owned temporary DynamoDB tables prove that only the local journey schema is accepted.

New installations are created with the journey schema (PK/SK plus the exact
GSI1 due index). An already initialized installation is reproduced by a raw
seed of that same table, this installation's sentinel and the rows a deployed
table may already hold (tests/fixtures/legacy_mock_v1_rows), so /api/v2 over
those legacy rows and their v1 slot values stays covered. A table with any
other schema, such as the PK/SK-only table of the removed control-only CLI,
is refused without a write (D122): there is no in-place migration any more.
No /mock/v1 code runs.
"""

from copy import deepcopy
import hashlib
import json
import secrets
import time
import uuid

from botocore.exceptions import ClientError
import pytest

from local_server import database as db
from mock_journey.jobs import DynamoJobRepository
from mock_journey.state import DynamoStateRepository
from tests.journey_support import JourneyStore, V2Journey, dummy_course
from tests.legacy_rows_support import seed_legacy_rows


GENERIC_MESSAGE = "Local database installation is unavailable or inconsistent."


@pytest.fixture
def installation(tmp_path, monkeypatch, dynamodb_client):
    table = "arc_local_migration_" + uuid.uuid4().hex
    monkeypatch.setattr(db, "TABLE_NAME", table)
    directory = tmp_path.resolve() / "private-installation"
    directory.mkdir(mode=0o700)
    material = db.prepare_material(directory)
    try:
        yield material
    finally:
        try:
            dynamodb_client.delete_table(TableName=table)
        except ClientError as error:
            if error.response["Error"]["Code"] != "ResourceNotFoundException":
                raise


def rows(client):
    response = client.scan(TableName=db.TABLE_NAME, ConsistentRead=True)
    assert "LastEvaluatedKey" not in response
    return sorted(response["Items"], key=lambda item: (item["PK"]["S"], item["SK"]["S"]))


def schema_signature(client):
    """The parts of the table description a schema write would change (read-only)."""
    table = client.describe_table(TableName=db.TABLE_NAME)["Table"]
    return (table["KeySchema"], sorted(table["AttributeDefinitions"], key=lambda item: item["AttributeName"]),
            [(index["IndexName"], index["KeySchema"], index["Projection"])
             for index in table.get("GlobalSecondaryIndexes", [])])


def wait_until(client, predicate):
    deadline = time.monotonic() + 30
    while True:
        try:
            table = client.describe_table(TableName=db.TABLE_NAME)["Table"]
        except ClientError as error:
            # DynamoDB Local answers DescribeTable with a transient InternalFailure
            # while a GSI it is creating settles; the fixture client has no retries.
            if error.response["Error"]["Code"] != "InternalFailure":
                raise
            table = None
        if table is not None and predicate(table):
            return table
        assert time.monotonic() < deadline
        time.sleep(0.05)


def mark_initialized(material):
    path = material.data_dir / db.MATERIAL_FILENAME
    record = json.loads(path.read_text())
    record["database_initialized"] = True
    db._write_record(path, record, replace=True)
    return db.prepare_material(material.data_dir)


def seed_journey(client, material, *, legacy_rows=True):
    """Raw seed of an initialized journey-schema installation, then (optionally) its legacy rows.

    Returns the re-read initialized material and the seeded legacy fixture.
    """
    client.create_table(TableName=db.TABLE_NAME, KeySchema=db._KEY_SCHEMA,
                        AttributeDefinitions=db._ATTRIBUTES + db._JOB_ATTRIBUTES,
                        GlobalSecondaryIndexes=[db._JOB_INDEX], BillingMode="PAY_PER_REQUEST")
    wait_until(client, lambda table: db._table_kind(table) == "journey")
    client.put_item(TableName=db.TABLE_NAME, Item=db._sentinel(material),
                    ConditionExpression="attribute_not_exists(PK)")
    seeded = seed_legacy_rows(JourneyStore(client, db.TABLE_NAME)) if legacy_rows else None
    return mark_initialized(material), seeded


def seed_other_schema(client, material, kind):
    """A table that is not the journey schema, with this installation's sentinel and initialized keys.

    ``control``: the PK/SK-only table of the removed control-only CLI.
    ``foreign-projection``: that table plus a GSI1 whose projection differs.
    """
    client.create_table(TableName=db.TABLE_NAME, KeySchema=db._KEY_SCHEMA,
                        AttributeDefinitions=db._ATTRIBUTES, BillingMode="PAY_PER_REQUEST")
    wait_until(client, lambda table: table["TableStatus"] == "ACTIVE")
    if kind == "foreign-projection":
        foreign = {**deepcopy(db._JOB_INDEX), "Projection": {"ProjectionType": "KEYS_ONLY"}}
        client.update_table(TableName=db.TABLE_NAME, AttributeDefinitions=db._JOB_ATTRIBUTES,
                            GlobalSecondaryIndexUpdates=[{"Create": foreign}])
        wait_until(client, lambda table: table["TableStatus"] == "ACTIVE"
                   and table["GlobalSecondaryIndexes"][0].get("IndexStatus") == "ACTIVE")
    table = client.describe_table(TableName=db.TABLE_NAME)["Table"]
    assert db._table_kind(table) is None and db._table_kind(table, pending=True) is None
    client.put_item(TableName=db.TABLE_NAME, Item=db._sentinel(material),
                    ConditionExpression="attribute_not_exists(PK)")
    return mark_initialized(material)


def seed_peer_session(client, seeded):
    """A second active legacy session of the same tester (the removed CLI allowed several).

    The captured fixture holds one session; this peer is that exact SESSION/AUTH
    row shape with its own session id and the hash of a fresh random test token.
    Returns (session_id, token).
    """
    session_id = str(uuid.uuid4())
    token = f"s1.{session_id}.{secrets.token_urlsafe(32)}"
    raw = next(item for item in seeded.raw_rows
               if item["PK"] == {"S": f"SESSION#{seeded.session_id}"} and item["SK"] == {"S": "AUTH"})
    client.put_item(TableName=db.TABLE_NAME, ConditionExpression="attribute_not_exists(PK)", Item={
        **raw, "PK": {"S": f"SESSION#{session_id}"}, "session_id": {"S": session_id},
        "token_hash": {"S": hashlib.sha256(token.encode()).hexdigest()}})
    return session_id, token


@pytest.mark.parametrize("arguments", [{}, {"initialize": True}])
def test_new_installation_has_exact_active_index(installation, dynamodb_endpoint, arguments):
    runtime = db.connect_application(dynamodb_endpoint, installation, **arguments)
    try:
        assert runtime.ready() and runtime.table_name == db.TABLE_NAME
        table = runtime.client.describe_table(TableName=runtime.table_name)["Table"]
        assert db._table_kind(table) == "journey"
        assert runtime.client.get_item(TableName=db.TABLE_NAME, Key=db._SENTINEL_KEY, ConsistentRead=True)["Item"] == db._sentinel(installation)
        assert rows(runtime.client) == [db._sentinel(installation)]
        reopened = db.prepare_material(installation.data_dir)
        assert reopened.database_initialized is True
        assert reopened.resume_key == installation.resume_key and reopened.installation_id == installation.installation_id
    finally:
        runtime.close()


def test_legacy_rows_on_the_journey_schema_serve_v2_and_preserve_slots(installation, dynamodb_endpoint,
                                                                       dynamodb_client):
    material, seeded = seed_journey(dynamodb_client, installation)
    peer_id, peer_token = seed_peer_session(dynamodb_client, seeded)
    parent = child = again = reader = None
    try:
        # The read-only log reader form attaches to the initialized installation.
        reader = db.connect_application(dynamodb_endpoint, material, initialize=False)
        assert reader.ready()
        before = rows(dynamodb_client)
        signature = schema_signature(dynamodb_client)
        key_before = (installation.data_dir / db.MATERIAL_FILENAME).read_bytes()
        parent = db.connect_application(dynamodb_endpoint, material)
        assert parent.ready() and rows(parent.client) == before
        assert db._table_kind(parent.client.describe_table(TableName=db.TABLE_NAME)["Table"]) == "journey"
        assert schema_signature(parent.client) == signature
        assert (installation.data_dir / db.MATERIAL_FILENAME).read_bytes() == key_before
        assert db.prepare_material(installation.data_dir) == material
        assert reader.ready()
        queued = seeded.attempts["queued"]
        now = seeded.meta["capture_clock"] + 60
        jobs = DynamoJobRepository(DynamoStateRepository(parent.client, db.TABLE_NAME, clock=lambda: now))
        due, cursor = jobs.due_jobs(limit=20)
        outbox, _ = jobs.due_outbox(limit=20)
        assert cursor is None and [row["job_id"] for row in due] == [queued["job_id"]]
        assert [row["job_id"] for row in outbox] == [queued["job_id"]]
        child = db.connect_application(dynamodb_endpoint, material, initialize=False)
        assert child.client is not parent.client and child.ready()
        again = db.connect_application(dynamodb_endpoint, material)
        assert again.ready() and rows(again.client) == before
        # The table serves /api/v2 over the preserved sessions and rows.
        store = JourneyStore(child.client, db.TABLE_NAME)
        user_before = store.row("USER#" + seeded.principal, "STATE")
        assert user_before == seeded.rows[("USER#" + seeded.principal, "STATE")] and len(user_before["slots"]) == 15
        # The child reads the v1 slot values unchanged: a completed independent
        # slot and one open attempt for each unfinished attempt.
        slots = user_before["slots"]
        finished = slots["mock-ventilation-only:adult"]
        assert finished["completed"] is True and finished["open_attempts"] == 0
        assert finished["completed_by_attempt"] == seeded.attempts["evaluated"]["attempt_id"]
        assert slots["mock-cpr:adult"]["open_attempts"] == 1 and slots["mock-cpr:adult"]["completed"] is False
        assert slots["mock-compression-only:adult"]["open_attempts"] == 1
        h = V2Journey(store, objects=seeded.objects, start=now)
        token = seeded.issue_session_token()
        # Both legacy sessions authenticate end to end on the child.
        assert h.session(token)["sessionId"] == seeded.session_id
        assert h.session(peer_token)["sessionId"] == peer_id
        assert h.attempt(token, queued["attempt_id"])["state"] == "queued"
        # The legacy attempt stays bound to its own session; the peer gets no view of it.
        assert h.call("GET", f'/api/v2/attempts/{queued["attempt_id"]}/', token=peer_token).status == 404
        # The due index drives the existing Relay -> Worker path.
        assert set(h.relay()) == {queued["job_id"]}  # Outbox and due-job wakes may both name it.
        assert h.deliver() == {"batchItemFailures": []}
        assert h.result(token, queued["attempt_id"])["calculationStatus"] == "succeeded"
        user_after = store.row("USER#" + seeded.principal, "STATE")
        assert (user_after["principal"], user_after["epoch"]) == (user_before["principal"], user_before["epoch"])
        # The legacy completion path (kept for stored v1 jobs) closes exactly
        # the queued attempt's slot; every other v1 slot value is preserved.
        expected = deepcopy(user_before["slots"])
        expected["mock-compression-only:adult"] = {
            "completed": True, "completed_by_attempt": queued["attempt_id"], "completed_at": now, "open_attempts": 0}
        assert user_after["slots"] == expected
        # Shared progress: a course practice completed through one legacy session is the
        # peer's progress too (one tester epoch), and neither session reset that epoch.
        course = dummy_course("mock-ventilation-only", "adult")
        for session_token in (token, peer_token):
            h.refresh(session_token)
        assert h.course(peer_token, course) == h.course(token, course)
        assert [item["isCompleted"] for item in h.course(peer_token, course)["courseItems"]] == [False, False]
        started = h.start(token, course, course.practice_link_id)
        h.upload(token, started["attemptId"], started["condition"])
        assert h.work(started["attemptId"]) is True
        assert h.result(token, started["attemptId"])["progressApplication"]["reason"] == "APPLIED"
        shared = h.course(peer_token, course)
        assert [item["isCompleted"] for item in shared["courseItems"]] == [True, False]
        assert h.course(token, course) == shared
        final_user = store.row("USER#" + seeded.principal, "STATE")
        assert final_user["epoch"] == user_before["epoch"] and final_user["slots"] == expected
        assert h.session(peer_token)["sessionId"] == peer_id and h.session(token)["sessionId"] == seeded.session_id
        assert schema_signature(dynamodb_client) == signature
    finally:
        for runtime in (again, child, parent, reader):
            if runtime is not None:
                runtime.close()


@pytest.mark.parametrize("kind", ["control", "foreign-projection"])
@pytest.mark.parametrize("initialize", [True, False])
def test_tables_without_the_journey_schema_are_refused_without_writes(installation, dynamodb_endpoint,
                                                                     dynamodb_client, kind, initialize):
    # D122: neither the parent (initialize=True) nor a child/log reader
    # (initialize=False) migrates, repairs or adopts such a table.
    material = seed_other_schema(dynamodb_client, installation, kind)
    before = rows(dynamodb_client), schema_signature(dynamodb_client)
    key_before = (installation.data_dir / db.MATERIAL_FILENAME).read_bytes()
    with pytest.raises(db.LocalDatabaseError) as error:
        db.connect_application(dynamodb_endpoint, material, initialize=initialize)
    assert str(error.value) == GENERIC_MESSAGE
    assert (rows(dynamodb_client), schema_signature(dynamodb_client)) == before
    assert before[1][2] == ([] if kind == "control" else [("GSI1", db._JOB_INDEX["KeySchema"], {"ProjectionType": "KEYS_ONLY"})])
    assert (installation.data_dir / db.MATERIAL_FILENAME).read_bytes() == key_before


def test_foreign_sentinel_on_the_journey_schema_is_refused_without_writes(installation, dynamodb_endpoint,
                                                                          dynamodb_client):
    material, _ = seed_journey(dynamodb_client, installation, legacy_rows=False)
    bad = {**db._sentinel(material), "resume_key_sha256": {"S": "0" * 64}}
    dynamodb_client.put_item(TableName=db.TABLE_NAME, Item=bad)
    before = rows(dynamodb_client), schema_signature(dynamodb_client)
    for arguments in ({}, {"initialize": False}):
        with pytest.raises(db.LocalDatabaseError) as error:
            db.connect_application(dynamodb_endpoint, material, **arguments)
        assert str(error.value) == GENERIC_MESSAGE
    assert (rows(dynamodb_client), schema_signature(dynamodb_client)) == before
    assert db._table_kind(dynamodb_client.describe_table(TableName=db.TABLE_NAME)["Table"]) == "journey"


def test_initialized_material_never_recreates_a_missing_table(installation, dynamodb_endpoint, dynamodb_client):
    material, _ = seed_journey(dynamodb_client, installation, legacy_rows=False)
    dynamodb_client.delete_table(TableName=db.TABLE_NAME)
    for arguments in ({"initialize": False}, {}):
        with pytest.raises(db.LocalDatabaseError) as error:
            db.connect_application(dynamodb_endpoint, material, **arguments)
        assert str(error.value) == GENERIC_MESSAGE
    with pytest.raises(ClientError) as missing:
        dynamodb_client.describe_table(TableName=db.TABLE_NAME)
    assert missing.value.response["Error"]["Code"] == "ResourceNotFoundException"

"""Owned temporary DynamoDB tables prove additive local index migration."""

from copy import deepcopy
import time
import uuid

from botocore.exceptions import ClientError, ReadTimeoutError
import pytest

from integration_tests.test_local_completion_state import PendingDefinitions
from local_server import database as db
from mock_journey.catalog import Catalog
from mock_journey.jobs import DynamoJobRepository
from mock_journey.state import _encode


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


def seed_control(runtime):
    service = runtime.application
    service.catalog = Catalog(PendingDefinitions())
    session = service.login({"login_id": "test@test.com", "password": "2222"})
    other = service.login({"login_id": "test@test.com", "password": "2222"})
    auth = service.auth.authenticate(session["session_token"])
    status, attempt = service.create_attempt(auth, {"client_request_id": "retain-create-id",
                                                   "catalog_version": "mock-catalog-v1",
                                                   "program_id": "mock-cpr", "target": "adult"})
    assert status == 201
    jobs = DynamoJobRepository(service.state)
    stored = jobs.accept_input(auth, attempt["attempt_id"], "a" * 64,
                               {"bucket": "migration-test-only", "key": "unused-manifest", "sha256": "b" * 64, "size": 10},
                               job_id=str(uuid.uuid4()), adapter_version=PendingDefinitions().get_definition("mock-cpr", "adult")["adapter_version"],
                               next_due_at=int(time.time()))
    # Seed a previously completed independent slot. This migration test never
    # executes the synthetic manifest and does not invent a completion policy.
    runtime.client.update_item(
        TableName=db.TABLE_NAME, Key=_encode({"PK": "USER#" + auth.principal, "SK": "STATE"}),
        UpdateExpression="SET #s.#slot.#done = :yes, #r = #r + :one",
        ExpressionAttributeNames={"#s": "slots", "#slot": "mock-compression-only:adult", "#done": "completed", "#r": "revision"},
        ExpressionAttributeValues=_encode({":yes": True, ":one": 1}),
    )
    return session, other, attempt, stored


def test_new_journey_installation_has_exact_active_index(installation, dynamodb_endpoint):
    runtime = db.connect_application(dynamodb_endpoint, installation, journey=True)
    try:
        assert runtime.ready() and runtime.table_name == db.TABLE_NAME
        table = runtime.client.describe_table(TableName=runtime.table_name)["Table"]
        assert db._table_kind(table) == "journey"
        assert runtime.client.get_item(TableName=db.TABLE_NAME, Key=db._SENTINEL_KEY, ConsistentRead=True)["Item"] == db._sentinel(installation)
        reopened = db.prepare_material(installation.data_dir)
        assert reopened.database_initialized is True
        assert reopened.resume_key == installation.resume_key and reopened.installation_id == installation.installation_id
    finally:
        runtime.close()


def test_additive_migration_preserves_rows_keys_sessions_epoch_and_due_jobs(installation, dynamodb_endpoint):
    old = db.connect_application(dynamodb_endpoint, installation)
    upgraded = child = control = None
    try:
        session, other, attempt, stored = seed_control(old)
        before = rows(old.client)
        material = db.prepare_material(installation.data_dir)
        key_before = (installation.data_dir / db.MATERIAL_FILENAME).read_bytes()
        upgraded = db.connect_application(dynamodb_endpoint, material, journey=True)
        assert upgraded.ready() and rows(upgraded.client) == before
        assert (installation.data_dir / db.MATERIAL_FILENAME).read_bytes() == key_before
        assert db.prepare_material(installation.data_dir) == material
        jobs = DynamoJobRepository(upgraded.application.state)
        due, cursor = jobs.due_jobs(limit=20)
        outbox, _ = jobs.due_outbox(limit=20)
        assert cursor is None and [row["job_id"] for row in due] == [stored["job_id"]]
        assert [row["job_id"] for row in outbox] == [stored["job_id"]]
        child = db.connect_application(dynamodb_endpoint, material, journey=True, initialize=False)
        assert child.client is not upgraded.client and child.ready()
        token = session["session_token"]
        child_auth = child.application.auth.authenticate(token)
        assert child.application.state.get_attempt(child_auth, attempt["attempt_id"])["job_id"] == stored["job_id"]
        other_auth = child.application.auth.authenticate(other["session_token"])
        shared = child.application.state.get_progress(other_auth)
        assert shared["slots"]["mock-compression-only:adult"]["completed"] is True
        assert shared["slots"]["mock-cpr:adult"]["open_attempts"] == 1
        control = db.connect_application(dynamodb_endpoint, material)
        assert control.ready() and rows(control.client) == before
        assert old.ready()  # Current control client recognizes the exact additive schema.
    finally:
        for runtime in (control, child, upgraded, old):
            if runtime is not None:
                runtime.close()


def test_lost_update_response_restarts_without_duplicate_mutation(installation, dynamodb_endpoint, monkeypatch):
    old = db.connect_application(dynamodb_endpoint, installation)
    seed_control(old)
    before = rows(old.client)
    old.close()
    material = db.prepare_material(installation.data_dir)
    new_client = db._new_client
    mutations = []

    class LoseUpdateResponse:
        def __init__(self, client):
            self.client = client

        def __getattr__(self, name):
            return getattr(self.client, name)

        def update_table(self, **kwargs):
            answer = self.client.update_table(**kwargs)
            mutations.append(deepcopy(kwargs))
            if len(mutations) == 1:
                raise ReadTimeoutError(endpoint_url=dynamodb_endpoint)
            return answer

    monkeypatch.setattr(db, "_new_client", lambda endpoint: LoseUpdateResponse(new_client(endpoint)))
    with pytest.raises(db.LocalDatabaseError):
        db.connect_application(dynamodb_endpoint, material, journey=True)
    restarted = db.connect_application(dynamodb_endpoint, material, journey=True)
    try:
        assert restarted.ready() and len(mutations) == 1
        assert rows(restarted.client) == before
    finally:
        restarted.close()


def test_foreign_sentinel_prevents_any_index_change(installation, dynamodb_endpoint):
    old = db.connect_application(dynamodb_endpoint, installation)
    try:
        seed_control(old)
        material = db.prepare_material(installation.data_dir)
        bad = {**db._sentinel(material), "resume_key_sha256": {"S": "0" * 64}}
        old.client.put_item(TableName=db.TABLE_NAME, Item=bad)
        before = rows(old.client)
        with pytest.raises(db.LocalDatabaseError):
            db.connect_application(dynamodb_endpoint, material, journey=True)
        table = old.client.describe_table(TableName=db.TABLE_NAME)["Table"]
        assert db._table_kind(table) == "control" and rows(old.client) == before
    finally:
        old.close()


def test_existing_foreign_projection_is_not_repaired(installation, dynamodb_endpoint):
    old = db.connect_application(dynamodb_endpoint, installation)
    try:
        material = db.prepare_material(installation.data_dir)
        foreign = {**deepcopy(db._JOB_INDEX), "Projection": {"ProjectionType": "KEYS_ONLY"}}
        old.client.update_table(TableName=db.TABLE_NAME, AttributeDefinitions=db._JOB_ATTRIBUTES,
                                GlobalSecondaryIndexUpdates=[{"Create": foreign}])
        before = rows(old.client)
        with pytest.raises(db.LocalDatabaseError):
            db.connect_application(dynamodb_endpoint, material, journey=True)
        index = old.client.describe_table(TableName=db.TABLE_NAME)["Table"]["GlobalSecondaryIndexes"][0]
        assert index["Projection"] == {"ProjectionType": "KEYS_ONLY"}
        assert rows(old.client) == before
    finally:
        old.close()


def test_attach_only_cannot_upgrade_control_or_recreate_missing_table(installation, dynamodb_endpoint):
    old = db.connect_application(dynamodb_endpoint, installation)
    try:
        material = db.prepare_material(installation.data_dir)
        before = rows(old.client)
        with pytest.raises(db.LocalDatabaseError):
            db.connect_application(dynamodb_endpoint, material, journey=True, initialize=False)
        assert db._table_kind(old.client.describe_table(TableName=db.TABLE_NAME)["Table"]) == "control"
        assert rows(old.client) == before
        old.client.delete_table(TableName=db.TABLE_NAME)
        with pytest.raises(db.LocalDatabaseError):
            db.connect_application(dynamodb_endpoint, material, journey=True, initialize=False)
        with pytest.raises(ClientError) as missing:
            old.client.describe_table(TableName=db.TABLE_NAME)
        assert missing.value.response["Error"]["Code"] == "ResourceNotFoundException"
    finally:
        old.close()

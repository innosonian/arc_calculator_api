"""Setup safety only; real transaction correctness is covered by DB integration."""

from dataclasses import replace
from copy import deepcopy
import json
import os
import socket

from botocore.exceptions import ClientError
import pytest

from local_server import database as db


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("DB unit test attempted network.")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)


@pytest.fixture
def private_root(tmp_path):
    root = tmp_path.resolve() / "installation"
    root.mkdir(mode=0o700)
    return root


def test_material_persistence_permissions_and_secret_repr(private_root):
    material = db.prepare_material(private_root)
    assert db.prepare_material(private_root) == material
    assert len(material.resume_key) == 32
    assert material.environment == "local-" + material.installation_id
    assert not material.database_initialized
    assert "resume_key=" not in repr(material)
    assert (private_root / db.MATERIAL_FILENAME).stat().st_mode & 0o777 == 0o600
    assert material.db_dir.stat().st_mode & 0o777 == 0o700


def test_existing_database_missing_key_never_regenerates(private_root):
    db.prepare_material(private_root)
    path = private_root / db.MATERIAL_FILENAME
    path.unlink()
    with pytest.raises(db.LocalDatabaseError, match="key is missing"):
        db.prepare_material(private_root)
    assert not path.exists()


@pytest.mark.parametrize("mode", [0o755, 0o770, 0o777])
def test_insecure_root_rejected_without_chmod(private_root, mode):
    private_root.chmod(mode)
    with pytest.raises(db.LocalDatabaseError, match="0700"):
        db.prepare_material(private_root)
    assert private_root.stat().st_mode & 0o777 == mode


def test_symlink_ancestor_rejected(private_root, tmp_path):
    link = tmp_path.resolve() / "redirect"
    link.symlink_to(private_root, target_is_directory=True)
    with pytest.raises(db.LocalDatabaseError, match="symbolic"):
        db.prepare_material(link)


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "permission", "fifo"])
def test_unsafe_key_file_rejected(private_root, tmp_path, kind):
    db.prepare_material(private_root)
    path = private_root / db.MATERIAL_FILENAME
    outside = tmp_path.resolve() / "other"
    if kind == "symlink":
        path.rename(outside)
        path.symlink_to(outside)
    elif kind == "hardlink":
        os.link(path, outside)
    elif kind == "fifo":
        path.unlink()
        os.mkfifo(path, 0o600)
    else:
        path.chmod(0o644)
    with pytest.raises(db.LocalDatabaseError):
        db.prepare_material(private_root)


@pytest.mark.parametrize("kind", ["symlink", "permission", "hardlink"])
def test_unsafe_database_file_rejected(private_root, tmp_path, kind):
    material = db.prepare_material(private_root)
    path = material.db_dir / "state.db"
    path.write_bytes(b"private state")
    path.chmod(0o600)
    outside = tmp_path.resolve() / "target"
    if kind == "symlink":
        path.rename(outside)
        path.symlink_to(outside)
    elif kind == "hardlink":
        os.link(path, outside)
    else:
        path.chmod(0o644)
    with pytest.raises(db.LocalDatabaseError):
        db.prepare_material(private_root)


@pytest.mark.parametrize("change", ["duplicate", "badkey", "foreign", "wrongtype", "oversize"])
def test_invalid_record_fails_without_echoing(private_root, change):
    db.prepare_material(private_root)
    path = private_root / db.MATERIAL_FILENAME
    original = json.loads(path.read_text())
    marker = "DO-NOT-ECHO-PRIVATE-MARKER"
    if change == "duplicate":
        data = path.read_text()[:-1] + ',"version":1}'
    elif change == "oversize":
        data = marker * 500
    else:
        field, value = {
            "badkey": ("resume_key", marker), "foreign": ("environment", marker),
            "wrongtype": ("database_initialized", 1),
        }[change]
        original[field] = value
        data = json.dumps(original)
    path.write_text(data)
    with pytest.raises(db.LocalDatabaseError) as error:
        db.prepare_material(private_root)
    assert marker not in str(error.value)
    assert path.read_text() == data


def test_pending_first_run_can_recover_before_db_directory_creation(private_root):
    material = db.prepare_material(private_root)
    material.db_dir.rmdir()
    assert db.prepare_material(private_root) == material


def test_initialized_missing_database_never_recreated(private_root):
    material = db.prepare_material(private_root)
    path = private_root / db.MATERIAL_FILENAME
    record = json.loads(path.read_text())
    record["database_initialized"] = True
    path.write_text(json.dumps(record))
    material.db_dir.rmdir()
    with pytest.raises(db.LocalDatabaseError, match="missing"):
        db.prepare_material(private_root)
    assert not material.db_dir.exists()


@pytest.mark.parametrize("endpoint", [
    "https://127.0.0.1:8001", "http://localhost:8001", "http://0.0.0.0:8001",
    "http://127.0.0.1:0", "http://127.0.0.1:65536", "http://127.0.0.1:08001",
    "http://127.0.0.1:8001/", "http://127.0.0.1:8001?x=y", "http://127.0.0.1:8001#x",
    "http://user@127.0.0.1:8001", "http://[::1]:8001", " http://127.0.0.1:8001",
    "http://127.0.0.1:8001\n", None, 8001,
])
def test_invalid_endpoint_rejected(endpoint):
    with pytest.raises(db.LocalDatabaseError, match="loopback"):
        db._new_client(endpoint)


def test_sdk_ignores_ambient_config_profile_and_proxy(monkeypatch, tmp_path):
    config = tmp_path / "poison_config"
    config.write_text("invalid config must never be parsed")
    for name, value in {
        "AWS_PROFILE": "foreign", "AWS_DEFAULT_PROFILE": "foreign",
        "AWS_CONFIG_FILE": str(config), "AWS_SHARED_CREDENTIALS_FILE": str(config),
        "AWS_DATA_PATH": str(config), "AWS_CA_BUNDLE": str(config),
        "AWS_DEFAULT_REGION": "foreign-region", "AWS_DEFAULTS_MODE": "auto",
        "AWS_ENDPOINT_URL": "https://forbidden.invalid",
        "AWS_ENDPOINT_URL_DYNAMODB": "https://forbidden.invalid",
        "HTTP_PROXY": "http://forbidden.invalid", "HTTPS_PROXY": "http://forbidden.invalid",
        "ALL_PROXY": "http://forbidden.invalid",
    }.items():
        monkeypatch.setenv(name, value)
    client = db._new_client("http://127.0.0.1:18767")
    try:
        assert client.meta.endpoint_url == "http://127.0.0.1:18767"
        assert client.meta.region_name == "us-east-2"
        assert client.meta.config.proxies == {}
        assert client.meta.config.connect_timeout == 2
        assert client.meta.config.read_timeout == 5
        credentials = client._request_signer._credentials
        assert credentials.access_key == "ARCLocalOnlyAccess"
        assert credentials.secret_key == "ARCLocalOnlySecret"
    finally:
        client.close()


class SetupClient:
    """Setup fixture only; never shipped or used to prove DB atomicity."""
    def __init__(self, *, exists=False):
        self.exists = exists
        self.item = self.schema = None
        self.foreign = self.down = False
        self.created = self.closed = self.puts = 0

    def describe_table(self, **kwargs):
        if self.down:
            raise RuntimeError("PRIVATE-UPSTREAM-MARKER")
        if not self.exists:
            raise ClientError({"Error": {"Code": "ResourceNotFoundException"}}, "DescribeTable")
        return {"Table": self.schema or {"TableStatus": "ACTIVE", "KeySchema": db._KEY_SCHEMA,
                                         "AttributeDefinitions": db._ATTRIBUTES}}

    def create_table(self, **kwargs):
        self.created += 1
        self.exists = True

    def get_item(self, **kwargs):
        return {"Item": self.item} if self.item is not None else {}

    def scan(self, **kwargs):
        return {"Items": [{"PK": {"S": "foreign"}}] if self.foreign else []}

    def put_item(self, **kwargs):
        self.puts += 1
        self.item = kwargs["Item"]

    def close(self):
        self.closed += 1


def test_setup_restart_binding_and_disabled_calculation(private_root, monkeypatch):
    material = db.prepare_material(private_root)
    client = SetupClient()
    monkeypatch.setattr(db, "_new_client", lambda endpoint: client)
    runtime = db.connect_application("http://127.0.0.1:18767", material)
    assert runtime.ready() is True
    assert runtime.application.calculation is None
    assert runtime.application.catalog.execution_definitions is None
    assert client.created == 1 and client.puts == 1
    reopened = db.prepare_material(private_root)
    assert reopened.database_initialized is True
    assert reopened.resume_key == material.resume_key
    second = db.connect_application("http://127.0.0.1:18767", reopened)
    assert second.ready() is True
    assert client.created == 1 and client.puts == 1
    client.down = True
    assert second.ready() is False
    runtime.close()
    second.close()
    assert client.closed == 2


def test_log_client_failure_is_on_its_writer_and_leaves_business_client_usable(private_root, monkeypatch):
    import threading
    from services.operational_logs import log_context, record_event
    material = db.prepare_material(private_root)
    client = SetupClient()
    callers = []
    def connect(endpoint):
        callers.append(threading.get_ident())
        if len(callers) == 1:
            return client
        raise OSError("PRIVATE-LOG-CLIENT-FAILURE")
    monkeypatch.setattr(db, "_new_client", connect)
    runtime = db.connect_application("http://127.0.0.1:18767", material, operations_role="api")
    try:
        with log_context(runtime.operations):
            record_event("login_succeeded")
        assert runtime.operations.close(timeout=2)
        assert len(callers) == 2 and callers[0] == threading.get_ident() and callers[1] != callers[0]
        assert runtime.operations.status()["unconfirmed"] == 1
        assert runtime.ready() is True and client.closed == 0
    finally:
        runtime.close()
    assert client.closed == 1


def test_log_cleanup_error_cannot_skip_owned_business_client_cleanup():
    class BrokenLogs:
        def close(self, **kwargs):
            raise OSError("PRIVATE-LOG-CLEANUP-FAILURE")
    client = SetupClient()
    runtime = db.LocalDatabase(client, None, None, operations=BrokenLogs())
    runtime.close()
    assert client.closed == 1


@pytest.mark.parametrize("mismatch", [
    "foreign-row", "foreign-sentinel", "schema", "missing-bound-table", "missing-bound-sentinel",
])
def test_foreign_or_lost_database_never_adopted(private_root, monkeypatch, mismatch):
    material = db.prepare_material(private_root)
    client = SetupClient(exists=True)
    if mismatch == "foreign-row":
        client.foreign = True
    elif mismatch == "foreign-sentinel":
        client.item = {"PK": {"S": "PRIVATE-UPSTREAM-MARKER"}}
    elif mismatch == "schema":
        client.schema = {"TableStatus": "ACTIVE", "KeySchema": [{"AttributeName": "other", "KeyType": "HASH"}]}
    else:
        path = private_root / db.MATERIAL_FILENAME
        record = json.loads(path.read_text())
        record["database_initialized"] = True
        path.write_text(json.dumps(record))
        material = db.prepare_material(private_root)
        client.exists = mismatch != "missing-bound-table"
    monkeypatch.setattr(db, "_new_client", lambda endpoint: client)
    with pytest.raises(db.LocalDatabaseError) as error:
        db.connect_application("http://127.0.0.1:18767", material)
    assert "PRIVATE-UPSTREAM-MARKER" not in str(error.value)
    assert client.created == 0 and client.puts == 0 and client.closed == 1


def test_changed_material_rejected_before_client(private_root, monkeypatch):
    material = db.prepare_material(private_root)
    monkeypatch.setattr(db, "_new_client", lambda endpoint: pytest.fail("Client before validation."))
    with pytest.raises(db.LocalDatabaseError):
        db.connect_application("http://127.0.0.1:18767", replace(material, resume_key=b"X" * 32))


def test_readiness_detects_changed_sentinel(private_root, monkeypatch):
    material = db.prepare_material(private_root)
    client = SetupClient()
    monkeypatch.setattr(db, "_new_client", lambda endpoint: client)
    runtime = db.connect_application("http://127.0.0.1:18767", material)
    client.item = {"PK": {"S": "foreign"}}
    assert runtime.ready() is False


class MigrationClient(SetupClient):
    """Setup-only script; real DDB migration has a separate integration file."""

    def __init__(self):
        super().__init__()
        self.updates = []
        self.events = []
        self.pending_reads = 0

    def describe_table(self, **kwargs):
        self.events.append("describe")
        value = deepcopy(super().describe_table(**kwargs))
        if self.pending_reads:
            self.pending_reads -= 1
            value["Table"]["TableStatus"] = "UPDATING"
            value["Table"]["GlobalSecondaryIndexes"][0]["IndexStatus"] = "CREATING"
        return value

    def create_table(self, **kwargs):
        self.events.append("create")
        super().create_table(**kwargs)
        self.schema = {"TableStatus": "ACTIVE", "KeySchema": deepcopy(kwargs["KeySchema"]),
                       "AttributeDefinitions": deepcopy(kwargs["AttributeDefinitions"])}
        if "GlobalSecondaryIndexes" in kwargs:
            self.schema["GlobalSecondaryIndexes"] = [
                {**deepcopy(index), "IndexStatus": "ACTIVE"} for index in kwargs["GlobalSecondaryIndexes"]]

    def get_item(self, **kwargs):
        self.events.append("sentinel")
        return super().get_item(**kwargs)

    def update_table(self, **kwargs):
        assert self.events[-1] == "sentinel"
        self.events.append("update")
        self.updates.append(deepcopy(kwargs))
        self.schema["AttributeDefinitions"] = deepcopy(db._ATTRIBUTES + db._JOB_ATTRIBUTES)
        self.schema["GlobalSecondaryIndexes"] = [{**deepcopy(db._JOB_INDEX), "IndexStatus": "ACTIVE"}]


def initialized_control(private_root, monkeypatch):
    material = db.prepare_material(private_root)
    client = MigrationClient()
    monkeypatch.setattr(db, "_new_client", lambda endpoint: client)
    db.connect_application("http://127.0.0.1:18767", material).close()
    return db.prepare_material(private_root), client


def test_fresh_journey_creates_exact_index_and_exposes_own_client(private_root, monkeypatch):
    material = db.prepare_material(private_root)
    client = MigrationClient()
    monkeypatch.setattr(db, "_new_client", lambda endpoint: client)
    runtime = db.connect_application("http://127.0.0.1:18767", material, journey=True)
    assert runtime.ready() and runtime.client is client and runtime.table_name == db.TABLE_NAME
    assert db._table_kind(client.schema) == "journey"
    assert client.created == client.puts == 1 and client.updates == []
    with pytest.raises(AttributeError):
        runtime.client = object()
    with pytest.raises(AttributeError):
        runtime.table_name = "foreign"


def test_only_owned_exact_control_schema_receives_additive_index(private_root, monkeypatch):
    material, client = initialized_control(private_root, monkeypatch)
    original_key, original_sentinel = material.resume_key, deepcopy(client.item)
    before = (private_root / db.MATERIAL_FILENAME).read_bytes()
    runtime = db.connect_application("http://127.0.0.1:18767", material, journey=True)
    assert runtime.ready()
    assert client.updates == [{"TableName": db.TABLE_NAME, "AttributeDefinitions": db._JOB_ATTRIBUTES,
                              "GlobalSecondaryIndexUpdates": [{"Create": db._JOB_INDEX}]}]
    assert client.item == original_sentinel and client.puts == client.created == 1
    assert (private_root / db.MATERIAL_FILENAME).read_bytes() == before
    assert db.prepare_material(private_root).resume_key == original_key
    assert db.connect_application("http://127.0.0.1:18767", material).ready()
    assert len(client.updates) == 1


@pytest.mark.parametrize("damage", ["sentinel", "index-name", "index-key-type", "projection", "extra-index", "lsi", "missing-attribute"])
def test_foreign_schema_or_sentinel_never_receives_an_update(private_root, monkeypatch, damage):
    material, client = initialized_control(private_root, monkeypatch)
    if damage == "sentinel":
        client.item = {"PK": {"S": "foreign"}}
    else:
        client.schema["AttributeDefinitions"] = deepcopy(db._ATTRIBUTES + db._JOB_ATTRIBUTES)
        client.schema["GlobalSecondaryIndexes"] = [{**deepcopy(db._JOB_INDEX), "IndexStatus": "ACTIVE"}]
        index = client.schema["GlobalSecondaryIndexes"][0]
        if damage == "index-name":
            index["IndexName"] = "foreign"
        elif damage == "index-key-type":
            client.schema["AttributeDefinitions"][-1]["AttributeType"] = "S"
        elif damage == "projection":
            index["Projection"] = {"ProjectionType": "KEYS_ONLY"}
        elif damage == "extra-index":
            client.schema["GlobalSecondaryIndexes"].append(deepcopy(index))
        elif damage == "lsi":
            client.schema["LocalSecondaryIndexes"] = [deepcopy(index)]
        elif damage == "missing-attribute":
            client.schema["AttributeDefinitions"].pop()
    before = deepcopy(client.schema), deepcopy(client.item)
    with pytest.raises(db.LocalDatabaseError):
        db.connect_application("http://127.0.0.1:18767", material, journey=True)
    assert client.updates == [] and client.puts == client.created == 1
    assert (client.schema, client.item) == before


def test_correct_pending_index_is_waited_for_without_another_update(private_root, monkeypatch):
    material, client = initialized_control(private_root, monkeypatch)
    db.connect_application("http://127.0.0.1:18767", material, journey=True)
    client.pending_reads = 3
    sleeps = []
    monkeypatch.setattr(db.time, "sleep", sleeps.append)
    runtime = db.connect_application("http://127.0.0.1:18767", material, journey=True)
    assert runtime.ready() and len(client.updates) == 1 and len(sleeps) == 3


def test_pending_index_timeout_preserves_schema_material_and_can_restart(private_root, monkeypatch):
    material, client = initialized_control(private_root, monkeypatch)
    db.connect_application("http://127.0.0.1:18767", material, journey=True)
    client.pending_reads = 100
    material_before = (private_root / db.MATERIAL_FILENAME).read_bytes()
    clock = iter((0, 31))
    monkeypatch.setattr(db.time, "monotonic", lambda: next(clock))
    with pytest.raises(db.LocalDatabaseError):
        db.connect_application("http://127.0.0.1:18767", material, journey=True)
    assert len(client.updates) == 1 and (private_root / db.MATERIAL_FILENAME).read_bytes() == material_before
    client.pending_reads = 0
    monkeypatch.setattr(db.time, "monotonic", lambda: 100)
    assert db.connect_application("http://127.0.0.1:18767", material, journey=True).ready()
    assert len(client.updates) == 1


def test_child_attach_has_no_setup_writes_and_requires_active_job_index(private_root, monkeypatch):
    material, client = initialized_control(private_root, monkeypatch)
    with pytest.raises(db.LocalDatabaseError):
        db.connect_application("http://127.0.0.1:18767", material, journey=True, initialize=False)
    assert client.updates == []
    db.connect_application("http://127.0.0.1:18767", material, journey=True)
    before = (client.created, client.puts, len(client.updates), (private_root / db.MATERIAL_FILENAME).read_bytes())
    assert db.connect_application("http://127.0.0.1:18767", material, journey=True, initialize=False).ready()
    assert (client.created, client.puts, len(client.updates), (private_root / db.MATERIAL_FILENAME).read_bytes()) == before
    client.pending_reads = 1
    with pytest.raises(db.LocalDatabaseError):
        db.connect_application("http://127.0.0.1:18767", material, journey=True, initialize=False)
    assert (client.created, client.puts, len(client.updates)) == before[:3]


def test_child_refuses_uninitialized_material_before_client_or_write(private_root, monkeypatch):
    material = db.prepare_material(private_root)
    before = (private_root / db.MATERIAL_FILENAME).read_bytes()
    monkeypatch.setattr(db, "_new_client", lambda *a: pytest.fail("Child connected an uninitialized installation."))
    with pytest.raises(db.LocalDatabaseError):
        db.connect_application("http://127.0.0.1:18767", material, journey=True, initialize=False)
    assert (private_root / db.MATERIAL_FILENAME).read_bytes() == before


@pytest.mark.parametrize("damage", ["material", "directory", "both", "tampered"])
def test_child_material_validation_never_regenerates_missing_or_changed_files(private_root, monkeypatch, damage):
    material, _ = initialized_control(private_root, monkeypatch)
    path = private_root / db.MATERIAL_FILENAME
    if damage in ("material", "both"):
        path.unlink()
    if damage in ("directory", "both"):
        material.db_dir.rmdir()
    if damage == "tampered":
        value = json.loads(path.read_text())
        value["resume_key"] = "invalid-key"
        path.write_text(json.dumps(value))
    before = {entry.name: entry.read_bytes() if entry.is_file() else None for entry in private_root.iterdir()}
    monkeypatch.setattr(db, "_new_client", lambda *a: pytest.fail("Invalid material reached the SDK."))
    with pytest.raises(db.LocalDatabaseError):
        db.connect_application("http://127.0.0.1:18767", material, journey=True, initialize=False)
    after = {entry.name: entry.read_bytes() if entry.is_file() else None for entry in private_root.iterdir()}
    assert after == before


@pytest.mark.parametrize("damage", ["empty", "missing-directory", "missing-material"])
def test_read_only_material_load_never_creates_setup_files(private_root, damage):
    if damage != "empty":
        material = db.prepare_material(private_root)
        if damage == "missing-directory":
            material.db_dir.rmdir()
        else:
            (private_root / db.MATERIAL_FILENAME).unlink()
    before = {entry.name: entry.read_bytes() if entry.is_file() else None for entry in private_root.iterdir()}
    with pytest.raises(db.LocalDatabaseError):
        db.prepare_material(private_root, initialize=False)
    assert {entry.name: entry.read_bytes() if entry.is_file() else None for entry in private_root.iterdir()} == before


def test_journey_ready_detects_lost_index_without_repair(private_root, monkeypatch):
    material, client = initialized_control(private_root, monkeypatch)
    runtime = db.connect_application("http://127.0.0.1:18767", material, journey=True)
    client.schema = {"TableStatus": "ACTIVE", "KeySchema": deepcopy(db._KEY_SCHEMA), "AttributeDefinitions": deepcopy(db._ATTRIBUTES)}
    assert not runtime.ready() and len(client.updates) == 1


@pytest.mark.parametrize("arguments", [{"journey": 1}, {"journey": None}, {"initialize": "no"}, {"initialize": 0}])
def test_invalid_setup_flags_are_rejected_before_client(private_root, monkeypatch, arguments):
    material = db.prepare_material(private_root)
    monkeypatch.setattr(db, "_new_client", lambda *a: pytest.fail("Invalid flags reached the SDK."))
    with pytest.raises(db.LocalDatabaseError):
        db.connect_application("http://127.0.0.1:18767", material, **arguments)

"""Private, persistent DynamoDB Local wiring; no AWS credential discovery.

The CLI holds the installation lock and starts a verified loopback-only DB
process before connecting. This module never launches a process, repairs a
foreign database, resets state, or configures calculation execution.
"""

import base64
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import time
import uuid

import boto3.session
import botocore.session
from botocore.config import Config
from botocore.exceptions import ClientError

from mock_journey.auth import AuthManager
from mock_journey.catalog import Catalog
from mock_journey.service import JourneyService
from mock_journey.state import DynamoStateRepository


TABLE_NAME = "arc_mock_local_v1"
MATERIAL_FILENAME = "installation.json"
KEY_VERSION = "local-v1"
_MATERIAL_KEYS = {"version", "installation_id", "environment", "resume_key", "database_initialized"}
_IDENT = re.compile(r"[0-9a-f]{32}\Z")
_ENDPOINT = re.compile(r"http://127\.0\.0\.1:([1-9][0-9]{0,4})\Z")
_SENTINEL_KEY = {"PK": {"S": "LOCAL#INSTALLATION"}, "SK": {"S": "META"}}
_KEY_SCHEMA = [{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}]
_ATTRIBUTES = [{"AttributeName": "PK", "AttributeType": "S"}, {"AttributeName": "SK", "AttributeType": "S"}]
_JOB_ATTRIBUTES = [{"AttributeName": "GSI1PK", "AttributeType": "S"}, {"AttributeName": "GSI1SK", "AttributeType": "N"}]
_JOB_INDEX = {
    "IndexName": "GSI1",
    "KeySchema": [{"AttributeName": "GSI1PK", "KeyType": "HASH"}, {"AttributeName": "GSI1SK", "KeyType": "RANGE"}],
    "Projection": {"ProjectionType": "ALL"},
}
# Local startup preparation only; this is unrelated to the app result budget.
_SCHEMA_WAIT_SECONDS = 30
_SCHEMA_POLL_SECONDS = 0.1


class LocalDatabaseError(RuntimeError):
    """Only fixed, non-secret setup messages are exposed to the CLI."""


@dataclass(frozen=True)
class LocalMaterial:
    data_dir: Path
    db_dir: Path
    environment: str
    resume_key: bytes = field(repr=False)
    installation_id: str
    database_initialized: bool
    key_version: str = KEY_VERSION


def _fail():
    return LocalDatabaseError("Local database installation is unavailable or inconsistent.")


def _private_directory(path):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise LocalDatabaseError("Local data directories must be owned by this user with mode 0700.")


def _no_symlink_components(path):
    # Caller supplies a physical absolute path, rather than silently resolving
    # a symlink that could redirect private data to a different installation.
    if not path.is_absolute() or ".." in path.parts:
        raise _fail()
    for parent in reversed((path, *path.parents)):
        if stat.S_ISLNK(parent.lstat().st_mode):
            raise LocalDatabaseError("Local data paths must not contain symbolic links.")


def _private_file(info):
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
        raise LocalDatabaseError("Local data files must be private regular files with mode 0600.")


def _validate_database_files(path):
    _private_directory(path)
    for child in path.iterdir():
        info = child.lstat()
        if stat.S_ISDIR(info.st_mode):
            _validate_database_files(child)
        else:
            _private_file(info)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _fail()
        result[key] = value
    return result


def _read_record(path):
    _private_file(path.lstat())
    # Reject FIFO/device paths before opening, and retain non-blocking open
    # plus fstat validation in case the path changes after lstat.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        _private_file(info)
        if info.st_size > 4096:
            raise _fail()
        with os.fdopen(fd, "rb", closefd=False) as stream:
            record = json.loads(stream.read(4097), object_pairs_hook=_pairs)
        if type(record) is not dict or set(record) != _MATERIAL_KEYS:
            raise _fail()
        ident = record["installation_id"]
        if (type(record["version"]) is not int or record["version"] != 1
                or type(ident) is not str or not _IDENT.fullmatch(ident)
                or record["environment"] != "local-" + ident
                or type(record["resume_key"]) is not str
                or type(record["database_initialized"]) is not bool):
            raise _fail()
        decoded = base64.b64decode(record["resume_key"], validate=True)
        if len(decoded) != 32 or base64.b64encode(decoded).decode("ascii") != record["resume_key"]:
            raise _fail()
        return record
    finally:
        os.close(fd)


def _sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_record(path, record, *, replace=False):
    destination = path.with_name(".installation-" + uuid.uuid4().hex) if replace else path
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        _private_file(os.fstat(fd))
        raw = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(fd)
    finally:
        os.close(fd)
    if replace:
        # Verify the destination immediately before replacing it; never follow
        # a link or overwrite another installation's secrets during recovery.
        before = _read_record(path)
        expected = dict(record, database_initialized=before["database_initialized"])
        if before != expected:
            raise _fail()
        os.replace(destination, path)
    _sync_directory(path.parent)


def prepare_material(data_dir, *, initialize=True):
    """Create or validate stable local keys; caller already holds its lock.

    Missing keys with any existing DB directory fail closed. A valid pending
    first-run record can recover an interruption before the DB directory was
    created. Once initialized, losing that directory never starts a fresh DB.
    """
    try:
        if type(initialize) is not bool:
            raise _fail()
        data_dir = Path(data_dir)
        _no_symlink_components(data_dir)
        _private_directory(data_dir)
        material_path, db_dir = data_dir / MATERIAL_FILENAME, data_dir / "dynamodb"
        if not material_path.exists() and not material_path.is_symlink():
            if not initialize:
                raise _fail()
            if db_dir.exists() or db_dir.is_symlink():
                raise LocalDatabaseError("The database exists but its installation key is missing. Restore them together.")
            ident = uuid.uuid4().hex
            record = {"version": 1, "installation_id": ident, "environment": "local-" + ident,
                      "resume_key": base64.b64encode(secrets.token_bytes(32)).decode("ascii"),
                      "database_initialized": False}
            _write_record(material_path, record)
        record = _read_record(material_path)
        if not db_dir.exists() and not db_dir.is_symlink():
            if record["database_initialized"] or not initialize:
                raise LocalDatabaseError("Initialized local database files are missing. Restore the installation together.")
            db_dir.mkdir(mode=0o700)
            _sync_directory(data_dir)
        _validate_database_files(db_dir)
        return LocalMaterial(data_dir, db_dir, record["environment"],
                             base64.b64decode(record["resume_key"], validate=True),
                             record["installation_id"], record["database_initialized"])
    except LocalDatabaseError:
        raise
    except Exception:
        raise _fail() from None


def _new_client(endpoint):
    if type(endpoint) is not str or not (match := _ENDPOINT.fullmatch(endpoint)) or int(match[1]) > 65535:
        raise LocalDatabaseError("The local database endpoint must be an explicit IPv4 loopback HTTP port.")
    # Use a fresh botocore Session, never boto3's process-wide default session.
    # Freeze providers, including explicit None values: Session's ordinary
    # instance-variable chain would otherwise fall through None to AWS_PROFILE.
    # The CLI additionally allowlists its environment before SDK imports.
    core = botocore.session.Session()
    store = core.get_component("config_store")
    for name, specification in core.session_var_map.items():
        store.set_config_variable(name, specification[2])
    for key, value in (("config_file", os.devnull), ("credentials_file", os.devnull),
                       ("profile", None), ("data_path", None), ("region", "us-east-2"),
                       ("ca_bundle", None), ("ignore_configured_endpoint_urls", True),
                       ("defaults_mode", "legacy")):
        store.set_config_variable(key, value)
    core.set_credentials("ARCLocalOnlyAccess", "ARCLocalOnlySecret", "ARCLocalOnlySession")
    session = boto3.session.Session(botocore_session=core)
    return session.client(
        "dynamodb", endpoint_url=endpoint, region_name="us-east-2",
        aws_access_key_id="ARCLocalOnlyAccess", aws_secret_access_key="ARCLocalOnlySecret",
        aws_session_token="ARCLocalOnlySession",
        config=Config(proxies={}, connect_timeout=2, read_timeout=5,
                      retries={"total_max_attempts": 1}, endpoint_discovery_enabled=False),
    )


def _sentinel(material):
    return {**_SENTINEL_KEY, "schema_version": {"N": "1"},
            "installation_id": {"S": material.installation_id},
            "environment": {"S": material.environment},
            "resume_key_sha256": {"S": hashlib.sha256(material.resume_key).hexdigest()}}


def _same_attributes(value, expected):
    return (type(value) is list and all(type(item) is dict for item in value)
            and sorted(value, key=lambda item: item.get("AttributeName", ""))
            == sorted(expected, key=lambda item: item["AttributeName"]))


def _table_kind(table, *, pending=False):
    """Recognize only the original table or its one exact additive job index."""
    try:
        if (type(table) is not dict or table.get("LocalSecondaryIndexes", []) != []
                or not _same_attributes(table.get("KeySchema"), _KEY_SCHEMA)):
            return None
        status = table.get("TableStatus")
        indexes = table.get("GlobalSecondaryIndexes", [])
        if type(indexes) is not list:
            return None
        if not indexes:
            if (not _same_attributes(table.get("AttributeDefinitions"), _ATTRIBUTES)
                    or status not in (("ACTIVE", "CREATING") if pending else ("ACTIVE",))):
                return None
            return "control"
        if (len(indexes) != 1 or type(indexes[0]) is not dict
                or not _same_attributes(table.get("AttributeDefinitions"), _ATTRIBUTES + _JOB_ATTRIBUTES)):
            return None
        index = indexes[0]
        if (index.get("IndexName") != _JOB_INDEX["IndexName"]
                or not _same_attributes(index.get("KeySchema"), _JOB_INDEX["KeySchema"])
                or index.get("Projection") != _JOB_INDEX["Projection"]
                or ("Backfilling" in index and type(index["Backfilling"]) is not bool)):
            return None
        if (status == "ACTIVE" and index.get("IndexStatus") == "ACTIVE"
                and not index.get("Backfilling", False)):
            return "journey"
        if pending and status in ("ACTIVE", "CREATING", "UPDATING") and index.get("IndexStatus") == "CREATING":
            return "journey"
        return None
    except (KeyError, TypeError, ValueError):
        return None


def _table_valid(table, *, journey=False):
    kind = _table_kind(table)
    return kind == "journey" if journey else kind in ("control", "journey")


def _wait_for_schema(client, table, kind):
    deadline = time.monotonic() + _SCHEMA_WAIT_SECONDS
    while True:
        if _table_kind(table, pending=True) != kind:
            raise _fail()
        if _table_kind(table) == kind:
            return table
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _fail()
        time.sleep(min(_SCHEMA_POLL_SECONDS, remaining))
        table = client.describe_table(TableName=TABLE_NAME)["Table"]


class LocalDatabase:
    def __init__(self, client, application, sentinel, *, journey=False, operations=None):
        self._client = client
        self._sentinel = sentinel
        self._journey = journey
        self.application = application
        self.operations = operations

    @property
    def client(self):
        """This process's explicitly connected local client, never inherited."""
        return self._client

    @property
    def table_name(self):
        return TABLE_NAME

    def ready(self):
        """A live DB and the exact installation binding are required."""
        try:
            table = self._client.describe_table(TableName=TABLE_NAME)["Table"]
            item = self._client.get_item(TableName=TABLE_NAME, Key=_SENTINEL_KEY, ConsistentRead=True).get("Item")
            return _table_valid(table, journey=self._journey) and item == self._sentinel
        except Exception:
            return False

    def close(self):
        if self.operations is not None:
            try:
                self.operations.close(timeout=1.0)
            except Exception:
                pass  # Log cleanup must not prevent the owned DB client closing.
        self._client.close()


def connect_application(endpoint, material, *, journey=False, initialize=True, operations_role=None):
    """Connect after the CLI verifies its own DB and holds installation_lock.

    ``journey`` requires the exact active due index. Only the parent's default
    initialize=True path creates setup records or adds that index. A child
    passes initialize=False with already initialized material to attach using
    its own client; it performs no schema, sentinel or material writes.
    """
    client = operations = None
    try:
        if (type(material) is not LocalMaterial or type(journey) is not bool or type(initialize) is not bool
                or (not initialize and not material.database_initialized)):
            raise _fail()
        # A material already supplied by the caller is a validation target,
        # never permission to regenerate missing keys/directories at attach.
        checked = prepare_material(material.data_dir, initialize=False)
        if checked != material:
            raise _fail()
        client = _new_client(endpoint)
        try:
            table = client.describe_table(TableName=TABLE_NAME)["Table"]
        except ClientError as error:
            if (error.response.get("Error", {}).get("Code") != "ResourceNotFoundException"
                    or material.database_initialized or not initialize):
                raise _fail() from None
            creation = {"TableName": TABLE_NAME, "KeySchema": _KEY_SCHEMA,
                        "AttributeDefinitions": _ATTRIBUTES + _JOB_ATTRIBUTES if journey else _ATTRIBUTES,
                        "BillingMode": "PAY_PER_REQUEST"}
            if journey:
                creation["GlobalSecondaryIndexes"] = [_JOB_INDEX]
            client.create_table(**creation)
            table = client.describe_table(TableName=TABLE_NAME)["Table"]
        kind = _table_kind(table, pending=initialize)
        if kind is None:
            raise _fail()
        if table["TableStatus"] == "CREATING":
            if material.database_initialized or not initialize:
                raise _fail()
            table = _wait_for_schema(client, table, kind)
        sentinel = _sentinel(material)
        found = client.get_item(TableName=TABLE_NAME, Key=_SENTINEL_KEY, ConsistentRead=True).get("Item")
        if found is None and not material.database_initialized and initialize:
            # Recover a first-start crash between table creation and sentinel
            # commit, but never adopt a table containing any unrelated data.
            if client.scan(TableName=TABLE_NAME, Limit=1, ConsistentRead=True,
                           ProjectionExpression="PK, SK").get("Items") != []:
                raise _fail()
            client.put_item(TableName=TABLE_NAME, Item=sentinel,
                            ConditionExpression="attribute_not_exists(PK)")
            found = client.get_item(TableName=TABLE_NAME, Key=_SENTINEL_KEY, ConsistentRead=True).get("Item")
        if found != sentinel:
            raise _fail()
        if journey and kind == "control":
            if not initialize:
                raise _fail()
            # The exact old schema and this installation's sentinel were both
            # checked before the only additive schema mutation.
            client.update_table(TableName=TABLE_NAME, AttributeDefinitions=_JOB_ATTRIBUTES,
                                GlobalSecondaryIndexUpdates=[{"Create": _JOB_INDEX}])
            kind = "journey"
            table = client.describe_table(TableName=TABLE_NAME)["Table"]
        if initialize:
            table = _wait_for_schema(client, table, kind)
        if not _table_valid(table, journey=journey):
            raise _fail()
        if client.get_item(TableName=TABLE_NAME, Key=_SENTINEL_KEY, ConsistentRead=True).get("Item") != sentinel:
            raise _fail()
        if not material.database_initialized:
            path = material.data_dir / MATERIAL_FILENAME
            record = _read_record(path)
            record["database_initialized"] = True
            _write_record(path, record, replace=True)
        state = DynamoStateRepository(client, TABLE_NAME)
        auth = AuthManager(state, material.environment, {material.key_version: material.resume_key}, material.key_version)
        application = JourneyService(state, auth, Catalog(), calculation=None)
        if operations_role is not None:
            from services.operational_logs import AsyncLogRecorder
            from mock_journey.log_storage import DynamoLogStore
            try:
                operations = AsyncLogRecorder(
                    lambda: DynamoLogStore(_new_client(endpoint), TABLE_NAME, material.environment), role=operations_role)
            except Exception:
                operations = None
            application.operations = operations
        return LocalDatabase(client, application, sentinel, journey=journey, operations=operations)
    except Exception:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
        raise _fail() from None

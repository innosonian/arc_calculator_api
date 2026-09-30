"""One explicitly initialized, fenced reconciliation checkpoint per environment.

Only scan metadata is replaced here. Jobs, due timestamps and result records
remain owned by their existing repositories. No client or resource is created.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
import hashlib
import json
import math
import re
import uuid

from botocore.exceptions import BotoCoreError, ClientError

from mock_journey.aws_scope import (
    ACCOUNT_ID_PATTERN, ENVIRONMENT_PATTERN, PARTITIONS, REGION_PATTERN, TABLE_NAME_PATTERN,
    environment_namespace, partition_matches_region,
)
from mock_journey.errors import JourneyError
from mock_journey.settings import check_queue_url
from mock_journey.state import encode_item, unavailable
from mock_journey.storage_keys import due_partition


_KINDS = ("OUTBOX", "JOB")
_FIELDS = {"PK", "SK", "schema", "binding_sha256", "revision", "owner", "fence",
           "lease_until", "next_kind", "scans"}
_CALL_GUARD = ContextVar("relay_progress_call_guard", default=None)


class RelayProgressLost(JourneyError):
    def __init__(self):
        super().__init__("TEMPORARILY_UNAVAILABLE")


class RelayProgressUncertain(JourneyError):
    def __init__(self, committed=None):
        super().__init__("TEMPORARILY_UNAVAILABLE")
        # Only this fixed classification is retained; never the row/SDK error.
        self.committed = committed


class RelayTimeStopped(JourneyError):
    def __init__(self):
        super().__init__("TEMPORARILY_UNAVAILABLE")


def check_relay_call():
    guard = _CALL_GUARD.get()
    if guard is not None:
        guard()


@contextmanager
def relay_call_guard(check):
    token = _CALL_GUARD.set(check)
    try:
        yield
    finally:
        _CALL_GUARD.reset(token)


class RelayGuardedClient:
    """Invocation-local admission before SDK calls, including command retries."""
    def __init__(self, client):
        self.client = client

    def __getattr__(self, name):
        value = getattr(self.client, name)
        if not callable(value):
            return value

        def guarded(*args, **kwargs):
            check_relay_call()
            return value(*args, **kwargs)
        return guarded


def _integer(value):
    # DynamoDB numbers support at most 38 significant decimal digits.
    if type(value) is not int or not 0 <= value < 10**38:
        raise unavailable()
    return value


def _kind(value):
    if type(value) is not str or value not in _KINDS:
        raise unavailable()


def _uuid(value):
    try:
        if type(value) is not str or str(uuid.UUID(value)) != value:
            raise unavailable()
    except (ValueError, AttributeError):
        raise unavailable() from None


def validate_cursor(value, kind, cutoff):
    """Validate decoded real GSI keys; never reconstruct a midpoint cursor."""
    _kind(kind)
    _integer(cutoff)
    if value is None:
        return None
    if type(value) is not dict or set(value) != {"PK", "SK", "GSI1PK", "GSI1SK"}:
        raise unavailable()
    prefix = kind + "#"
    if (type(value["PK"]) is not str or not value["PK"].startswith(prefix)
            or value["SK"] != ("DISPATCH" if kind == "OUTBOX" else "STATE")
            or value["GSI1PK"] != due_partition(kind)):
        raise unavailable()
    _uuid(value["PK"][len(prefix):])
    if _integer(value["GSI1SK"]) > cutoff:
        raise unavailable()
    return deepcopy(value)


class DynamoRelayProgress:
    def __init__(self, state, *, environment, partition, account_id, region, queue_url):
        checks = ((environment, ENVIRONMENT_PATTERN),
                  (account_id, ACCOUNT_ID_PATTERN),
                  (region, REGION_PATTERN),
                  (state.table_name, TABLE_NAME_PATTERN))
        if (any(type(v) is not str or not re.fullmatch(pattern, v) for v, pattern in checks)
                or partition not in PARTITIONS
                or not partition_matches_region(partition, region)):
            raise ValueError("Invalid relay progress scope.")
        check_queue_url(queue_url)
        self.state = state
        self.key = {"PK": environment_namespace("RELAY_SCAN#", environment), "SK": "PROGRESS#v1"}
        binding = [partition, account_id, region, state.table_name, environment, queue_url]
        self.binding_sha256 = hashlib.sha256(
            json.dumps(binding, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()

    def now(self):
        value = self.state.clock()
        if type(value) is int:
            return _integer(value)
        if type(value) is not float or not math.isfinite(value) or value < 0:
            raise unavailable()
        return _integer(int(value))

    def validate(self, row):
        if (type(row) is not dict or set(row) != _FIELDS
                or any(row[k] != v for k, v in self.key.items())
                or type(row["schema"]) is not int or row["schema"] != 1
                or row["binding_sha256"] != self.binding_sha256):
            raise unavailable()
        for key in ("revision", "fence", "lease_until"):
            _integer(row[key])
        if row["owner"] is None:
            if row["lease_until"] != 0:
                raise unavailable()
        else:
            _uuid(row["owner"])
            if row["lease_until"] == 0 or row["fence"] == 0:
                raise unavailable()
        if row["revision"] < row["fence"]:
            raise unavailable()
        _kind(row["next_kind"])
        if type(row["scans"]) is not dict or set(row["scans"]) != set(_KINDS):
            raise unavailable()
        for kind in _KINDS:
            scan = row["scans"][kind]
            if type(scan) is not dict or set(scan) != {"cutoff", "cursor"}:
                raise unavailable()
            if scan["cutoff"] is None:
                if scan["cursor"] is not None:
                    raise unavailable()
            else:
                validate_cursor(scan["cursor"], kind, scan["cutoff"])
        # Encoded JSON is a conservative upper bound on this small scalar/map
        # item's DynamoDB storage bytes. The exact shape cannot grow a history.
        if len(json.dumps(encode_item(row), separators=(",", ":")).encode()) >= 400 * 1024:
            raise unavailable()
        return deepcopy(row)

    def initial_item(self):
        return self.validate({**self.key, "schema": 1, "binding_sha256": self.binding_sha256,
                              "revision": 0, "owner": None, "fence": 0, "lease_until": 0,
                              "next_kind": "OUTBOX", "scans": {
                                  kind: {"cutoff": None, "cursor": None} for kind in _KINDS}})

    def initialization_request(self):
        return {"TableName": self.state.table_name, "Item": encode_item(self.initial_item()),
                "ConditionExpression": "attribute_not_exists(PK)"}

    def initialize(self):
        """Explicit create-only operation; runtime must never call this."""
        row = self.initial_item()
        if not self._put(self.initialization_request(), row):
            raise unavailable()
        return row

    def get(self):
        check_relay_call()
        return self.validate(self.state.get_row(self.key))

    def _put(self, request, proposed):
        try:
            check_relay_call()
            self.state.client.put_item(**request)
            return True
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return False
        except BotoCoreError:
            pass
        # A response loss may follow a committed conditional write. Resolve
        # with a strong read if time permits, but always stop this invocation.
        # In particular, never retry a stale revision or release a new owner.
        committed = None
        try:
            committed = self.get() == proposed
        except JourneyError:
            pass
        raise RelayProgressUncertain(committed) from None

    def _replace(self, old, new, *, acquire=False):
        self.validate(old)
        self.validate(new)
        now = self.now()
        expression = "#s = :schema AND #b = :binding AND #r = :revision AND #o = :owner AND #f = :fence AND #l = :lease"
        names = {"#s": "schema", "#b": "binding_sha256", "#r": "revision",
                 "#o": "owner", "#f": "fence", "#l": "lease_until"}
        values = {":schema": 1, ":binding": self.binding_sha256, ":revision": old["revision"],
                  ":owner": old["owner"], ":fence": old["fence"], ":lease": old["lease_until"], ":now": now}
        if acquire:
            expression += " AND #l <= :now"
        else:
            if old["owner"] is None or old["lease_until"] <= now:
                raise RelayProgressLost()
            expression += " AND #l > :now"
        return self._put({"TableName": self.state.table_name, "Item": encode_item(new),
                          "ConditionExpression": expression, "ExpressionAttributeNames": names,
                          "ExpressionAttributeValues": encode_item(values)}, new)

    def acquire(self, *, lease_seconds):
        if _integer(lease_seconds) == 0:
            raise unavailable()
        for _ in range(self.state.max_conflict_retries):
            old = self.get()
            now = self.now()
            if old["lease_until"] > now:
                return None
            new = {**old, "owner": str(uuid.uuid4()), "lease_until": now + lease_seconds,
                   "fence": old["fence"] + 1, "revision": old["revision"] + 1}
            if self._replace(old, new, acquire=True):
                return new
        raise RelayProgressLost()

    def _owned_update(self, old, new, *, lease_seconds):
        if _integer(lease_seconds) == 0:
            raise unavailable()
        new.update(revision=old["revision"] + 1, lease_until=self.now() + lease_seconds)
        if not self._replace(old, new):
            raise RelayProgressLost()
        return new

    def renew(self, snapshot, *, lease_seconds):
        return self._owned_update(snapshot, self.validate(snapshot), lease_seconds=lease_seconds)

    @staticmethod
    def _turn(row, kind, out_of_turn):
        # The persisted kind normally takes the step. The caller may step the
        # alternate kind only explicitly, once the persisted kind has completed
        # or used its budget in that invocation. next_kind then keeps its turn.
        _kind(kind)
        if type(out_of_turn) is not bool or (kind == row["next_kind"]) == out_of_turn:
            raise unavailable()

    def begin_pass(self, snapshot, kind, *, lease_seconds, out_of_turn=False):
        new = self.validate(snapshot)
        self._turn(new, kind, out_of_turn)
        if new["scans"][kind]["cutoff"] is not None:
            raise unavailable()
        new["scans"][kind]["cutoff"] = self.now()
        return self._owned_update(snapshot, new, lease_seconds=lease_seconds)

    def advance(self, snapshot, kind, cursor, *, lease_seconds, out_of_turn=False):
        new = self.validate(snapshot)
        self._turn(new, kind, out_of_turn)
        scan = new["scans"][kind]
        if scan["cutoff"] is None:
            raise unavailable()
        cursor = validate_cursor(cursor, kind, scan["cutoff"])
        if cursor is not None and cursor == scan["cursor"]:
            raise unavailable()
        new["scans"][kind] = {"cutoff": scan["cutoff"] if cursor is not None else None, "cursor": cursor}
        new["next_kind"] = "JOB" if kind == "OUTBOX" else "OUTBOX"
        return self._owned_update(snapshot, new, lease_seconds=lease_seconds)

    def release(self, snapshot):
        new = self.validate(snapshot)
        new.update(owner=None, lease_until=0, revision=snapshot["revision"] + 1)
        if not self._replace(snapshot, new):
            raise RelayProgressLost()
        return new

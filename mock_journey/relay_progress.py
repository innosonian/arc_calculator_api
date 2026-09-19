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

from mock_journey.errors import JourneyError
from mock_journey.settings import _queue_url
from mock_journey.state import _decode, _encode, _unavailable


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
        raise _unavailable()
    return value


def _kind(value):
    if type(value) is not str or value not in _KINDS:
        raise _unavailable()


def _uuid(value):
    try:
        if type(value) is not str or str(uuid.UUID(value)) != value:
            raise _unavailable()
    except (ValueError, AttributeError):
        raise _unavailable() from None


def validate_cursor(value, kind, cutoff):
    """Validate decoded real GSI keys; never reconstruct a midpoint cursor."""
    _kind(kind)
    _integer(cutoff)
    if value is None:
        return None
    if type(value) is not dict or set(value) != {"PK", "SK", "GSI1PK", "GSI1SK"}:
        raise _unavailable()
    prefix = kind + "#"
    if (type(value["PK"]) is not str or not value["PK"].startswith(prefix)
            or value["SK"] != ("DISPATCH" if kind == "OUTBOX" else "STATE")
            or value["GSI1PK"] != "DUE#" + kind):
        raise _unavailable()
    _uuid(value["PK"][len(prefix):])
    if _integer(value["GSI1SK"]) > cutoff:
        raise _unavailable()
    return deepcopy(value)


class DynamoRelayProgress:
    def __init__(self, state, *, environment, partition, account_id, region, queue_url):
        checks = ((environment, r"[A-Za-z0-9_.-]{1,128}"),
                  (account_id, r"[0-9]{12}"),
                  (region, r"[a-z]{2}(?:-[a-z]+)+-[0-9]+"),
                  (state.table_name, r"[A-Za-z0-9_.-]{3,255}"))
        if (any(type(v) is not str or not re.fullmatch(pattern, v) for v, pattern in checks)
                or partition not in ("aws", "aws-cn", "aws-us-gov")
                or region.startswith("cn-") != (partition == "aws-cn")
                or region.startswith("us-gov-") != (partition == "aws-us-gov")):
            raise ValueError("Invalid relay progress scope.")
        _queue_url(queue_url)
        self.state = state
        self.key = {"PK": "RELAY_SCAN#" + hashlib.sha256(environment.encode()).hexdigest(),
                    "SK": "PROGRESS#v1"}
        binding = [partition, account_id, region, state.table_name, environment, queue_url]
        self.binding_sha256 = hashlib.sha256(
            json.dumps(binding, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()

    def now(self):
        value = self.state.clock()
        if type(value) is int:
            return _integer(value)
        if type(value) is not float or not math.isfinite(value) or value < 0:
            raise _unavailable()
        return _integer(int(value))

    def validate(self, row):
        if (type(row) is not dict or set(row) != _FIELDS
                or any(row[k] != v for k, v in self.key.items())
                or type(row["schema"]) is not int or row["schema"] != 1
                or row["binding_sha256"] != self.binding_sha256):
            raise _unavailable()
        for key in ("revision", "fence", "lease_until"):
            _integer(row[key])
        if row["owner"] is None:
            if row["lease_until"] != 0:
                raise _unavailable()
        else:
            _uuid(row["owner"])
            if row["lease_until"] == 0 or row["fence"] == 0:
                raise _unavailable()
        if row["revision"] < row["fence"]:
            raise _unavailable()
        _kind(row["next_kind"])
        if type(row["scans"]) is not dict or set(row["scans"]) != set(_KINDS):
            raise _unavailable()
        for kind in _KINDS:
            scan = row["scans"][kind]
            if type(scan) is not dict or set(scan) != {"cutoff", "cursor"}:
                raise _unavailable()
            if scan["cutoff"] is None:
                if scan["cursor"] is not None:
                    raise _unavailable()
            else:
                validate_cursor(scan["cursor"], kind, scan["cutoff"])
        # Encoded JSON is a conservative upper bound on this small scalar/map
        # item's DynamoDB storage bytes. The exact shape cannot grow a history.
        if len(json.dumps(_encode(row), separators=(",", ":")).encode()) >= 400 * 1024:
            raise _unavailable()
        return deepcopy(row)

    def initial_item(self):
        return self.validate({**self.key, "schema": 1, "binding_sha256": self.binding_sha256,
                              "revision": 0, "owner": None, "fence": 0, "lease_until": 0,
                              "next_kind": "OUTBOX", "scans": {
                                  kind: {"cutoff": None, "cursor": None} for kind in _KINDS}})

    def initialization_request(self):
        return {"TableName": self.state.table_name, "Item": _encode(self.initial_item()),
                "ConditionExpression": "attribute_not_exists(PK)"}

    def initialize(self):
        """Explicit create-only operation; runtime must never call this."""
        row = self.initial_item()
        if not self._put(self.initialization_request(), row):
            raise _unavailable()
        return row

    def get(self):
        check_relay_call()
        return self.validate(self.state._get(self.key))

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
        return self._put({"TableName": self.state.table_name, "Item": _encode(new),
                          "ConditionExpression": expression, "ExpressionAttributeNames": names,
                          "ExpressionAttributeValues": _encode(values)}, new)

    def acquire(self, *, lease_seconds):
        if _integer(lease_seconds) == 0:
            raise _unavailable()
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
            raise _unavailable()
        new.update(revision=old["revision"] + 1, lease_until=self.now() + lease_seconds)
        if not self._replace(old, new):
            raise RelayProgressLost()
        return new

    def renew(self, snapshot, *, lease_seconds):
        return self._owned_update(snapshot, self.validate(snapshot), lease_seconds=lease_seconds)

    def begin_pass(self, snapshot, kind, *, lease_seconds):
        new = self.validate(snapshot)
        _kind(kind)
        if kind != new["next_kind"] or new["scans"][kind]["cutoff"] is not None:
            raise _unavailable()
        new["scans"][kind]["cutoff"] = self.now()
        return self._owned_update(snapshot, new, lease_seconds=lease_seconds)

    def advance(self, snapshot, kind, cursor, *, lease_seconds):
        new = self.validate(snapshot)
        _kind(kind)
        scan = new["scans"][kind]
        if kind != new["next_kind"] or scan["cutoff"] is None:
            raise _unavailable()
        cursor = validate_cursor(cursor, kind, scan["cutoff"])
        if cursor is not None and cursor == scan["cursor"]:
            raise _unavailable()
        new["scans"][kind] = {"cutoff": scan["cutoff"] if cursor is not None else None, "cursor": cursor}
        new["next_kind"] = "JOB" if kind == "OUTBOX" else "OUTBOX"
        return self._owned_update(snapshot, new, lease_seconds=lease_seconds)

    def release(self, snapshot):
        new = self.validate(snapshot)
        new.update(owner=None, lease_until=0, revision=snapshot["revision"] + 1)
        if not self._replace(snapshot, new):
            raise RelayProgressLost()
        return new

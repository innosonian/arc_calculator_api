"""DynamoDB client doubles shared by unit, integration and local-server tests. Never imported by runtime code.

* ``ScriptedClient``/``item``/``snapshot``: finite scripted SDK calls for
  control-flow tests (no condition semantics; real conditions run on
  tests/memory_dynamodb.py or DynamoDB Local).
* ``RecordingClient``: records every request like ``MemoryDynamoDB.calls`` in
  front of a real (DynamoDB Local) client.
* ``OneTransactionInterruption``: runs a race once around the first
  transaction of a real client; every condition stays real.
* ``Clock``: a settable clock callable.

The copies these replaced (tests/mock_state_support.py,
local_server_tests/scripted_dynamodb.py, tests/legacy_attempt_seeds.py,
integration_tests/worker_journey_support.py) re-export the same names.
"""

from copy import deepcopy
from threading import Lock

from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ReadTimeoutError
import pytest


SERIALIZER = TypeSerializer()


def item(value):
    return {key: SERIALIZER.serialize(nested) for key, nested in value.items()}


def snapshot(*values):
    return {"Responses": [{"Item": item(value)} if value else {} for value in values]}


class ScriptedClient:
    """Assert finite SDK calls without implementing DynamoDB expressions."""

    def __init__(self, calls):
        self.expected = list(calls)
        self.calls = []

    def _call(self, operation, kwargs):
        self.calls.append((operation, deepcopy(kwargs)))
        if not self.expected:
            pytest.fail(f"Unexpected {operation} call")
        expected, response = self.expected.pop(0)
        assert operation == expected
        if isinstance(response, BaseException):
            raise response
        return response

    def get_item(self, **kwargs):
        return self._call("get", kwargs)

    def put_item(self, **kwargs):
        return self._call("put", kwargs)

    def transact_get_items(self, **kwargs):
        return self._call("read", kwargs)

    def transact_write_items(self, **kwargs):
        return self._call("write", kwargs)

    def query(self, **kwargs):
        return self._call("query", kwargs)


class RecordingClient:
    """Record DynamoDB requests like MemoryDynamoDB.calls, for a real (DynamoDB Local) client."""

    _OPERATIONS = {"get_item": "GetItem", "put_item": "PutItem", "transact_get_items": "TransactGetItems",
                   "transact_write_items": "TransactWriteItems", "query": "Query", "scan": "Scan"}

    def __init__(self, client):
        self.client = client
        self.calls = []

    def __getattr__(self, name):
        method = getattr(self.client, name)
        operation = self._OPERATIONS.get(name)
        if operation is None:
            return method

        def recorded(**request):
            self.calls.append((operation, deepcopy(request)))
            return method(**request)
        return recorded


class Clock:
    def __init__(self, now=1_800_000_000):
        self.now = now

    def __call__(self):
        return self.now


class OneTransactionInterruption:
    """Run a race once around the first transaction, then delegate every condition to the real client."""

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

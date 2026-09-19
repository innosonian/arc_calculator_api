"""Independent Q26 fault/authority probes with injected clients; never AWS."""

from copy import deepcopy
import json

from botocore.exceptions import ReadTimeoutError
import pytest

from mock_journey.dispatch import run
from mock_journey.relay_progress import RelayProgressUncertain
from mock_journey.state import _decode
from tests.test_aws_relay_progress_integration import assembled
from tests.test_aws_runtime import context
from tests.test_relay_progress_unit import cursor, put, row, world


@pytest.mark.parametrize("committed", [False, True])
def test_security_unknown_checkpoint_with_no_time_makes_no_resolution_or_release_call(world, monkeypatch, committed):
    front, tail = row("JOB", 1), row("JOB", 2)
    put(world, front, tail)
    before = {world.db.key(value): deepcopy(value) for value in (front, tail)}
    original = world.db.put_item

    def uncertain(**request):
        proposed = _decode(request["Item"])
        if proposed["scans"]["JOB"]["cursor"] is not None:
            if committed:
                original(**request)
            else:
                world.db.calls.append(("put_item", deepcopy(request)))
            world.remaining[0] = 0
            raise ReadTimeoutError(endpoint_url="https://PRIVATE-MARKER.invalid")
        return original(**request)

    monkeypatch.setattr(world.db, "put_item", uncertain)
    world.db.calls.clear()
    with pytest.raises(RelayProgressUncertain) as error:
        world.relay().reconcile(context=world.context)
    assert error.value.committed is None
    assert "PRIVATE-MARKER" not in str(error.value)
    assert world.db.calls[-1][0] == "put_item"
    assert sum(name == "query" for name, _ in world.db.calls) == 2
    saved = world.db.rows[world.db.key(world.progress.key)]
    assert saved["owner"] is not None
    assert saved["scans"]["JOB"]["cursor"] == (cursor(front) if committed else None)
    assert all(world.db.rows[key] == value for key, value in before.items())
    assert world.sent == [front["job_id"]]


def test_security_event_cannot_supply_cursor_queue_scope_or_initialization(monkeypatch):
    runtime, factory = assembled(monkeypatch)
    runtime.target.clock = lambda: 1000
    runtime.target.progress.state.clock = lambda: 1000
    front, tail = row("JOB", 1), row("JOB", 2)
    for value in (front, tail):
        factory.db.rows[factory.db.key(value)] = deepcopy(value)
    before = {factory.db.key(value): deepcopy(value) for value in (front, tail)}
    event = {
        "source": "aws.events", "cursor": cursor(tail), "ExclusiveStartKey": cursor(tail),
        "queue_url": "https://PRIVATE-MARKER.invalid/queue", "environment": "PRIVATE-MARKER",
        "next_kind": "JOB", "reset": True, "initialize": True,
        "detail": {"cursor": cursor(tail), "table_name": "PRIVATE-MARKER"},
    }
    result = run(event, context())
    assert result["job_wakes"] >= 1
    assert json.loads(factory.sent[0]["MessageBody"]) == {"job_id": front["job_id"]}
    for request in factory.sent:
        assert request["QueueUrl"] == runtime.settings.role_settings.queue_url
        assert set(json.loads(request["MessageBody"])) == {"job_id"}
    progress_key = factory.db.key(runtime.target.progress.key)
    for name, request in factory.db.calls:
        assert name in {"query", "get_item", "put_item"}
        assert request["TableName"] == runtime.settings.state.table_name
        if name == "put_item":
            assert factory.db.key(_decode(request["Item"])) == progress_key
            assert request["ConditionExpression"] != "attribute_not_exists(PK)"
        elif name == "query":
            assert request["IndexName"] == "GSI1" and request["Limit"] == 1
            assert set(request["ExpressionAttributeNames"].values()) == {"PK", "SK", "GSI1PK", "GSI1SK"}
    assert all(factory.db.rows[key] == value for key, value in before.items())
    assert "PRIVATE-MARKER" not in json.dumps(factory.logs)
    assert "PRIVATE-MARKER" not in json.dumps(factory.db.calls)


@pytest.mark.parametrize("event_name", ["INSERT", "MODIFY", "REMOVE"])
def test_security_progress_stream_images_cannot_trigger_checkpoint_or_work(monkeypatch, event_name):
    runtime, factory = assembled(monkeypatch, initialize=False)
    record = {
        "eventName": event_name,
        "dynamodb": {
            "SequenceNumber": "opaque-sequence",
            "Keys": {name: {"S": value} for name, value in runtime.target.progress.key.items()},
            "NewImage": {"PK": {"S": "OUTBOX#PRIVATE-MARKER"}, "cursor": {"S": "PRIVATE-MARKER"}},
            "OldImage": {"secret": {"S": "PRIVATE-MARKER"}},
        },
    }
    assert run({"Records": [record]}, context()) == {"batchItemFailures": []}
    assert factory.db.calls == [] and factory.db.rows == {} and factory.sent == []
    assert "PRIVATE-MARKER" not in json.dumps(factory.logs)

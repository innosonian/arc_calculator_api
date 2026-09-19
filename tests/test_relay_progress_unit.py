"""Checkpoint fault boundaries with injected SDK doubles, never AWS access."""

from copy import deepcopy
from types import SimpleNamespace
import json
import uuid

from botocore.exceptions import ClientError, ReadTimeoutError
import pytest

from mock_journey.dispatch import OutboxRelay, handle_stream
from mock_journey.errors import JourneyError
from mock_journey.jobs import DynamoJobRepository
from mock_journey.relay_progress import (
    DynamoRelayProgress, RelayGuardedClient, RelayProgressLost,
    RelayProgressUncertain, RelayTimeStopped, relay_call_guard, validate_cursor,
)
from mock_journey.state import DynamoStateRepository, _decode, _encode
from services.operational_logs import _operation_fields
from tests.relay_progress_support import RelayDynamo


SCOPE = dict(environment="unit-relay", partition="aws", account_id="000000000000",
             region="us-east-2", queue_url="https://sqs.us-east-2.amazonaws.com/000000000000/unit-relay")


@pytest.fixture
def world():
    db, now, remaining, sent = RelayDynamo(), [1000], [100000], []
    state = DynamoStateRepository(RelayGuardedClient(db), "unit-relay-table", clock=lambda: now[0], max_conflict_retries=2)
    progress = DynamoRelayProgress(state, **SCOPE)
    progress.initialize()
    jobs = DynamoJobRepository(state)
    context = SimpleNamespace(get_remaining_time_in_millis=lambda: remaining[0])

    def relay():
        value = OutboxRelay(jobs, SimpleNamespace(send=lambda ident: sent.append(ident)),
                            progress=DynamoRelayProgress(state, **SCOPE),
                            lease_seconds=30, retry_seconds=5, page_size=1, max_pages=1,
                            clock=lambda: now[0])
        value.processing_reserve_ms = 100
        return value
    return SimpleNamespace(db=db, state=state, progress=progress, jobs=jobs, now=now,
                           remaining=remaining, sent=sent, context=context, relay=relay)


def row(kind, number, *, due=100):
    ident = str(uuid.UUID(int=number))
    return {"PK": kind + "#" + ident, "SK": "STATE" if kind == "JOB" else "DISPATCH",
            "GSI1PK": "DUE#" + kind, "GSI1SK": due, "next_due_at": due,
            "job_id": ident, "lease_until": 0, "state": "queued" if kind == "JOB" else "pending"}


def put(world, *rows):
    for value in rows:
        world.db.rows[world.db.key(value)] = deepcopy(value)


def cursor(value):
    return {key: value[key] for key in ("PK", "SK", "GSI1PK", "GSI1SK")}


def test_initialization_is_explicit_create_only_and_scope_bound(world):
    before = deepcopy(world.db.rows)
    with pytest.raises(JourneyError):
        world.progress.initialize()
    assert world.db.rows == before
    other = DynamoRelayProgress(world.state, **{**SCOPE, "queue_url": SCOPE["queue_url"] + "-changed"})
    assert other.key == world.progress.key
    with pytest.raises(JourneyError):
        other.get()
    assert world.db.rows == before
    missing = DynamoRelayProgress(world.state, **{**SCOPE, "environment": "missing"})
    count = len(world.db.calls)
    with pytest.raises(JourneyError):
        missing.acquire(lease_seconds=30)
    assert [name for name, _ in world.db.calls[count:]] == ["get_item"]


@pytest.mark.parametrize("mutate", [
    lambda value: value.update(schema=True),
    lambda value: value.update(schema=2),
    lambda value: value.update(revision=True),
    lambda value: value.update(fence=-1),
    lambda value: value.update(lease_until=1),
    lambda value: value.update(owner=str(uuid.uuid4())),
    lambda value: value.update(next_kind="ATTEMPT"),
    lambda value: value.update(TTL=0),
    lambda value: value.update(GSI1PK="DUE#JOB"),
    lambda value: value["scans"]["JOB"].update(cursor=cursor(row("JOB", 1))),
    lambda value: value["scans"]["JOB"].update(cutoff=True),
    lambda value: value["scans"]["JOB"].update(extra="PRIVATE-MARKER"),
])
def test_bad_stored_rows_fail_before_query_or_write(world, mutate):
    value = world.progress.initial_item()
    mutate(value)
    world.db.rows[world.db.key(value)] = deepcopy(value)
    world.db.calls.clear()
    with pytest.raises(JourneyError) as failure:
        world.relay().reconcile(context=world.context)
    assert failure.value.code == "TEMPORARILY_UNAVAILABLE"
    assert [name for name, _ in world.db.calls] == ["get_item"]
    assert not world.sent


@pytest.mark.parametrize("change", [
    {"PK": "SESSION#private"}, {"PK": "JOB#not-a-uuid"}, {"SK": "DISPATCH"},
    {"GSI1PK": "DUE#OUTBOX"}, {"GSI1SK": True}, {"GSI1SK": 1.5},
    {"GSI1SK": -1}, {"GSI1SK": 1001}, {"extra": "private"},
])
def test_cursor_rejects_foreign_namespace_or_unbounded_shape(change):
    with pytest.raises(JourneyError):
        validate_cursor({**cursor(row("JOB", 1)), **change}, "JOB", 1000)


def test_lease_fence_and_revision_reject_stale_owner(world):
    first = world.progress.acquire(lease_seconds=30)
    assert world.progress.acquire(lease_seconds=30) is None
    first = world.progress.begin_pass(first, "OUTBOX", lease_seconds=30)
    world.now[0] += 31
    successor = world.progress.acquire(lease_seconds=30)
    assert successor["fence"] == first["fence"] + 1
    assert successor["scans"] == first["scans"]
    for operation in (lambda: world.progress.release(first),
                      lambda: world.progress.advance(first, "OUTBOX", None, lease_seconds=30)):
        with pytest.raises(RelayProgressLost):
            operation()
    assert world.progress.get() == successor


@pytest.mark.parametrize("commits", [False, True])
def test_unknown_write_resolves_with_strong_read_and_stops(world, monkeypatch, commits):
    owned = world.progress.acquire(lease_seconds=30)
    original = world.db.put_item
    calls = []
    def uncertain(**request):
        calls.append(request)
        if commits:
            original(**request)
        raise ReadTimeoutError(endpoint_url="https://private.invalid")
    monkeypatch.setattr(world.db, "put_item", uncertain)
    with pytest.raises(RelayProgressUncertain) as failure:
        world.progress.begin_pass(owned, "OUTBOX", lease_seconds=30)
    assert failure.value.committed is commits and len(calls) == 1
    saved = world.progress.get()
    assert saved["revision"] == owned["revision"] + int(commits)
    assert saved["scans"]["OUTBOX"]["cutoff"] == (1000 if commits else None)


def test_acquisition_conflicts_are_bounded_and_never_reset(world, monkeypatch):
    def conflict(**request):
        world.db.calls.append(("put_item", request))
        raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")
    monkeypatch.setattr(world.db, "put_item", conflict)
    world.db.calls.clear()
    with pytest.raises(RelayProgressLost):
        world.progress.acquire(lease_seconds=30)
    assert [name for name, _ in world.db.calls] == ["get_item", "put_item"] * 2
    assert world.progress.get() == world.progress.initial_item()


@pytest.mark.parametrize("response", [
    {"Items": [_encode({**cursor(row("JOB", 1)), "extra": "private"})]},
    {"Items": [_encode(cursor(row("JOB", 1))), _encode(cursor(row("JOB", 2)))]},
    {"Items": [_encode(cursor(row("JOB", 1)))], "LastEvaluatedKey": _encode(cursor(row("JOB", 2)))},
    {"Items": [], "LastEvaluatedKey": []},
])
def test_raw_query_rejects_malformed_response_before_base_reads(world, monkeypatch, response):
    world.db.calls.clear()
    monkeypatch.setattr(world.db, "query", lambda **_: response)
    with pytest.raises(JourneyError):
        world.jobs.due_step("JOB", cutoff=1000)
    assert world.db.calls == []


def test_raw_query_uses_physical_limit_and_actual_key(world):
    front, tail = row("JOB", 1), row("JOB", 2)
    put(world, front, tail)
    raw, next_key = world.jobs.due_step("JOB", cutoff=999)
    assert raw == next_key == cursor(front)
    request = world.db.calls[-1][1]
    assert request["Limit"] == 1 and request["ScanIndexForward"] is True
    assert "ConsistentRead" not in request and "FilterExpression" not in request
    assert _decode(request["ExpressionAttributeValues"])[":cutoff"] == 999
    # Refreshing current state never replaces the real query continuation.
    world.db.rows[world.db.key(front)]["GSI1SK"] = 950
    assert world.jobs.due_current("JOB", raw, cutoff=999)["GSI1SK"] == 950
    assert next_key == cursor(front)


@pytest.mark.parametrize("change", [{"lease_until": 1001}, {"next_due_at": 1001},
                                    {"state": "done"}, {"GSI1PK": "not-due"}])
def test_current_base_eligibility_is_reread(world, change):
    initial = row("JOB", 1)
    put(world, {**initial, **change})
    assert world.jobs.due_current("JOB", cursor(initial), cutoff=1000) is None


def test_deadline_after_query_before_read_preserves_position(world):
    put(world, row("OUTBOX", 1), row("OUTBOX", 2))
    world.db.after_query = lambda: world.remaining.__setitem__(0, 100)
    world.db.calls.clear()
    assert world.relay().reconcile(context=world.context)["failures"] == 0
    saved = world.progress.get()
    assert saved["next_kind"] == "OUTBOX"
    assert saved["scans"]["OUTBOX"] == {"cutoff": 1000, "cursor": None}
    assert [name for name, _ in world.db.calls[:4]] == ["get_item", "put_item", "put_item", "query"]
    assert not world.sent


def test_lease_expiry_after_query_is_not_an_item_failure(world):
    put(world, row("OUTBOX", 1), row("OUTBOX", 2))
    world.db.after_query = lambda: world.now.__setitem__(0, 1031)
    with pytest.raises(RelayProgressLost):
        world.relay().reconcile(context=world.context)
    assert world.progress.get()["scans"]["OUTBOX"]["cursor"] is None
    assert world.progress.get()["next_kind"] == "OUTBOX" and not world.sent


@pytest.mark.parametrize("failure_stage", ["read", "send"])
def test_failed_head_is_considered_without_changing_jobs(world, monkeypatch, failure_stage):
    front, tail = row("JOB", 1), row("JOB", 2)
    put(world, front, tail)
    jobs_before = {world.db.key(value): deepcopy(value) for value in (front, tail)}
    original = world.jobs.due_current
    if failure_stage == "read":
        def get(kind, raw, **kwargs):
            if raw["PK"] == front["PK"]:
                raise JourneyError("TEMPORARILY_UNAVAILABLE")
            return original(kind, raw, **kwargs)
        monkeypatch.setattr(world.jobs, "due_current", get)
    def make_relay():
        relay = world.relay()
        if failure_stage == "send":
            def send(ident):
                if ident == front["job_id"]:
                    raise ReadTimeoutError(endpoint_url="https://private.invalid")
                world.sent.append(ident)
            relay.sender.send = send
        return relay
    assert make_relay().reconcile(context=world.context)["failures"] == 1
    assert world.progress.get()["scans"]["JOB"]["cursor"] == cursor(front)
    assert make_relay().reconcile(context=world.context)["job_wakes"] == 1
    assert world.sent == [tail["job_id"]]
    assert all(world.db.rows[key] == value for key, value in jobs_before.items())


def test_send_before_deadline_without_checkpoint_is_replayed(world):
    front, tail = row("JOB", 1), row("JOB", 2)
    put(world, front, tail)
    relay = world.relay()
    def send(ident):
        world.sent.append(ident)
        world.remaining[0] = 100
    relay.sender.send = send
    assert relay.reconcile(context=world.context)["job_wakes"] == 1
    saved = world.progress.get()
    assert saved["next_kind"] == "JOB" and saved["scans"]["JOB"]["cursor"] is None
    world.remaining[0] = 100000
    world.now[0] += 31
    world.relay().reconcile(context=world.context)
    assert world.sent[:2] == [front["job_id"], front["job_id"]]


def test_sticky_sdk_guard_interrupts_internal_command_before_second_call(world):
    put(world, row("OUTBOX", 1), row("OUTBOX", 2))
    invoked = []
    def claim(*args):
        world.state.client.get_item(TableName=world.state.table_name,
                                    Key=_encode(world.progress.key), ConsistentRead=True)
        invoked.append("read")
        world.remaining[0] = 100
        world.state.client.put_item(**world.progress.initialization_request())
        pytest.fail("Time guard allowed a second SDK operation")
    world.jobs.claim_outbox = claim
    assert world.relay().reconcile(context=world.context)["failures"] == 0
    assert invoked == ["read"] and not world.sent
    assert world.progress.get()["scans"]["OUTBOX"]["cursor"] is None


def test_guard_is_invocation_scoped_and_resets_on_failure():
    calls = []
    client = RelayGuardedClient(SimpleNamespace(query=lambda: calls.append("query")))
    with pytest.raises(RelayTimeStopped):
        with relay_call_guard(lambda: (_ for _ in ()).throw(RelayTimeStopped())):
            client.query()
    client.query()
    assert calls == ["query"]


def test_checkpoint_metadata_stream_is_ignored_and_log_fields_are_fixed(world):
    initial = world.progress.initial_item()
    record = {"eventName": "INSERT", "dynamodb": {"SequenceNumber": "1", "Keys": _encode(world.progress.key)}}
    assert handle_stream({"Records": [record]}, None, world.relay()) == {"batchItemFailures": []}
    assert not world.sent
    fields = dict(progress_busy=True, continuation=True, passes_completed=1, query_steps=2,
                  cursor="private", binding_sha256="private", row=initial)
    assert _operation_fields(fields) == {key: fields[key] for key in (
        "progress_busy", "continuation", "passes_completed", "query_steps")}
    assert len(json.dumps(_encode(initial)).encode()) < 4096


def test_maximum_supported_row_remains_bounded_before_serialization(world):
    maximum = 10**38 - 1
    value = world.progress.initial_item()
    value.update(owner=str(uuid.uuid4()), lease_until=maximum, revision=maximum, fence=maximum)
    for kind in ("OUTBOX", "JOB"):
        value["scans"][kind] = {"cutoff": maximum, "cursor": cursor(row(kind, 1, due=maximum))}
    assert world.progress.validate(value) == value
    assert len(json.dumps(_encode(value), separators=(",", ":")).encode()) < 4096
    value["revision"] += 1
    with pytest.raises(JourneyError):
        world.progress.validate(value)


def test_empty_query_with_real_continuation_advances_without_sending(world, monkeypatch):
    front, tail = row("JOB", 1), row("JOB", 2)
    put(world, front, tail)
    original = world.db.query
    def query(**request):
        response = original(**request)
        if _decode(request["ExpressionAttributeValues"])[":kind"] == "DUE#JOB" and "ExclusiveStartKey" not in request:
            response["Items"] = []
        return response
    monkeypatch.setattr(world.db, "query", query)
    assert world.relay().reconcile(context=world.context)["job_wakes"] == 0
    assert world.progress.get()["scans"]["JOB"]["cursor"] == cursor(front)
    assert world.relay().reconcile(context=world.context)["job_wakes"] == 1
    assert world.sent == [tail["job_id"]]


def test_committed_checkpoint_response_loss_stops_before_release_or_next_query(world, monkeypatch):
    put(world, row("JOB", 1), row("JOB", 2))
    original = world.db.put_item
    def put_response_lost(**request):
        value = _decode(request["Item"])
        original(**request)
        if value["scans"]["JOB"]["cursor"] is not None:
            raise ReadTimeoutError(endpoint_url="https://private.invalid")
    monkeypatch.setattr(world.db, "put_item", put_response_lost)
    world.db.calls.clear()
    with pytest.raises(RelayProgressUncertain) as failure:
        world.relay().reconcile(context=world.context)
    assert failure.value.committed is True
    assert sum(name == "query" for name, _ in world.db.calls) == 2
    assert len(world.sent) == 1
    saved = world.progress.get()
    assert saved["scans"]["JOB"]["cursor"] == cursor(row("JOB", 1))
    assert saved["next_kind"] == "OUTBOX" and saved["owner"] is not None


def test_single_completed_step_persists_alternation_and_fixed_cutoff(world):
    put(world, row("OUTBOX", 1), row("OUTBOX", 2), row("JOB", 1), row("JOB", 2))
    world.db.after_query = lambda: world.remaining.__setitem__(0, 500)
    for expected in ("JOB", "OUTBOX"):
        world.remaining[0] = 100000
        relay = world.relay()
        relay.relay_budget = SimpleNamespace(call_ms=10, step_ms=1000, acquire_ms=20, reserve_ms=100)
        relay.dispatch = lambda ident: world.sent.append(ident) or True
        relay.reconcile(context=world.context)
        assert world.progress.get()["next_kind"] == expected
        world.now[0] += 1
    saved = world.progress.get()
    assert saved["scans"]["OUTBOX"]["cutoff"] == 1000
    assert saved["scans"]["JOB"]["cutoff"] == 1001
    assert saved["owner"] is None and len(world.sent) == 2


def test_budget_refuses_invocation_before_acquisition(world):
    relay = world.relay()
    relay.relay_budget = SimpleNamespace(call_ms=10, step_ms=1000, acquire_ms=20, reserve_ms=100)
    world.remaining[0] = 1120
    world.db.calls.clear()
    assert relay.reconcile(context=world.context)["failures"] == 0
    assert world.db.calls == []


@pytest.mark.parametrize("now", [True, float("nan"), float("inf"), -1, 10**400])
def test_invalid_clock_is_a_fixed_unavailable_error(world, now):
    world.now[0] = now
    with pytest.raises(JourneyError) as failure:
        world.progress.now()
    assert failure.value.code == "TEMPORARILY_UNAVAILABLE"

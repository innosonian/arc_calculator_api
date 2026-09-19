"""Independent Relay progress acceptance tests against owned DynamoDB Local.

All table operations and index continuations use the local service. Queue sends
and selected transport failures are injected; these tests make no AWS claim.
The numeric lease/work limits below are test fixtures, not deployment defaults.
"""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from threading import Barrier
from types import SimpleNamespace
import uuid

from botocore.exceptions import ReadTimeoutError
import pytest

from integration_tests.test_mock_jobs_dynamodb import jobs_world  # noqa: F401
from integration_tests.test_mock_state_dynamodb import _encode as fixture_encode
from mock_journey.dispatch import OutboxRelay, handle_stream
from mock_journey.errors import JourneyError
from mock_journey.relay_progress import DynamoRelayProgress, RelayProgressLost
from mock_journey.state import _decode, _encode


class MeasuredClient:
    """Delegate every operation to the real DB; retain only this test's inputs."""

    def __init__(self, client):
        self.client = client
        self.calls = []
        self.after_query = None
        self.after_put = None
        self.before_put = None

    def __getattr__(self, name):
        method = getattr(self.client, name)
        if not callable(method):
            return method

        def call(**kwargs):
            self.calls.append((name, deepcopy(kwargs)))
            if name == "put_item" and self.before_put:
                self.before_put(kwargs)
            response = method(**kwargs)
            hook = self.after_query if name == "query" else self.after_put if name == "put_item" else None
            if hook:
                hook(kwargs, response)
            return response

        return call


class RecordingSender:
    def __init__(self, failing=()):
        self.sent = []
        self.attempted = []
        self.failing = set(failing)

    def send(self, job_id):
        self.attempted.append(job_id)
        if job_id in self.failing:
            raise JourneyError("TEMPORARILY_UNAVAILABLE")
        self.sent.append(job_id)


def job_fingerprints(world):
    return {
        row["PK"]: fixture_encode(row)
        for row in world.all_items()
        if row["PK"].startswith("JOB#")
    }


def accepted_jobs(world, count):
    auth = world.session()
    return [world.submit(auth)["job_id"] for _ in range(count)]


def retire_outboxes(world, job_ids):
    for job_id in job_ids:
        action, row = world.jobs.claim_outbox(job_id, "acceptance-setup", 60)
        assert action == "send"
        world.jobs.mark_outbox_sent(job_id, "acceptance-setup", row["fence"])


def indexed_ids(world, kind):
    """Independent full DB query oracle, not the dispatcher implementation."""
    response = world.client.query(
        TableName=world.table,
        IndexName="GSI1",
        KeyConditionExpression="#pk = :kind AND #sk <= :cutoff",
        ExpressionAttributeNames={"#pk": "GSI1PK", "#sk": "GSI1SK"},
        ExpressionAttributeValues=_encode({":kind": "DUE#" + kind, ":cutoff": world.clock()}),
        ScanIndexForward=True,
    )
    assert "LastEvaluatedKey" not in response
    return [_decode(row)["job_id"] for row in response["Items"]]


def progress_repository(world, client=None):
    return DynamoRelayProgress(
        world.repo(client or world.client),
        partition="aws",
        account_id="000000000000",
        region="us-east-2",
        environment="local-relay-progress-acceptance",
        queue_url="https://sqs.us-east-2.amazonaws.com/000000000000/local-relay-acceptance",
    )


def relay_instance(world, sender, client=None, *, page_size=1, max_pages=1):
    client = client or world.client
    return OutboxRelay(
        world.job_repo(client), sender,
        lease_seconds=30, retry_seconds=5, page_size=page_size, max_pages=max_pages,
        clock=world.clock, progress=progress_repository(world, client),
    )


@pytest.mark.parametrize("cold", [False, True], ids=["warm", "cold-every-invocation"])
def test_due_tail_reached_without_postponing_always_due_front(jobs_world, cold, record_property):
    w = jobs_world
    expected = accepted_jobs(w, 7)
    retire_outboxes(w, expected)
    oracle = indexed_ids(w, "JOB")
    before = job_fingerprints(w)
    measured = MeasuredClient(w.client)
    progress_repository(w, measured).initialize()
    sender = RecordingSender()
    relay = relay_instance(w, sender, measured)
    # Each kind has a one-position budget. Allow an invocation to stop after
    # either kind: persisted alternation must still reach the tail finitely.
    invocations = 0
    for _ in range(2 * len(expected)):
        invocations += 1
        if cold:
            relay = relay_instance(w, sender, measured)
        relay.reconcile()
        if len(sender.sent) == len(expected):
            break
    assert sender.sent == oracle
    assert len(set(sender.sent)) == len(expected)
    assert job_fingerprints(w) == before
    stored = progress_repository(w).get()
    assert len([r for r in w.all_items() if r["PK"].startswith("RELAY_SCAN#")]) == 1
    assert set(stored) == {
        "PK", "SK", "schema", "binding_sha256", "revision", "owner", "fence",
        "lease_until", "next_kind", "scans",
    }
    encoded_size = len(json.dumps(_encode(stored), separators=(",", ":")).encode())
    assert encoded_size < 4096
    queries = [kwargs for name, kwargs in measured.calls if name == "query"]
    assert len(queries) <= 2 * invocations
    assert all(request["Limit"] == 1 for request in queries)
    assert all(request["ScanIndexForward"] is True for request in queries)
    strong_reads = [kwargs for name, kwargs in measured.calls if name == "get_item"]
    assert strong_reads and all(request.get("ConsistentRead") is True for request in strong_reads)
    record_property("measured_operations", json.dumps(Counter(name for name, _ in measured.calls), sort_keys=True))
    record_property("checkpoint_serialized_ddb_json_bytes", encoded_size)
    record_property("unique_jobs_reached", len(set(sender.sent)))
    record_property("invocations_to_reach_tail", invocations)
    record_property("job_rows_unchanged", True)


def stop_after_considered_step(client, context):
    """Exhaust the clock only after a real durable alternation has committed."""
    seen_queries = [0]

    def after_query(_request, _response):
        seen_queries[0] += 1

    def after_put(request, _response):
        row = _decode(request["Item"])
        if row["PK"].startswith("RELAY_SCAN#") and seen_queries[0]:
            # begin_pass precedes Query; advance is its first following Put.
            context.remaining = 0
            client.after_put = None

    client.after_query = after_query
    client.after_put = after_put


def test_one_completed_step_per_cold_invocation_alternates_both_kinds(jobs_world, record_property):
    w = jobs_world
    expected = accepted_jobs(w, 4)
    initial_orders = {kind: indexed_ids(w, kind) for kind in ("OUTBOX", "JOB")}
    before = job_fingerprints(w)
    progress_repository(w).initialize()
    sender = RecordingSender()
    visited = {kind: [] for kind in initial_orders}
    observed_kinds = []
    row_sizes = []
    for _ in range(8):
        measured = MeasuredClient(w.client)
        context = SimpleNamespace(remaining=100_000)
        context.get_remaining_time_in_millis = lambda: context.remaining
        stop_after_considered_step(measured, context)
        relay = relay_instance(w, sender, measured, page_size=7)
        relay.processing_reserve_ms = 100
        starting = progress_repository(w).get()["next_kind"]
        sent_before = len(sender.sent)
        relay.reconcile(context=context)
        queries = [kwargs for name, kwargs in measured.calls if name == "query"]
        assert len(queries) == 1
        kind = _decode(queries[0]["ExpressionAttributeValues"])[":kind"].removeprefix("DUE#")
        assert kind == starting
        observed_kinds.append(kind)
        visited[kind].extend(sender.sent[sent_before:])
        saved = progress_repository(w).get()
        assert saved["next_kind"] != starting
        row_sizes.append(len(json.dumps(_encode(saved), separators=(",", ":")).encode()))
        # A simulated invocation with zero time could not release its lease.
        # A cold successor must defer until expiry, never steal ownership.
        busy_client = MeasuredClient(w.client)
        assert relay_instance(w, sender, busy_client).reconcile() == {
            "outbox_wakes": 0, "job_wakes": 0, "failures": 0,
        }
        assert not [name for name, _ in busy_client.calls if name == "query"]
        assert progress_repository(w).get() == saved
        w.clock.now += 31
    assert observed_kinds == ["OUTBOX", "JOB"] * 4
    assert visited == initial_orders
    assert set(sender.sent) == set(expected)
    assert job_fingerprints(w) == before
    assert max(row_sizes) < 4096
    record_property("full_cursor_checkpoint_max_serialized_ddb_json_bytes", max(row_sizes))
    record_property("one_step_cold_kind_order", ",".join(observed_kinds))


@pytest.mark.parametrize("change", ["delete", "move-future"])
def test_real_equal_sort_key_cursor_survives_source_delete_or_move(jobs_world, change):
    w = jobs_world
    expected = accepted_jobs(w, 5)
    retire_outboxes(w, expected)
    oracle = indexed_ids(w, "JOB")
    progress_repository(w).initialize()
    sender = RecordingSender()
    relay_instance(w, sender).reconcile()
    assert sender.sent == oracle[:1]
    checkpoint = progress_repository(w).get()
    cursor = checkpoint["scans"]["JOB"]["cursor"]
    assert cursor["PK"] == "JOB#" + oracle[0]
    assert cursor["GSI1SK"] == w.clock()
    first = w.jobs.get_job(oracle[0])
    if change == "delete":
        w.client.delete_item(TableName=w.table, Key=_encode({"PK": first["PK"], "SK": first["SK"]}))
    else:
        first["GSI1SK"] = first["next_due_at"] = w.clock() + 3600
        w.client.put_item(TableName=w.table, Item=_encode(first))
    for _ in range(4):
        relay_instance(w, sender).reconcile()
    assert sender.sent == oracle


def test_failed_head_wake_does_not_starve_later_jobs_or_rewrite_state(jobs_world):
    w = jobs_world
    expected = accepted_jobs(w, 4)
    retire_outboxes(w, expected)
    oracle = indexed_ids(w, "JOB")
    before = job_fingerprints(w)
    progress_repository(w).initialize()
    sender = RecordingSender(failing=oracle[:1])
    failures = 0
    for _ in range(6):
        failures += relay_instance(w, sender).reconcile()["failures"]
    assert sender.attempted[:4] == oracle
    assert sender.sent[:3] == oracle[1:]
    assert sender.attempted.count(oracle[0]) >= 2
    assert failures >= 2
    assert job_fingerprints(w) == before


def test_progress_stream_insert_modify_remove_never_send_or_reconcile(jobs_world):
    repo = progress_repository(jobs_world)
    repo.initialize()
    row = repo.get()
    relay = SimpleNamespace(dispatch=lambda *_: pytest.fail("Checkpoint stream enqueued a Job."))
    events = [{
        "eventName": name,
        "dynamodb": {"SequenceNumber": str(index), "Keys": _encode({"PK": row["PK"], "SK": row["SK"]})},
    } for index, name in enumerate(("INSERT", "MODIFY", "REMOVE"), 1)]
    assert handle_stream({"Records": events}, None, relay) == {"batchItemFailures": []}


def test_explicit_initialization_is_create_only_and_missing_runtime_fails_closed(jobs_world):
    w = jobs_world
    measured = MeasuredClient(w.client)
    sender = RecordingSender()
    with pytest.raises(JourneyError):
        relay_instance(w, sender, measured).reconcile()
    assert sender.sent == []
    assert not [name for name, _ in measured.calls if name in ("query", "put_item")]
    repo = progress_repository(w, measured)
    initial = repo.initialize()
    assert initial == repo.get()
    with pytest.raises(JourneyError):
        repo.initialize()
    assert repo.get() == initial
    initialization_puts = [kw for name, kw in measured.calls if name == "put_item"]
    assert len(initialization_puts) == 2
    assert all("attribute_not_exists" in kw["ConditionExpression"] for kw in initialization_puts)


def test_competing_real_conditional_acquisitions_have_one_owner_and_fence(jobs_world):
    w = jobs_world
    progress_repository(w).initialize()
    barrier = Barrier(2)
    clients = [MeasuredClient(w.client), MeasuredClient(w.client)]

    def synchronize_first_claim(request):
        row = _decode(request["Item"])
        if row["revision"] == 1 and row["owner"] is not None:
            barrier.wait(timeout=10)

    for client in clients:
        client.before_put = synchronize_first_claim
    repos = [progress_repository(w, client) for client in clients]
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(repo.acquire, lease_seconds=30) for repo in repos]
        results = [future.result(timeout=20) for future in futures]
    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    winner = winners[0]
    assert winner["fence"] == winner["revision"] == 1
    assert progress_repository(w).get() == winner
    assert winner["lease_until"] == w.clock() + 30


def test_expired_scan_owner_cannot_advance_or_release_successor(jobs_world):
    w = jobs_world
    repo = progress_repository(w)
    repo.initialize()
    old = repo.acquire(lease_seconds=30)
    old = repo.begin_pass(old, "OUTBOX", lease_seconds=30)
    w.clock.now += 31
    successor_repo = progress_repository(w)
    successor = successor_repo.acquire(lease_seconds=30)
    assert successor["fence"] == old["fence"] + 1
    assert successor["scans"] == old["scans"]
    with pytest.raises(RelayProgressLost):
        repo.advance(old, "OUTBOX", None, lease_seconds=30)
    with pytest.raises(RelayProgressLost):
        repo.release(old)
    assert successor_repo.get() == successor


def test_post_query_deadline_does_not_save_unconsidered_cursor(jobs_world):
    w = jobs_world
    expected = accepted_jobs(w, 3)
    retire_outboxes(w, expected)
    progress_repository(w).initialize()
    # Empty OUTBOX step legally switches to JOB before the interruption.
    repo = progress_repository(w)
    held = repo.acquire(lease_seconds=30)
    held = repo.begin_pass(held, "OUTBOX", lease_seconds=30)
    held = repo.advance(held, "OUTBOX", None, lease_seconds=30)
    repo.release(held)
    before = job_fingerprints(w)
    measured = MeasuredClient(w.client)
    context = SimpleNamespace(remaining=100_000)
    context.get_remaining_time_in_millis = lambda: context.remaining
    measured.after_query = lambda *_: setattr(context, "remaining", 0)
    sender = RecordingSender()
    relay = relay_instance(w, sender, measured)
    relay.processing_reserve_ms = 100
    relay.reconcile(context=context)
    saved = repo.get()
    assert saved["next_kind"] == "JOB"
    assert saved["scans"]["JOB"]["cursor"] is None
    assert sender.attempted == []
    assert job_fingerprints(w) == before
    w.clock.now += 31
    relay_instance(w, sender).reconcile()
    assert sender.sent == indexed_ids(w, "JOB")[:1]


def test_successful_cursor_commit_with_lost_response_resolves_without_reset(jobs_world):
    w = jobs_world
    expected = accepted_jobs(w, 3)
    retire_outboxes(w, expected)
    oracle = indexed_ids(w, "JOB")
    before = job_fingerprints(w)
    progress_repository(w).initialize()
    sender = RecordingSender()
    measured = MeasuredClient(w.client)
    committed = []

    def lose_job_advance_response(request, _response):
        row = _decode(request["Item"])
        if (row["PK"].startswith("RELAY_SCAN#") and row["next_kind"] == "OUTBOX"
                and row["scans"]["JOB"]["cursor"] is not None and not committed):
            committed.append(row)
            raise ReadTimeoutError(endpoint_url="http://127.0.0.1/local-progress-response-loss")

    measured.after_put = lose_job_advance_response
    with pytest.raises(JourneyError):
        relay_instance(w, sender, measured).reconcile()
    assert len(committed) == 1
    stored = progress_repository(w).get()
    assert stored["next_kind"] == "OUTBOX"
    assert stored["scans"] == committed[0]["scans"]
    assert sender.sent == oracle[:1]
    put_index = next(index for index, (name, kw) in enumerate(measured.calls)
                     if name == "put_item" and _decode(kw["Item"]) == committed[0])
    assert any(name == "get_item" and kw.get("ConsistentRead") is True
               for name, kw in measured.calls[put_index + 1:])
    w.clock.now += 31
    relay_instance(w, sender).reconcile()
    assert sender.sent == oracle[:2]
    assert job_fingerprints(w) == before


def test_stale_index_entry_uses_fresh_base_lease_and_keeps_real_cursor(jobs_world):
    w = jobs_world
    expected = accepted_jobs(w, 3)
    retire_outboxes(w, expected)
    oracle = indexed_ids(w, "JOB")
    progress_repository(w).initialize()
    measured = MeasuredClient(w.client)
    claims = []

    def claim_after_index_query(request, response):
        if not claims and response["Items"]:
            row = _decode(response["Items"][0])
            if row["PK"] == "JOB#" + oracle[0]:
                claims.append(w.jobs.claim(oracle[0], "independent-worker", 60))

    measured.after_query = claim_after_index_query
    sender = RecordingSender()
    relay_instance(w, sender, measured).reconcile()
    assert claims[0][0] == "execute"
    assert sender.sent == []
    stored = progress_repository(w).get()
    assert stored["scans"]["JOB"]["cursor"]["PK"] == "JOB#" + oracle[0]
    assert stored["scans"]["JOB"]["cursor"]["GSI1SK"] == w.clock()
    after_claim = job_fingerprints(w)
    relay_instance(w, sender).reconcile()
    assert sender.sent == oracle[1:2]
    assert job_fingerprints(w) == after_claim


def test_job_moving_behind_cursor_is_revisited_on_next_pass(jobs_world):
    w = jobs_world
    expected = accepted_jobs(w, 3)
    retire_outboxes(w, expected)
    oracle = indexed_ids(w, "JOB")
    progress_repository(w).initialize()
    sender = RecordingSender()
    relay_instance(w, sender).reconcile()
    assert sender.sent == oracle[:1]
    moved = w.jobs.get_job(oracle[-1])
    moved["next_due_at"] = moved["GSI1SK"] = w.clock() - 1
    w.client.put_item(TableName=w.table, Item=_encode(moved))
    for _ in range(4):
        relay_instance(w, sender).reconcile()
    assert set(sender.sent) == set(expected)
    # The newly earlier row is first in the following pass, never silently lost.
    assert sender.sent[:3] == [oracle[0], oracle[1], oracle[2]]


def test_pass_cutoff_excludes_newer_due_job_until_following_sweep(jobs_world):
    w = jobs_world
    expected = accepted_jobs(w, 2)
    retire_outboxes(w, expected)
    oracle = indexed_ids(w, "JOB")
    original_cutoff = w.clock()
    progress_repository(w).initialize()
    sender = RecordingSender()
    relay_instance(w, sender).reconcile()
    assert progress_repository(w).get()["scans"]["JOB"]["cutoff"] == original_cutoff
    w.clock.now += 1
    newcomer = accepted_jobs(w, 1)[0]
    retire_outboxes(w, [newcomer])
    relay_instance(w, sender).reconcile()
    assert sender.sent == oracle
    assert progress_repository(w).get()["scans"]["JOB"]["cutoff"] in (None, original_cutoff)
    for _ in range(5):
        relay_instance(w, sender).reconcile()
    assert newcomer in sender.sent

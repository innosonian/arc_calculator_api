"""Q9: checkpoint relay drains a one-sided backlog within one invocation's budget."""

from copy import deepcopy
from types import SimpleNamespace
import uuid

import pytest

from mock_journey.dispatch import OutboxRelay
from mock_journey.errors import JourneyError
from mock_journey.jobs import DynamoJobRepository
from mock_journey.relay_progress import DynamoRelayProgress, RelayGuardedClient, RelayProgressLost
from mock_journey.state import DynamoStateRepository, _decode
from tests.relay_progress_support import SCOPE, RelayDynamo, cursor, row


@pytest.fixture
def world():
    db, now, remaining = RelayDynamo(), [1000], [100000]
    state = DynamoStateRepository(RelayGuardedClient(db), "unit-relay-table", clock=lambda: now[0], max_conflict_retries=2)
    progress = DynamoRelayProgress(state, **SCOPE)
    progress.initialize()
    jobs = DynamoJobRepository(state)
    sent = {"OUTBOX": [], "JOB": []}
    context = SimpleNamespace(get_remaining_time_in_millis=lambda: remaining[0])

    def relay(*, page_size=5, max_pages=4, budget=True):
        value = OutboxRelay(jobs, SimpleNamespace(send=lambda ident: sent["JOB"].append(ident)),
                            progress=DynamoRelayProgress(state, **SCOPE),
                            lease_seconds=30, retry_seconds=5, page_size=page_size, max_pages=max_pages,
                            clock=lambda: now[0])
        value.processing_reserve_ms = 100
        if budget:
            value.relay_budget = SimpleNamespace(call_ms=10, step_ms=1000, acquire_ms=20, reserve_ms=100)
        value.dispatch = wake_outbox
        return value

    def wake_outbox(ident):
        # OUTBOX claim/mark-sent is covered elsewhere. A sent outbox leaves the
        # due index, as the real mark-sent does; a JOB wake changes nothing.
        sent["OUTBOX"].append(ident)
        db.rows[("OUTBOX#" + ident, "DISPATCH")]["GSI1PK"] = "SENT"
        return True
    return SimpleNamespace(db=db, state=state, progress=progress, now=now, remaining=remaining,
                           sent=sent, context=context, relay=relay)


def backlog(world, kind, count, *, start=1):
    rows = [row(kind, number) for number in range(start, start + count)]
    for value in rows:
        world.db.rows[world.db.key(value)] = deepcopy(value)
    return [value["job_id"] for value in rows]


def query_kinds(world):
    return [_decode(request["ExpressionAttributeValues"])[":kind"].removeprefix("DUE#")
            for name, request in world.db.calls if name == "query"]


def test_empty_outbox_does_not_limit_job_backlog_to_one_wake_per_invocation(world):
    oracle = backlog(world, "JOB", 50)
    result = world.relay().reconcile(context=world.context)
    # cap = page_size * max_pages = 20 per kind and invocation.
    assert result == {"outbox_wakes": 0, "job_wakes": 20, "failures": 0}
    assert world.sent["JOB"] == oracle[:20]
    saved = world.progress.get()
    assert saved["owner"] is None and saved["next_kind"] == "OUTBOX"
    assert saved["scans"]["JOB"] == {"cutoff": 1000, "cursor": cursor(row("JOB", 20))}
    assert saved["scans"]["OUTBOX"] == {"cutoff": None, "cursor": None}
    assert query_kinds(world) == ["OUTBOX"] + ["JOB"] * 20
    for expected in (40, 50):
        world.relay().reconcile(context=world.context)
        assert world.sent["JOB"] == oracle[:expected]
    saved = world.progress.get()
    assert saved["scans"]["JOB"] == {"cutoff": None, "cursor": None}
    assert len(set(world.sent["JOB"])) == 50 and world.sent["OUTBOX"] == []


def test_empty_job_scan_does_not_limit_outbox_backlog(world):
    oracle = backlog(world, "OUTBOX", 30)
    world.db.rows[world.db.key(world.progress.key)]["next_kind"] = "JOB"
    assert world.relay().reconcile(context=world.context)["outbox_wakes"] == 20
    assert world.sent["OUTBOX"] == oracle[:20]
    assert query_kinds(world) == ["JOB"] + ["OUTBOX"] * 20
    assert world.progress.get()["next_kind"] == "JOB"


def test_both_kinds_due_alternate_evenly_until_each_cap(world):
    outbox, jobs = backlog(world, "OUTBOX", 30), backlog(world, "JOB", 30, start=100)
    result = world.relay(page_size=5, max_pages=1).reconcile(context=world.context)
    assert result == {"outbox_wakes": 5, "job_wakes": 5, "failures": 0}
    assert query_kinds(world) == ["OUTBOX", "JOB"] * 5
    assert world.sent == {"OUTBOX": outbox[:5], "JOB": jobs[:5]}
    assert world.progress.get()["next_kind"] == "OUTBOX"


def test_short_kind_completes_then_other_continues_and_keeps_its_turn(world):
    outbox, jobs = backlog(world, "OUTBOX", 3), backlog(world, "JOB", 30, start=100)
    world.relay(page_size=5, max_pages=1).reconcile(context=world.context)
    # OUTBOX completes on its third step; JOB then uses the rest of its cap.
    assert query_kinds(world) == ["OUTBOX", "JOB"] * 3 + ["JOB"] * 2
    assert world.sent == {"OUTBOX": outbox, "JOB": jobs[:5]}
    saved = world.progress.get()
    assert saved["next_kind"] == "OUTBOX" and saved["scans"]["OUTBOX"]["cutoff"] is None
    # A later invocation starts a fresh OUTBOX pass first and resumes JOB.
    new_outbox = backlog(world, "OUTBOX", 1, start=50)
    world.now[0] += 1
    world.db.calls.clear()
    world.relay(page_size=5, max_pages=1).reconcile(context=world.context)
    assert query_kinds(world)[:2] == ["OUTBOX", "JOB"]
    assert world.sent["OUTBOX"] == outbox + new_outbox
    assert world.sent["JOB"] == jobs[:10]


def test_completed_kind_is_not_restarted_for_items_arriving_in_the_same_invocation(world):
    backlog(world, "OUTBOX", 1)
    jobs = backlog(world, "JOB", 10, start=100)
    arrivals = []

    def arrive():
        # New due OUTBOX rows appear after OUTBOX completed in this invocation.
        if query_kinds(world).count("JOB") == 2 and not arrivals:
            arrivals.extend(backlog(world, "OUTBOX", 3, start=200))
    world.db.after_query = arrive
    world.relay().reconcile(context=world.context)
    assert query_kinds(world).count("OUTBOX") == 1
    assert len(world.sent["OUTBOX"]) == 1 and not set(arrivals) & set(world.sent["OUTBOX"])
    assert world.sent["JOB"] == jobs and len(set(world.sent["JOB"])) == len(world.sent["JOB"])
    world.db.after_query = None
    world.relay().reconcile(context=world.context)
    assert world.sent["OUTBOX"][1:] == arrivals


def test_time_budget_stop_resumes_at_saved_position_and_kind(world):
    oracle = backlog(world, "JOB", 50)
    relay = world.relay()

    def send(ident):
        world.sent["JOB"].append(ident)
        if len(world.sent["JOB"]) == 7:
            # Enough for this step's checkpoint and release, not another step.
            world.remaining[0] = 500
    relay.sender.send = send
    assert relay.reconcile(context=world.context)["job_wakes"] == 7
    saved = world.progress.get()
    assert saved["owner"] is None and saved["next_kind"] == "OUTBOX"
    assert saved["scans"]["JOB"]["cursor"] == cursor(row("JOB", 7))
    world.remaining[0] = 100000
    world.db.calls.clear()
    world.relay().reconcile(context=world.context)
    assert query_kinds(world) == ["OUTBOX"] + ["JOB"] * 20
    assert world.sent["JOB"] == oracle[:27]


def test_hard_time_stop_after_send_replays_only_the_unsaved_item(world):
    oracle = backlog(world, "JOB", 50)
    relay = world.relay()

    def send(ident):
        world.sent["JOB"].append(ident)
        if len(world.sent["JOB"]) == 7:
            world.remaining[0] = 100
    relay.sender.send = send
    assert relay.reconcile(context=world.context)["job_wakes"] == 7
    saved = world.progress.get()
    # No checkpoint or release call follows a sticky stop; the owner remains.
    assert saved["owner"] is not None and saved["scans"]["JOB"]["cursor"] == cursor(row("JOB", 6))
    world.remaining[0] = 100000
    world.now[0] += 31
    world.relay().reconcile(context=world.context)
    assert world.sent["JOB"] == oracle[:7] + oracle[6:26]


@pytest.mark.parametrize("budget", [True, False])
def test_idle_invocation_sdk_calls_are_unchanged(world, budget):
    world.db.calls.clear()
    assert world.relay(budget=budget).reconcile(context=world.context) == {
        "outbox_wakes": 0, "job_wakes": 0, "failures": 0}
    assert [name for name, _ in world.db.calls] == [
        "get_item", "put_item",
        "put_item", "query", "put_item",
        "put_item", "query", "put_item",
        "put_item"]
    saved = world.progress.get()
    assert saved["next_kind"] == "OUTBOX" and saved["owner"] is None
    assert all(scan == {"cutoff": None, "cursor": None} for scan in saved["scans"].values())


def test_long_single_kind_run_keeps_one_fenced_owner(world):
    oracle = backlog(world, "JOB", 50)
    world.db.after_query = lambda: world.now.__setitem__(0, world.now[0] + 1)
    fence = world.progress.get()["fence"]
    world.relay(page_size=50, max_pages=1).reconcile(context=world.context)
    assert world.sent["JOB"] == oracle
    saved = world.progress.get()
    assert saved["fence"] == fence + 1 and saved["owner"] is None


def test_turn_override_is_explicit_and_fenced(world):
    owned = world.progress.acquire(lease_seconds=30)
    assert owned["next_kind"] == "OUTBOX"
    for kind, flag in (("OUTBOX", True), ("JOB", False), ("JOB", 1), ("JOB", None)):
        with pytest.raises(JourneyError):
            world.progress.begin_pass(owned, kind, lease_seconds=30, out_of_turn=flag)
    owned = world.progress.begin_pass(owned, "JOB", lease_seconds=30, out_of_turn=True)
    assert owned["next_kind"] == "OUTBOX" and owned["scans"]["JOB"]["cutoff"] == 1000
    with pytest.raises(JourneyError):
        world.progress.advance(owned, "JOB", None, lease_seconds=30)
    stale = owned
    owned = world.progress.advance(owned, "JOB", None, lease_seconds=30, out_of_turn=True)
    assert owned["next_kind"] == "OUTBOX" and owned["scans"]["JOB"] == {"cutoff": None, "cursor": None}
    with pytest.raises(RelayProgressLost):
        world.progress.advance(stale, "JOB", None, lease_seconds=30, out_of_turn=True)
    assert set(owned) == set(world.progress.initial_item())

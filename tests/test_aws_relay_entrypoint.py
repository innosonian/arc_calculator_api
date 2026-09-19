"""Relay entrypoint and bounded work with fake SDK clients; no AWS access."""

import json
from types import SimpleNamespace
import uuid

import pytest

from mock_journey.aws_runtime import build_runtime
from mock_journey.dispatch import OutboxRelay, handle_stream, run
from mock_journey.errors import JourneyError
from services.operational_logs import log_context, record_event
from tests.test_aws_runtime import context, environment
from tests.relay_progress_support import RelaySdk


def stream_record(ident, sequence):
    return {"eventName": "INSERT", "dynamodb": {"SequenceNumber": sequence, "Keys": {
        "PK": {"S": "OUTBOX#" + ident}, "SK": {"S": "DISPATCH"},
    }, "NewImage": {"private": "PRIVATE-RELAY-MARKER"}}}


def attach(monkeypatch):
    import mock_journey.worker_runtime as module
    factory = RelaySdk()
    runtime = build_runtime("relay", environment("relay"), client_factory=factory)
    # Simulate the separately reviewed create-only bootstrap in this fake DB.
    # Scheduled runtime must read this row; no injected in-memory cursor bypass.
    factory.db.put_item(**runtime.target.progress.initialization_request())
    factory.db.calls.clear()
    monkeypatch.setattr(module, "get_relay", lambda: runtime.target)
    return runtime, factory


def test_scheduled_relay_runs_with_invocation_logs_and_flushes(monkeypatch):
    runtime, factory = attach(monkeypatch)
    assert run({"source": "aws.events"}, context()) == {"outbox_wakes": 0, "job_wakes": 0, "failures": 0}
    assert any(name == "get_item" for name, _ in factory.db.calls)
    assert sum(name == "query" for name, _ in factory.db.calls) == 2
    records = [json.loads(row["Item"]["record"]["S"]) for row in factory.logs]
    assert len(records) == 1 and records[0]["event"] == "relay_reconciled"
    assert records[0]["role"] == "relay" and records[0]["fields"]["scan_exhausted"] is False
    assert runtime.operations.status()["stored"] == 1


@pytest.mark.parametrize("event", [{"source": "aws.events"}, {"Records": []}])
def test_relay_checks_runtime_account_region_before_any_job_work(monkeypatch, event):
    runtime, factory = attach(monkeypatch)
    runtime.target.jobs = SimpleNamespace(due_outbox=lambda **_: pytest.fail("Mismatched ARN reached Jobs"))
    wrong = context()
    wrong.invoked_function_arn = "arn:aws:lambda:eu-west-1:123456789012:function:other"
    with pytest.raises(JourneyError) as error:
        run(event, wrong)
    assert error.value.code == "TEMPORARILY_UNAVAILABLE"
    assert factory.logs == []


def test_stream_invocation_log_failure_does_not_change_success(monkeypatch, capsys):
    from mock_journey.aws_logs import InvocationLogs
    runtime, factory = attach(monkeypatch)
    def failed_store():
        raise RuntimeError("PRIVATE-RELAY-MARKER")
    operations = InvocationLogs(failed_store, role="relay", settings=runtime.settings.logs, warning=lambda _: None)
    runtime.operations = operations
    calls = []
    def dispatch(ident):
        calls.append(ident)
        record_event("relay_reconciled", outbox_wakes=1, failures=0)
        return True
    runtime.target.dispatch = dispatch
    ident = str(uuid.uuid4())
    assert run({"Records": [stream_record(ident, "10")]}, context()) == {"batchItemFailures": []}
    assert calls == [ident] and operations.status()["unconfirmed"] == 1
    assert "PRIVATE-RELAY-MARKER" not in capsys.readouterr().out


def test_stream_defers_only_unprocessed_records_near_deadline():
    calls = []
    times = iter((1000, 100))
    ctx = context()
    ctx.get_remaining_time_in_millis = lambda: next(times)
    relay = SimpleNamespace(processing_reserve_ms=500, dispatch=lambda ident: calls.append(ident) or True)
    ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    result = handle_stream({"Records": [stream_record(ident, str(i + 10)) for i, ident in enumerate(ids)]}, ctx, relay)
    assert result == {"batchItemFailures": [{"itemIdentifier": "11"}]}
    assert calls == ids[:1]


def test_page_cap_records_exhaustion_and_leaves_runnable_time_untouched():
    calls, rows, records = [], [{"job_id": str(uuid.uuid4()), "next_due_at": 100}], []
    def due_jobs(**kwargs):
        calls.append(kwargs)
        return rows, {"PK": "opaque-continuation"}
    relay = OutboxRelay(SimpleNamespace(due_outbox=lambda **_: ([], None), due_jobs=due_jobs),
                        SimpleNamespace(send=lambda _: None), lease_seconds=1, retry_seconds=1,
                        page_size=1, max_pages=2)
    recorder = SimpleNamespace(record=lambda category, event, fields: records.append((event, fields)))
    with log_context(recorder):
        result = relay.reconcile()
    assert result == {"outbox_wakes": 0, "job_wakes": 2, "failures": 0}
    assert len(calls) == 2 and rows[0]["next_due_at"] == 100
    assert records == [("relay_reconciled", {**result, "scan_exhausted": True})]


def test_reconcile_reserves_return_time_before_query_and_emits_incomplete_scan(monkeypatch):
    runtime, factory = attach(monkeypatch)
    runtime.target.jobs = SimpleNamespace(due_outbox=lambda **_: pytest.fail("Insufficient reserve queried DB"),
                                         due_jobs=lambda **_: pytest.fail("Insufficient reserve queried DB"))
    assert run({"source": "aws.events"}, context(remaining=100)) == {"outbox_wakes": 0, "job_wakes": 0, "failures": 0}
    record = json.loads(factory.logs[0]["Item"]["record"]["S"])
    assert record["fields"]["scan_exhausted"] is True


def test_query_failure_is_sanitized_but_flushes_failure_count(monkeypatch, capsys):
    runtime, factory = attach(monkeypatch)
    factory.db.query_error = RuntimeError("PRIVATE-RELAY-MARKER")
    with pytest.raises(JourneyError) as error:
        run({"source": "aws.events"}, context())
    assert error.value.code == "TEMPORARILY_UNAVAILABLE"
    assert "PRIVATE-RELAY-MARKER" not in str(error.value) + capsys.readouterr().out
    assert json.loads(factory.logs[0]["Item"]["record"]["S"])["fields"]["failures"] == 1


@pytest.mark.parametrize("event", [{}, {"source": "PRIVATE-RELAY-MARKER"}, {"Records": [None]},
                                   {"Records": [{"dynamodb": "PRIVATE-RELAY-MARKER"}]}])
def test_malformed_relay_envelope_is_fixed_error(monkeypatch, event):
    attach(monkeypatch)
    with pytest.raises(JourneyError) as error:
        run(event, context())
    assert error.value.code == "TEMPORARILY_UNAVAILABLE" and error.value.__suppress_context__
    assert "PRIVATE-RELAY-MARKER" not in str(error.value)


def test_relay_initialization_error_is_fixed(monkeypatch):
    import mock_journey.worker_runtime as module
    def failed():
        raise RuntimeError("PRIVATE-RELAY-MARKER")
    monkeypatch.setattr(module, "get_relay", failed)
    with pytest.raises(JourneyError) as error:
        run({}, context())
    assert error.value.code == "TEMPORARILY_UNAVAILABLE" and "PRIVATE-RELAY-MARKER" not in str(error.value)

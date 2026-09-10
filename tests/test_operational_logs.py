"""Privacy, finite memory and failure isolation of optional operational logs."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import threading
import time
import uuid
from types import SimpleNamespace

import pytest

from mock_journey.dispatch import handle_stream
from mock_journey.log_storage import DynamoLogStore, OperationalLogError
from services.operational_logs import (
    AsyncLogRecorder, log_context, record_event, validate_record, write_diagnostic,
)

STAMP = datetime(2026, 9, 10, 0, 0, tzinfo=timezone.utc)
MARKER = "PRIVATE-LOG-MARKER-NEVER-STORE"


class MemoryStore:
    def __init__(self):
        self.records = []
        self.closed = False
    def write(self, raw):
        self.records.append(json.loads(raw))
    def close(self):
        self.closed = True


def recorder(store, **kwargs):
    return AsyncLogRecorder(lambda: store, role="api", clock=lambda: STAMP, **kwargs)


def drain(log):
    assert log.close(timeout=2)
    assert log.status()["pending"] == 0


def test_major_and_detailed_records_never_store_caller_secrets(capsys):
    store = MemoryStore()
    log = recorder(store)
    ident = str(uuid.uuid4())
    try:
        with log_context(log, request_id=ident, session_id=ident, Authorization=MARKER):
            record_event("login_failed", error_code="LOGIN_FAILED", password=MARKER,
                         body={"token": MARKER}, chart_url=MARKER, reason=MARKER)
            try:
                raise ValueError(MARKER)
            except ValueError as error:
                write_diagnostic("error", "calc_failed", {"exception": error, "body": MARKER, "calc_ms": 7})
        drain(log)
        raw = json.dumps(store.records)
        assert MARKER not in raw and MARKER not in capsys.readouterr().out
        assert len(store.records) == 2
        assert store.records[1]["fields"]["error_message"] == "Exception details redacted."
        assert store.records[1]["fields"]["calc_ms"] == 7
        assert store.records[1]["fields"]["stacktrace"][0].startswith("tests/test_operational_logs.py:")
        assert all(row["fields"]["request_id"] == ident for row in store.records)
        assert all(validate_record(row) for row in store.records)
    finally:
        log.close()


def test_context_is_not_shared_by_threads_or_leaked_after_exception(capsys):
    store = MemoryStore(); log = recorder(store)
    barrier = threading.Barrier(8)
    ids = [str(uuid.uuid4()) for _ in range(8)]
    def task(ident):
        with pytest.raises(RuntimeError), log_context(log, request_id=ident):
            barrier.wait(timeout=3)
            record_event("attempt_created", attempt_id=ident)
            raise RuntimeError()
        record_event("logout_succeeded")  # outside binding: no DB write
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(task, ids))
        drain(log)
        assert len(store.records) == 8
        assert {r["fields"]["request_id"] for r in store.records} == set(ids)
        assert all(r["fields"]["request_id"] == r["fields"]["attempt_id"] for r in store.records)
        write_diagnostic("info", "calc_complete", {"elapsed_ms": 3})
        assert json.loads(capsys.readouterr().out)["elapsed_ms"] == 3
    finally:
        log.close()


def test_blocked_store_and_full_queue_do_not_block_emit_or_close():
    entered, release = threading.Event(), threading.Event()
    class Blocked(MemoryStore):
        def write(self, raw):
            entered.set()
            assert release.wait(5)
            super().write(raw)
    store = Blocked(); log = recorder(store, capacity=2, warning=lambda _: None)
    try:
        with log_context(log):
            record_event("login_succeeded")
            assert entered.wait(2)
            before = time.monotonic()
            for _ in range(1000):
                record_event("attempt_created")
            assert time.monotonic() - before < 2
            status = log.status()
            assert status["accepted"] == 3 and status["dropped"] == 998
            assert log.close(timeout=0.01) is False
            record_event("logout_succeeded")
            assert log.status()["dropped"] == 999
        release.set()
        drain(log)
        assert log.status()["stored"] == 3
    finally:
        release.set(); log.close()


@pytest.mark.parametrize("factory_failure", [False, True])
def test_failed_storage_or_client_creation_is_unconfirmed_and_can_recover(factory_failure):
    failed = threading.Event(); recovered = threading.Event(); warnings = []
    store = MemoryStore()
    def factory():
        if factory_failure and not recovered.is_set():
            failed.set(); raise RuntimeError(MARKER)
        return store
    original = store.write
    def write(raw):
        if not recovered.is_set():
            failed.set(); raise RuntimeError(MARKER)
        original(raw)
    store.write = write
    log = AsyncLogRecorder(factory, role="worker", warning=warnings.append)
    try:
        with log_context(log):
            record_event("calculation_started")
            assert failed.wait(2)
            recovered.set()
            record_event("calculation_completed")
        drain(log)
        assert log.status()["stored"] == 1 and log.status()["unconfirmed"] == 1
        assert warnings == ["operational_log_write_unconfirmed"]
        assert MARKER not in json.dumps(store.records)
    finally:
        log.close()


def test_warning_failure_or_blocked_stderr_does_not_hold_admission_lock():
    entered, release = threading.Event(), threading.Event()
    def broken_write(_): raise OSError(MARKER)
    def warning(_):
        entered.set(); release.wait(3); raise OSError(MARKER)
    log = recorder(SimpleNamespace(write=broken_write, close=lambda: None), warning=warning)
    try:
        with log_context(log):
            record_event("login_succeeded")
            assert entered.wait(2)
            record_event("attempt_created")
            assert log.status()["accepted"] == 2
            assert not log.close(timeout=0.01)
        release.set(); drain(log)
    finally:
        release.set(); log.close()


def test_logging_thread_start_failure_does_not_raise(monkeypatch):
    def fail(_): raise RuntimeError(MARKER)
    monkeypatch.setattr(threading.Thread, "start", fail)
    log = recorder(MemoryStore())
    with log_context(log): record_event("login_succeeded")
    assert log.status()["running"] is False and log.status()["dropped"] == 1
    assert log.close()


def test_bad_record_or_oversized_trace_is_dropped_without_breaking_caller():
    log = recorder(MemoryStore())
    try:
        assert log.record("operation", MARKER, {}) is False
        assert log.record("operation", "login_succeeded", {"body": MARKER}) is False
        assert log.record("diagnostic", "calc_failed", {
            "level": "error", "message": "calc_failed", "stacktrace": [MARKER]}) is False
        assert log.status()["dropped"] == 3
    finally:
        log.close()


def raw_record():
    return validate_record({"schema": 1, "log_id": str(uuid.uuid4()), "occurred_at": "2026-09-10T00:00:00.000000Z",
                            "role": "api", "category": "operation", "event": "login_succeeded", "fields": {}})


def test_append_uses_private_namespace_no_ttl_no_job_index_and_no_overwrite():
    calls = []
    client = SimpleNamespace(put_item=lambda **kw: calls.append(kw), close=lambda: None)
    store = DynamoLogStore(client, "owned-table", "local-installation")
    store.write(raw_record())
    call = calls[0]; row = call["Item"]
    assert call["ConditionExpression"] == "attribute_not_exists(PK)"
    assert row["PK"]["S"].startswith("OPS#") and row["PK"]["S"].endswith("#2026-09-10")
    assert set(row) == {"PK", "SK", "kind", "record", "sha256"}
    assert handle_stream({"Records": [{"eventName": "INSERT", "dynamodb": {
        "SequenceNumber": "1", "Keys": {k: row[k] for k in ("PK", "SK")}}}]}, None,
        SimpleNamespace(dispatch=lambda _: pytest.fail("Log row became a calculation wake"))) == {"batchItemFailures": []}


@pytest.mark.parametrize("bad", [b'{"schema":1,"schema":1}', b'{}', b'[]', b'x' * 16_385])
def test_invalid_persisted_payload_never_reaches_db(bad):
    client = SimpleNamespace(put_item=lambda **kw: pytest.fail("Invalid log reached DB"))
    with pytest.raises(OperationalLogError, match="unavailable or invalid"):
        DynamoLogStore(client, "owned-table", "local-test").write(bad)


def test_log_page_refuses_corruption_foreign_partition_and_foreign_cursor():
    written = []
    client = SimpleNamespace(put_item=lambda **kw: written.append(kw["Item"]))
    store = DynamoLogStore(client, "owned-table", "local-test")
    store.write(raw_record()); row = written[0]
    client.query = lambda **kw: {"Items": [row], "LastEvaluatedKey": {k: row[k] for k in ("PK", "SK")}}
    page = store.read_page("2026-09-10")
    assert len(page["records"]) == 1 and page["next_cursor"] == row["SK"]["S"]
    with pytest.raises(OperationalLogError): store.read_page("2026-09-10", after="SESSION#secret")
    row["PK"] = {"S": "SESSION#secret"}
    with pytest.raises(OperationalLogError): store.read_page("2026-09-10")


def test_duplicate_put_accepts_only_the_exact_existing_record():
    from botocore.exceptions import ClientError
    saved = {}
    def put(**kw):
        saved.setdefault("item", kw["Item"])
        raise ClientError({"Error": {"Code": "ConditionalCheckFailedException", "Message": MARKER}}, "PutItem")
    client = SimpleNamespace(put_item=put, get_item=lambda **kw: {"Item": saved["item"]})
    store = DynamoLogStore(client, "owned-table", "local-test")
    raw = raw_record(); store.write(raw)
    saved["item"]["sha256"] = {"S": "0" * 64}
    with pytest.raises(OperationalLogError): store.write(raw)

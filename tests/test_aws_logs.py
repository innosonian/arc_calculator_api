"""Blocking fake sinks exercise bounded return and warm invocation isolation."""

from dataclasses import replace
import json
import threading
import time
from types import SimpleNamespace

import pytest

from mock_journey.aws_logs import InvocationLogs
from mock_journey.aws_settings import AwsSettings
from services.operational_logs import record_event, write_diagnostic
from tests.test_aws_runtime import configuration, context


def settings():
    return AwsSettings.parse(json.dumps(configuration()), "api").logs


def test_normal_records_and_warm_invocations_keep_context_and_counts_separate():
    records = []
    log = InvocationLogs(lambda: SimpleNamespace(write=lambda raw: records.append(json.loads(raw)), close=lambda: None),
                         role="api", settings=settings())
    one, two = context(), context()
    with log.invocation(one) as first:
        record_event("attempt_created", attempt_id=one.aws_request_id, raw_body="PRIVATE-MARKER")
    with log.invocation(two) as second:
        record_event("login_succeeded")
    assert first.status()["stored"] == second.status()["stored"] == 1
    assert records[0]["fields"] == {"request_id": one.aws_request_id, "attempt_id": one.aws_request_id}
    assert records[1]["fields"] == {"request_id": two.aws_request_id}
    assert "PRIVATE-MARKER" not in json.dumps(records)


@pytest.mark.parametrize("block_at", ["factory", "write", "close", "warning"])
def test_stuck_sink_never_accumulates_writers_or_blocks_business_return(block_at):
    entered, release = threading.Event(), threading.Event()
    records, factories = [], []
    def block():
        entered.set()
        assert release.wait(3)
    def write(raw):
        if block_at == "write":
            block()
        if block_at == "warning":
            raise RuntimeError("PRIVATE-MARKER")
        records.append(raw)
    def create():
        factories.append(True)
        if block_at == "factory":
            block()
        return SimpleNamespace(write=write, close=block if block_at == "close" else lambda: None)
    log = InvocationLogs(create, role="api", settings=settings(),
                         warning=lambda _: block() if block_at == "warning" else None)
    try:
        started = time.monotonic()
        with log.invocation(context()) as first:
            record_event("calculation_accepted")
            business = {"statusCode": 202, "body": "stored input"}
        assert business["statusCode"] == 202
        assert time.monotonic() - started < 0.4
        assert entered.wait(0.2)
        writer = log.writer
        for _ in range(5):
            with log.invocation(context()) as later:
                record_event("login_succeeded")
            assert later.status()["dropped"] == 1
            assert log.writer is writer
        assert len(factories) == 1
        if block_at in ("factory", "write", "warning"):
            assert first.status()["unconfirmed"] == 1
    finally:
        release.set()
        if log.writer:
            log.writer.join(1)
    assert not log.writer.is_alive()


def test_no_remaining_budget_skips_sdk_creation_and_reports_dropped():
    calls = []
    log = InvocationLogs(lambda: calls.append(True), role="api", settings=settings())
    with log.invocation(context(remaining=5)) as buffer:
        record_event("calculation_completed")
    assert calls == [] and buffer.status()["stored"] == 0 and buffer.status()["dropped"] == 1


def test_capacity_bytes_and_seal_are_bounded():
    calls = []
    cfg = replace(settings(), capacity=1)
    log = InvocationLogs(lambda: SimpleNamespace(write=lambda raw: calls.append(raw), close=lambda: None),
                         role="api", settings=cfg)
    with log.invocation(context()) as buffer:
        record_event("login_succeeded")
        record_event("login_succeeded")
        write_diagnostic("error", "request_failed", {"exception": RuntimeError("PRIVATE-MARKER")})
    assert len(calls) == 1 and buffer.status()["dropped"] == 2
    assert buffer.record("operation", "login_succeeded", {}) is False
    tiny = InvocationLogs(lambda: pytest.fail("No record fits"), role="api", settings=replace(cfg, max_bytes=1))
    with tiny.invocation(context()) as rejected:
        record_event("login_succeeded")
    assert rejected.status()["accepted"] == 0 and rejected.status()["dropped"] == 1


@pytest.mark.parametrize("stage", ["factory", "write", "close", "warning", "start"])
def test_logging_failures_preserve_business_exception_and_never_echo_marker(stage, monkeypatch, capsys):
    def fail(*args):
        raise RuntimeError("PRIVATE-MARKER")
    factory = fail if stage == "factory" else lambda: SimpleNamespace(
        write=fail if stage in ("write", "warning") else lambda raw: None,
        close=fail if stage == "close" else lambda: None)
    log = InvocationLogs(factory, role="api", settings=settings(), warning=fail)
    if stage == "start":
        monkeypatch.setattr(threading.Thread, "start", fail)
    with pytest.raises(ValueError, match="business-result"):
        with log.invocation(context()):
            record_event("calculation_completed")
            raise ValueError("business-result")
    if log.writer and log.writer.ident:
        log.writer.join(1)
    assert "PRIVATE-MARKER" not in capsys.readouterr().out


def test_late_inflight_write_is_unconfirmed_then_confirmed_without_cross_context():
    started, release = threading.Event(), threading.Event()
    records = []
    def write(raw):
        started.set()
        assert release.wait(2)
        records.append(json.loads(raw))
    log = InvocationLogs(lambda: SimpleNamespace(write=write, close=lambda: None), role="api", settings=settings())
    ctx = context()
    try:
        with log.invocation(ctx) as buffer:
            record_event("calculation_completed")
        assert started.is_set()
        assert buffer.status()["unconfirmed"] == 1 and buffer.status()["stored"] == 0
    finally:
        release.set()
        log.writer.join(1)
    assert buffer.status()["stored"] == 1 and buffer.status()["unconfirmed"] == 0
    assert records[0]["fields"]["request_id"] == ctx.aws_request_id


def test_all_rejected_records_are_observable_without_creating_sdk_client():
    warnings = []
    log = InvocationLogs(lambda: pytest.fail("Rejected records need no DB client"), role="api",
                         settings=replace(settings(), max_bytes=1), warning=warnings.append)
    with log.invocation(context()):
        record_event("login_succeeded")
    assert len(warnings) == 1
    assert warnings[0]["unreported_dropped"] == 1
    assert log.unreported_dropped == 0


@pytest.mark.parametrize("reason", ["no_budget", "busy"])
def test_skipped_invocation_counts_are_reported_by_next_available_writer(reason):
    warnings = []
    log = InvocationLogs(lambda: SimpleNamespace(write=lambda raw: None, close=lambda: None),
                         role="api", settings=settings(), warning=warnings.append)
    if reason == "busy":
        log.slot.acquire()
    try:
        with log.invocation(context(remaining=5) if reason == "no_budget" else context()):
            record_event("login_succeeded")
    finally:
        if reason == "busy":
            log.slot.release()
    assert not warnings and log.unreported_dropped == 1
    # An empty later invocation can report counters without needing a DB write.
    with log.invocation(context()):
        pass
    assert warnings[0]["unreported_dropped"] == 1
    assert log.unreported_dropped == 0


def test_failed_warning_retains_counts_for_next_invocation():
    def failed(status):
        raise RuntimeError("PRIVATE-MARKER")
    log = InvocationLogs(lambda: pytest.fail("Rejected records need no DB"), role="api",
                         settings=replace(settings(), max_bytes=1), warning=failed)
    with log.invocation(context()):
        record_event("login_succeeded")
    assert log.unreported_dropped == 1
    warnings = []
    log.warning = warnings.append
    with log.invocation(context()):
        pass
    assert warnings[0]["unreported_dropped"] == 1
    assert log.unreported_dropped == 0

"""Local blocking callbacks test ownership, bounded waits and slot quarantine."""

import threading
import time
from types import SimpleNamespace

import pytest

from mock_journey.aws_lease import AwsLeaseGuardFactory
from mock_journey.jobs import JobLeaseLost
from tests.test_aws_runtime import context


def factory():
    return AwsLeaseGuardFactory(lease_seconds=1, interval_seconds=0.03, renewal_timeout_seconds=0.07)


def test_periodic_renewal_and_final_renew_stop_before_return():
    guard = factory()
    calls = []
    enough = threading.Event()
    def heartbeat():
        calls.append(True)
        if len(calls) >= 3:
            enough.set()
    with guard(heartbeat) as renew:
        assert enough.wait(0.6)
        renew()
    count = len(calls)
    time.sleep(0.06)
    assert len(calls) == count
    guard.final_renew(heartbeat)
    assert len(calls) == count + 1
    assert guard.slot.acquire(blocking=False)
    guard.slot.release()


def test_periodic_exception_is_sticky_and_blocks_finalization():
    guard = factory()
    failed = threading.Event()
    calls = []
    def heartbeat():
        calls.append(True)
        if len(calls) > 1:
            failed.set()
            raise RuntimeError("PRIVATE-MARKER")
    with pytest.raises(JobLeaseLost):
        with guard(heartbeat):
            assert failed.wait(0.6)


def test_hung_renewal_bounds_wait_and_quarantines_slot_until_callback_finishes():
    guard = factory()
    started, release = threading.Event(), threading.Event()
    def heartbeat():
        started.set()
        assert release.wait(2)
    beginning = time.monotonic()
    try:
        with pytest.raises(JobLeaseLost):
            with guard(heartbeat):
                pytest.fail("Blocked initial renewal must not enter business work")
        assert time.monotonic() - beginning < 0.5
        assert started.is_set()
        with pytest.raises(JobLeaseLost):
            with guard(lambda: pytest.fail("Busy slot must not execute another job")):
                pass
    finally:
        release.set()
    deadline = time.monotonic() + 1
    while guard.slot.locked() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not guard.slot.locked()
    with guard(lambda: None):
        pass


def test_invocation_deadline_refuses_renewal_without_calling_database():
    guard = factory()
    guard.response_reserve_ms = 100
    with guard.invocation(context(remaining=5)):
        with pytest.raises(JobLeaseLost):
            with guard(lambda: pytest.fail("Expired invocation touched DB")):
                pass


def test_thread_start_failure_is_fixed_and_releases_slot(monkeypatch):
    guard = factory()
    def failed(*args):
        raise RuntimeError("PRIVATE-MARKER")
    monkeypatch.setattr(threading.Thread, "start", failed)
    with pytest.raises(JobLeaseLost) as error:
        with guard(lambda: None):
            pass
    assert "PRIVATE-MARKER" not in str(error.value)
    assert not guard.slot.locked()


def test_thread_construction_failure_releases_slot(monkeypatch):
    guard = factory()
    def failed(*args, **kwargs):
        raise RuntimeError("PRIVATE-MARKER")
    monkeypatch.setattr(threading, "Thread", failed)
    with pytest.raises(JobLeaseLost) as error:
        with guard(lambda: None):
            pass
    assert "PRIVATE-MARKER" not in str(error.value)
    assert not guard.slot.locked()


def test_callback_finishing_after_invocation_deadline_cannot_authorize_work(monkeypatch):
    from mock_journey import aws_lease
    now = [10.0]
    monkeypatch.setattr(aws_lease, "time", SimpleNamespace(monotonic=lambda: now[0]))
    guard = factory()
    token = guard.deadline.set(10.05)
    def heartbeat():
        # Within the configured SDK wait budget, but beyond this invocation's
        # remaining allowance. Successful DB renewal still cannot authorize work.
        now[0] = 10.06
    try:
        with pytest.raises(JobLeaseLost):
            with guard(heartbeat):
                pytest.fail("Expired invocation entered business work")
    finally:
        guard.deadline.reset(token)

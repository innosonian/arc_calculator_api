"""Local lease ownership thread lifecycle; no database/network evidence."""

import socket
import threading
import time
from types import SimpleNamespace

import pytest

from local_server.lease import LocalLeaseGuardFactory, LocalLeaseGuardShutdown
from mock_journey.jobs import JobLeaseLost
from mock_journey.worker import JourneyWorker


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Lease unit test attempted a network connection.")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)


def factory(**overrides):
    values = dict(lease_seconds=3, interval_seconds=0.02, renewal_timeout_seconds=0.3)
    return LocalLeaseGuardFactory(**{**values, **overrides})


@pytest.mark.parametrize("options", [
    {"lease_seconds": True}, {"lease_seconds": 0}, {"lease_seconds": 1.0},
    {"interval_seconds": None}, {"interval_seconds": True}, {"interval_seconds": 0},
    {"interval_seconds": float("nan")}, {"interval_seconds": float("inf")},
    {"interval_seconds": 1.1}, {"renewal_timeout_seconds": -1},
    {"renewal_timeout_seconds": float("inf")}, {"renewal_timeout_seconds": True},
    {"renewal_timeout_seconds": 2.98},
])
def test_invalid_or_unbounded_timing_rejected(options):
    with pytest.raises(ValueError, match="Invalid local lease renewal configuration"):
        factory(**options)


def test_factory_does_not_start_threads_or_invoke_callback():
    calls = []
    before = set(threading.enumerate())
    owner = factory()
    guard = owner(lambda: calls.append("renew"))
    assert guard._thread is None
    assert calls == []
    assert set(threading.enumerate()) == before
    with pytest.raises(ValueError):
        owner(None)


def test_initial_periodic_and_explicit_renewals_stop_and_never_overlap():
    lock = threading.Lock()
    periodic = threading.Event()
    calls = []
    def renew():
        assert lock.acquire(blocking=False), "Renewal callbacks overlapped."
        try:
            calls.append(threading.current_thread().name)
            if len(calls) >= 3:
                periodic.set()
        finally:
            lock.release()
    guard = factory()(renew)
    with guard as heartbeat:
        assert calls == [threading.current_thread().name]
        heartbeat()
        assert periodic.wait(1)
    assert not guard._thread.is_alive()
    count = len(calls)
    assert guard._stop.wait(0.1)
    assert len(calls) == count
    with pytest.raises(JobLeaseLost):
        heartbeat()
    with pytest.raises(ValueError):
        guard.__enter__()


@pytest.mark.parametrize("failure_kind", ["lease_lost", "transient", "domain"])
def test_periodic_failure_is_sticky_and_no_secrets_are_logged(failure_kind, capsys, caplog):
    failed = threading.Event()
    count = [0]
    def renew():
        count[0] += 1
        if count[0] > 1:
            failed.set()
            if failure_kind == "lease_lost":
                raise JobLeaseLost()
            if failure_kind == "domain":
                from mock_journey.errors import JourneyError
                raise JourneyError("STORED_INPUT_INVALID")
            raise RuntimeError("DO-NOT-EXPOSE-SECRET")
    guard = factory()(renew)
    with pytest.raises(JobLeaseLost):
        with guard as heartbeat:
            assert failed.wait(1)
            assert guard._failed.wait(1)
            with pytest.raises(JobLeaseLost):
                heartbeat()
    assert count[0] == 2
    assert not guard._thread.is_alive()
    captured = capsys.readouterr()
    assert captured.out == captured.err == caplog.text == ""


def test_initial_renew_failure_never_starts_background_thread():
    def fail():
        raise RuntimeError("secret")
    guard = factory()(fail)
    with pytest.raises(JobLeaseLost):
        guard.__enter__()
    assert guard._thread is None


def test_successful_but_late_callback_loses_authority():
    def slow():
        time.sleep(0.03)
    guard = factory(renewal_timeout_seconds=0.01)(slow)
    with pytest.raises(JobLeaseLost):
        guard.__enter__()
    assert guard._thread is None


def test_body_error_propagates_only_after_thread_stops():
    guard = factory()(lambda: None)
    with pytest.raises(ValueError, match="calculation error"):
        with guard:
            raise ValueError("calculation error")
    assert not guard._thread.is_alive()


def test_keyboard_interrupt_cleans_up_owned_renewal_thread():
    guard = factory()(lambda: None)
    with pytest.raises(KeyboardInterrupt):
        with guard:
            raise KeyboardInterrupt()
    assert not guard._thread.is_alive()


@pytest.mark.parametrize("after_start", [False, True])
def test_thread_start_failure_cleans_up_even_if_thread_began(monkeypatch, after_start):
    original = threading.Thread.start
    def fail(thread):
        if after_start:
            original(thread)
        raise RuntimeError("startup failed")
    guard = factory()(lambda: None)
    monkeypatch.setattr(threading.Thread, "start", fail)
    with pytest.raises(RuntimeError, match="startup failed"):
        guard.__enter__()
    assert not guard._thread.is_alive()


def test_latent_lease_failure_takes_priority_over_permanent_domain_error():
    failed = threading.Event()
    calls = [0]
    def renew():
        calls[0] += 1
        if calls[0] > 1:
            failed.set()
            raise JobLeaseLost()
    guard = factory()(renew)
    with pytest.raises(JobLeaseLost):
        with guard:
            assert failed.wait(1)
            assert guard._failed.wait(1)
            raise ValueError("Do not mark this stale job permanently failed.")
    assert not guard._thread.is_alive()


def test_stuck_callback_causes_bounded_nonzero_process_exit_not_thread_leak():
    started, release = threading.Event(), threading.Event()
    count = [0]
    def renew():
        count[0] += 1
        if count[0] > 1:
            started.set()
            release.wait(2)
    guard = factory(renewal_timeout_seconds=0.04)(renew)
    try:
        began = time.monotonic()
        with pytest.raises(LocalLeaseGuardShutdown) as failure:
            with guard:
                assert started.wait(1)
        assert failure.value.code == 1
        assert time.monotonic() - began < 0.5
        assert guard._thread.daemon
    finally:
        release.set()
        guard._thread.join(timeout=1)
        assert not guard._thread.is_alive()


def test_normal_stop_during_successful_periodic_callback_is_not_lease_loss():
    started, release = threading.Event(), threading.Event()
    calls = [0]
    def renew():
        calls[0] += 1
        if calls[0] > 1:
            started.set()
            release.wait(0.03)
    guard = factory()(renew)
    with guard:
        assert started.wait(1)
        release.set()
    assert not guard._thread.is_alive()
    assert not guard._failed.is_set()


@pytest.mark.parametrize("action,expected", [("busy", False), ("done", True)])
def test_unclaimed_or_finished_job_never_creates_guard(action, expected):
    class Jobs:
        def claim(self, *args):
            return action, {}
    def forbidden(*args):
        pytest.fail("A job without a lease started a guard.")
    worker = JourneyWorker(Jobs(), None, None, lease_seconds=60, retry_seconds=5,
                           lease_guard_factory=forbidden)
    assert worker.process("job") is expected


def test_generic_worker_default_keeps_guard_disabled_and_rejects_bad_factory():
    assert JourneyWorker(None, None, None, lease_seconds=60, retry_seconds=5).lease_guard_factory is None
    with pytest.raises(ValueError):
        JourneyWorker(None, None, None, lease_seconds=60, retry_seconds=5, lease_guard_factory=1)


def test_worker_domain_failure_is_marked_only_after_renewal_thread_stops():
    from mock_journey.errors import JourneyError
    guards, marked = [], []
    def guard_factory(heartbeat):
        guard = factory()(heartbeat)
        guards.append(guard)
        return guard
    def domain(*args):
        raise JourneyError("CALCULATION_FAILED")
    def mark_failed(*args):
        assert not guards[0]._thread.is_alive()
        marked.append(args)
    jobs = SimpleNamespace(claim=lambda *args: ("execute", {"fence": 1, "adapter_version": "a", "projection_version": "p"}),
                           renew_lease=lambda *args: None, mark_failed=mark_failed)
    worker = JourneyWorker(jobs, None, SimpleNamespace(resolve=domain), lease_seconds=60,
                           retry_seconds=5, lease_guard_factory=guard_factory)
    assert worker.process("job") is True
    assert marked[0][-1] == "CALCULATION_FAILED"


@pytest.mark.parametrize("returned", [None, 1, "PRIVATE-GUARD-MARKER"])
def test_invalid_factory_result_never_calculates_or_finalizes(returned, capsys):
    jobs = SimpleNamespace(claim=lambda *args: ("execute", {"fence": 1}))
    worker = JourneyWorker(jobs, None, None, lease_seconds=60, retry_seconds=5,
                           lease_guard_factory=lambda heartbeat: returned)
    assert worker.process("job") is False
    assert "PRIVATE-GUARD-MARKER" not in capsys.readouterr().out


def test_local_assembly_passes_explicit_factory_without_starting_it():
    from local_server_tests.test_execution import components
    from local_server.execution import build_local_execution
    api, settings, arguments = components()
    guard_factory = factory(lease_seconds=60)
    execution = build_local_execution(api, settings, **arguments, lease_guard_factory=guard_factory)
    assert execution.worker.lease_guard_factory is guard_factory

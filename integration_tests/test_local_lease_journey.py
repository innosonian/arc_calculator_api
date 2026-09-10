"""Actual DDB + files + bundled calculation with owned lease renewal.

The caller runs this only through the isolated local database test launcher.
Clock/renewal fault hooks are explicit; no real network outage is claimed.
"""

import threading
import time

import pytest

from integration_tests.test_local_filesystem_journey import files_journey
from integration_tests.test_mock_journey import api, create, login, stored_attempt, submit
from local_server.lease import LocalLeaseGuardFactory
from mock_journey.contracts import CalculatorRegistry
from mock_journey.worker import JourneyWorker
from tests._synth import comp_session


def configured(world, *, enabled=True):
    guards = []
    factory = LocalLeaseGuardFactory(lease_seconds=2, interval_seconds=0.15, renewal_timeout_seconds=0.5)
    def capture(heartbeat):
        guard = factory(heartbeat)
        guards.append(guard)
        return guard
    worker = JourneyWorker(world.jobs, world.storage, CalculatorRegistry([world.adapter]),
                           lease_seconds=2, retry_seconds=1, clock=world.state.clock,
                           lease_guard_factory=capture if enabled else None)
    return worker, guards


def accepted(world):
    token = login(world)
    attempt = create(world, token, program="mock-compression-only")
    assert submit(world, token, attempt, data=comp_session(60))["statusCode"] == 202
    return token, attempt, stored_attempt(world, token, attempt)["job_id"]


@pytest.mark.parametrize("enabled", [False, True])
def test_real_calculation_longer_than_lease_needs_periodic_renewal(files_journey, monkeypatch, enabled):
    world = files_journey
    token, attempt, job_id = accepted(world)
    original = world.adapter.calculate
    started = time.monotonic()
    base = world.now[0]
    world.state.clock = lambda: base + time.monotonic() - started
    worker, guards = configured(world, enabled=enabled)
    calls, contenders = [], []
    def long_calculation(loaded, binding, heartbeat):
        raw = original(loaded, binding, heartbeat)
        calls.append(raw)
        time.sleep(3)  # actual elapsed time exceeds the two-second DDB lease
        if enabled:
            contenders.append(world.jobs.claim(job_id, "competing-worker", 2)[0])
        return raw
    monkeypatch.setattr(world.adapter, "calculate", long_calculation)
    assert worker.process(job_id) is enabled
    assert time.monotonic() - started >= 3
    current = world.jobs.get_job(job_id)
    assert len(calls) == 1
    status = stored_attempt(world, token, attempt)
    if enabled:
        assert contenders == ["busy"]
        assert current["state"] == "done"
        assert status["evaluation"]["program_completed"] is True
        assert status["progress_application"]["reason"] == "APPLIED"
        assert all(not guard._thread.is_alive() for guard in guards)
    else:
        assert current["state"] == "running" and current["candidate_ref"] is None
        assert status["state"] == "processing"
        assert status["evaluation"] is None


def test_lost_owner_cannot_commit_late_real_candidate_and_new_fence_recovers(files_journey, monkeypatch):
    world = files_journey
    token, attempt, job_id = accepted(world)
    worker, guards = configured(world)
    original = world.adapter.calculate
    selected, old_bindings = [], []
    def lose_owner(loaded, binding, heartbeat):
        raw = original(loaded, binding, heartbeat)
        old_bindings.append(binding)
        world.now[0] += 100
        action, job = world.jobs.claim(job_id, "replacement-owner", 2)
        assert action == "recover" and job["fence"] == 2
        selected.append(job)
        assert guards[0]._failed.wait(3)
        return raw
    monkeypatch.setattr(world.adapter, "calculate", lose_owner)
    assert worker.process(job_id) is False
    assert world.jobs.get_job(job_id)["owner"] == "replacement-owner"
    assert world.jobs.get_job(job_id)["candidate_ref"] is None
    assert stored_attempt(world, token, attempt)["evaluation"] is None
    assert not guards[0]._thread.is_alive()
    world.now[0] += 3
    monkeypatch.setattr(world.adapter, "calculate", original)
    assert worker.process(job_id) is True
    current = world.jobs.get_job(job_id)
    assert current["fence"] == 3 and current["call_id"] != old_bindings[0]["call_id"]
    assert current["planned_candidate_ref"] != selected[0]["planned_candidate_ref"]
    assert stored_attempt(world, token, attempt)["evaluation"]["program_completed"] is True


def test_latent_renewal_failure_after_final_file_blocks_db_finalization(files_journey, monkeypatch, capsys):
    world = files_journey
    token, attempt, job_id = accepted(world)
    worker, guards = configured(world)
    renewal_failed = threading.Event()
    original_renew, original_save, original_calculate = world.jobs.renew_lease, world.storage.save_final, world.adapter.calculate
    finals, calls = [], []
    def renewal(*args):
        if renewal_failed.is_set():
            raise RuntimeError("PRIVATE-RENEWAL-CREDENTIAL")
        return original_renew(*args)
    def final_file(*args):
        ref = original_save(*args)
        finals.append(ref)
        renewal_failed.set()
        assert guards[0]._failed.wait(3)
        return ref
    def calculation(*args):
        calls.append(1)
        return original_calculate(*args)
    monkeypatch.setattr(world.jobs, "renew_lease", renewal)
    monkeypatch.setattr(world.storage, "save_final", final_file)
    monkeypatch.setattr(world.adapter, "calculate", calculation)
    assert worker.process(job_id) is False
    current = world.jobs.get_job(job_id)
    assert current["call_phase"] == "candidate_saved" and current["final_ref"] is None
    assert stored_attempt(world, token, attempt)["evaluation"] is None
    assert len(finals) == len(calls) == 1
    assert not guards[0]._thread.is_alive()
    renewal_failed.clear()
    world.now[0] += 3
    monkeypatch.setattr(world.storage, "save_final", original_save)
    assert worker.process(job_id) is True
    assert len(calls) == 1  # verified saved candidate recovers without recalculation
    assert stored_attempt(world, token, attempt)["evaluation"]["program_completed"] is True
    captured = capsys.readouterr()
    assert "PRIVATE-RENEWAL-CREDENTIAL" not in captured.out + captured.err


def test_logout_epoch_reset_remains_authoritative_with_guard(files_journey, monkeypatch):
    world = files_journey
    token, attempt, job_id = accepted(world)
    other = login(world)
    original = world.adapter.calculate
    def calculate(loaded, binding, heartbeat):
        raw = original(loaded, binding, heartbeat)
        assert api(world, "DELETE", "session", token=other)["statusCode"] == 204
        return raw
    monkeypatch.setattr(world.adapter, "calculate", calculate)
    worker, guards = configured(world)
    assert worker.process(job_id) is True
    status = stored_attempt(world, token, attempt)
    assert status["evaluation"]["program_completed"] is True
    assert status["progress_application"]["reason"] == "PROGRESS_RESET"
    assert world.state.get_progress(world.auth.authenticate(token))["slots"]["mock-compression-only:adult"]["completed"] is False
    assert not guards[0]._thread.is_alive()

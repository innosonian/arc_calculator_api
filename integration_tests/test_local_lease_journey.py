"""Actual DDB + files + bundled calculation with owned lease renewal.

The caller runs this only through the isolated local database test launcher.
Clock/renewal fault hooks are explicit; no real network outage is claimed.
Course attempts through the public /api/v2 harness; the Worker comes from the
public build_worker factory with the local lease guard.
"""

import threading
import time

import pytest

from integration_tests.worker_journey_support import (  # noqa: F401 (store, files_journey fixtures)
    accepted, attempt_row, course_rows, files_journey, item_view, job_row, store,
)
from local_server.lease import LocalLeaseGuardFactory
from tests._synth import comp_session
from tests.journey_support import dummy_course


COURSE = dummy_course("mock-compression-only", "adult")


def configured(world, *, enabled=True):
    guards = []
    factory = LocalLeaseGuardFactory(lease_seconds=2, interval_seconds=0.15, renewal_timeout_seconds=0.5)

    def capture(heartbeat):
        guard = factory(heartbeat)
        guards.append(guard)
        return guard
    return world.lease_worker(capture if enabled else None), guards


def accepted_attempt(world):
    token, attempt, job_id = accepted(world, COURSE, data=comp_session(60))
    return token, attempt["attemptId"], job_id


@pytest.mark.parametrize("enabled", [False, True])
def test_real_calculation_longer_than_lease_needs_periodic_renewal(files_journey, monkeypatch, enabled):
    world = files_journey
    token, attempt_id, job_id = accepted_attempt(world)
    original = world.calculator.calculate
    started = time.monotonic()
    world.realtime_from(started)
    worker, guards = configured(world, enabled=enabled)
    calls, contenders = [], []

    def long_calculation(loaded, binding, heartbeat):
        raw = original(loaded, binding, heartbeat)
        calls.append(raw)
        time.sleep(3)  # actual elapsed time exceeds the two-second DDB lease
        if enabled:
            contenders.append(worker.jobs.claim(job_id, "competing-worker", 2)[0])
        return raw
    monkeypatch.setattr(world.calculator, "calculate", long_calculation)
    assert worker.process(job_id) is enabled
    assert time.monotonic() - started >= 3
    current = job_row(world, job_id)
    assert len(calls) == 1
    status = attempt_row(world, attempt_id)
    if enabled:
        assert contenders == ["busy"]
        assert current["state"] == "done"
        assert status["evaluation"]["program_completed"] is True
        assert status["progress_application"]["reason"] == "APPLIED"
        assert all(not guard._thread.is_alive() for guard in guards)
    else:
        assert current["state"] == "running" and current["candidate_ref"] is None
        assert status["state"] == "processing"
        # Course attempt rows carry no evaluation key until a result is committed.
        assert status.get("evaluation") is None and status.get("result_ref") is None


def test_lost_owner_cannot_commit_late_real_candidate_and_new_fence_recovers(files_journey, monkeypatch):
    world = files_journey
    token, attempt_id, job_id = accepted_attempt(world)
    worker, guards = configured(world)
    original = world.calculator.calculate
    selected, old_bindings = [], []

    def lose_owner(loaded, binding, heartbeat):
        raw = original(loaded, binding, heartbeat)
        old_bindings.append(binding)
        world.advance(100)
        action, job = worker.jobs.claim(job_id, "replacement-owner", 2)
        assert action == "recover" and job["fence"] == 2
        selected.append(job)
        assert guards[0]._failed.wait(3)
        return raw
    monkeypatch.setattr(world.calculator, "calculate", lose_owner)
    assert worker.process(job_id) is False
    assert job_row(world, job_id)["owner"] == "replacement-owner"
    assert job_row(world, job_id)["candidate_ref"] is None
    unfinished = attempt_row(world, attempt_id)
    assert unfinished.get("evaluation") is None and unfinished.get("result_ref") is None
    assert not guards[0]._thread.is_alive()
    world.advance(3)
    monkeypatch.setattr(world.calculator, "calculate", original)
    assert worker.process(job_id) is True
    current = job_row(world, job_id)
    assert current["fence"] == 3 and current["call_id"] != old_bindings[0]["call_id"]
    assert current["planned_candidate_ref"] != selected[0]["planned_candidate_ref"]
    assert current["calculation_restarts"] == 1
    assert attempt_row(world, attempt_id)["evaluation"]["program_completed"] is True


def test_latent_renewal_failure_after_final_file_blocks_db_finalization(files_journey, monkeypatch, capsys):
    world = files_journey
    token, attempt_id, job_id = accepted_attempt(world)
    worker, guards = configured(world)
    renewal_failed = threading.Event()
    original_renew, original_save = worker.jobs.renew_lease, worker.storage.save_final
    original_calculate = world.calculator.calculate
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
    monkeypatch.setattr(worker.jobs, "renew_lease", renewal)
    monkeypatch.setattr(worker.storage, "save_final", final_file)
    monkeypatch.setattr(world.calculator, "calculate", calculation)
    assert worker.process(job_id) is False
    current = job_row(world, job_id)
    assert current["call_phase"] == "candidate_saved" and current["final_ref"] is None
    stored = attempt_row(world, attempt_id)
    assert stored.get("evaluation") is None and stored.get("result_ref") is None
    assert len(finals) == len(calls) == 1
    assert not guards[0]._thread.is_alive()
    renewal_failed.clear()
    world.advance(3)
    monkeypatch.setattr(worker.storage, "save_final", original_save)
    assert worker.process(job_id) is True
    assert len(calls) == 1  # verified saved candidate recovers without recalculation
    assert attempt_row(world, attempt_id)["evaluation"]["program_completed"] is True
    captured = capsys.readouterr()
    assert "PRIVATE-RENEWAL-CREDENTIAL" not in captured.out + captured.err


def test_logout_epoch_reset_remains_authoritative_with_guard(files_journey, monkeypatch):
    world = files_journey
    token, attempt_id, job_id = accepted_attempt(world)
    other = world.login().token
    original_rows = course_rows(world, attempt_id)
    original = world.calculator.calculate

    def calculate(loaded, binding, heartbeat):
        raw = original(loaded, binding, heartbeat)
        world.logout(other)
        return raw
    monkeypatch.setattr(world.calculator, "calculate", calculate)
    worker, guards = configured(world)
    assert worker.process(job_id) is True
    status = attempt_row(world, attempt_id)
    assert status["evaluation"]["program_completed"] is True
    assert status["progress_application"]["reason"] == "PROGRESS_RESET"
    assert course_rows(world, attempt_id) == original_rows
    world.refresh(token)
    assert item_view(world, token, COURSE, COURSE.practice_link_id)["isCompleted"] is False
    assert not guards[0]._thread.is_alive()

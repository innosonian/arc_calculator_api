"""Actual default CLI state acceptance using recorded measurement uploads.

Only the test-owned spawned worker is paused. HTTP acceptance, durable queue
recovery, calculation and progress writes all use the unmodified product path.
These tests do not claim physical app/manikin or external ARC acceptance.
"""

import os
import signal
import subprocess
import time
import uuid

import pytest

from local_server_tests.test_journey_runtime import JourneyServer
from local_server_tests.test_live_server import error, require


@pytest.fixture
def state_cli(tmp_path):
    server = JourneyServer(tmp_path / "state-journey")
    try:
        server.start()
        yield server
    finally:
        server.stop()
        server.assert_private_logs()


def programs(server, token):
    reply = server.request("GET", "/mock/v1/programs", token=token)
    require(reply.status == 200, "A surviving session must read shared progress.")
    return reply.json()


def slot(value, *, program="mock-compression-only", target="adult"):
    matches = [item for item in value["programs"] if item["id"] == program]
    require(len(matches) == 1, "Expected one program in the shared catalog.")
    return (matches[0]["progress_by_target"][target],
            matches[0]["active_attempts_by_target"][target])


def assessment(server, token, attempt):
    reply = server.request("GET", f'/mock/v1/attempts/{attempt["attempt_id"]}', token=token)
    require(reply.status == 200, "The bound session must read its attempt.")
    return reply.json()


def completed(value):
    require(value["state"] == "evaluated", "The product worker must commit its evaluation.")
    require(value["evaluation"]["goal"]["status"] == "evaluated"
            and value["evaluation"]["goal"]["met"] is True
            and value["evaluation"]["score"]["decision"] == "pass"
            and value["evaluation"]["program_completed"] is True,
            "Recorded Only measurements must meet the fixed target and real tester Pass.")


def pause_owned_worker(server):
    worker = server.owned_worker_pid()
    parent = server.process.pid
    os.kill(worker, signal.SIGSTOP)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        checked = subprocess.run(["ps", "-p", str(worker), "-o", "ppid=,stat="],
                                 capture_output=True, text=True, timeout=3)
        parts = checked.stdout.strip().split()
        require(checked.returncode == 0 and len(parts) == 2 and parts[0] == str(parent),
                "The paused process must remain the verified test-owned child.")
        if "T" in parts[1]:
            return
        time.sleep(0.02)
    raise AssertionError("The owned worker did not enter the requested stopped state.")


def restart_with_queued_input(server, token, attempt):
    # A stopped worker cannot read the stop pipe or handle SIGTERM. Product
    # shutdown must use its bounded owned-child escalation before restart.
    parent = server.process
    server.stop()
    require(parent.returncode == 0,
            "The real CLI must cleanly stop its paused worker without test group-kill fallback.")
    server.start()
    return server.result(token, attempt)


def test_only_completion_is_shared_retraining_denied_and_early_stop_is_not_completion(state_cli):
    server = state_cli
    first, peer = server.login()["session_token"], server.login()["session_token"]
    attempt = server.attempt(first)
    require(slot(programs(server, peer)) == ("not_completed", 1),
            "Peer progress must expose the selected active training.")
    require(server.upload(first, attempt).status in (200, 202),
            "The actual recorded upload must be accepted or already committed.")
    result = server.result(first, attempt)
    view = assessment(server, first, attempt)
    completed(view)
    require(view["progress_application"] == {
        "applied": True, "applied_epoch": attempt["progress_epoch"], "reason": "APPLIED",
    }, "The first completed attempt must apply to its current epoch exactly once.")
    shared = programs(server, peer)
    require(slot(shared) == ("completed", 0), "Peer must see completion with no active count remaining.")
    error(server.request("POST", "/mock/v1/attempts", token=peer, body={
        "client_request_id": str(uuid.uuid4()), "catalog_version": "mock-catalog-v1",
        "program_id": "mock-compression-only", "target": "adult",
    }), 409, "PROGRAM_ALREADY_COMPLETED")
    require(server.upload(first, attempt, alias=False).body == result.body,
            "Retrying the completed upload must return the same stored calculation.")
    require(assessment(server, first, attempt) == view and programs(server, peer) == shared,
            "Denied retraining and stored-result retry must not apply progress twice.")

    next_attempt = server.attempt(peer, program="mock-ventilation-only")
    cancel_path = f'/mock/v1/attempts/{next_attempt["attempt_id"]}/cancel'
    require(server.request("POST", cancel_path, token=peer, body={"reason": "user_stopped"}).status == 204,
            "Early stop before submission must cancel the selected training.")
    cancelled = assessment(server, peer, next_attempt)
    require(cancelled["state"] == "cancelled" and cancelled["evaluation"] is None
            and cancelled["progress_application"] is None,
            "An early stop must not invent a calculation or completion assessment.")
    error(server.request("GET", next_attempt["calculation_path"], token=peer), 409, "INVALID_STATE")
    after_cancel = programs(server, first)
    require(slot(after_cancel) == ("completed", 0)
            and slot(after_cancel, program="mock-ventilation-only") == ("not_completed", 0),
            "Cancellation must close its count and preserve the previous completed program.")
    replacement = server.attempt(peer, program="mock-ventilation-only")
    require(replacement["attempt_id"] != next_attempt["attempt_id"],
            "An incomplete cancelled program must allow a fresh selection.")


def test_accepted_queue_automatically_finishes_after_same_installation_restart(state_cli):
    server = state_cli
    token = server.login()["session_token"]
    attempt = server.attempt(token)
    pause_owned_worker(server)
    require(server.upload(token, attempt).status == 202,
            "Upload must durably accept input even while the owned worker is paused.")
    require(assessment(server, token, attempt)["state"] == "queued",
            "No test helper may execute the paused worker's accepted queue.")
    require(server.request("GET", attempt["calculation_path"], token=token).status == 202,
            "Accepted but unexecuted work must remain pending before restart.")
    response = restart_with_queued_input(server, token, attempt)
    view = assessment(server, token, attempt)
    completed(view)
    require(view["progress_application"]["reason"] == "APPLIED"
            and view["progress_application"]["applied"] is True,
            "Restart must automatically complete work in its preserved current epoch.")
    shared = programs(server, token)
    require(slot(shared) == ("completed", 0), "Recovered work must close its active count once.")
    require(server.upload(token, attempt).body == response.body
            and server.request("GET", attempt["calculation_path"], token=token).body == response.body,
            "Recovery and HTTP retry must converge to the same committed result.")
    require(programs(server, token) == shared and assessment(server, token, attempt) == view,
            "Reading or retrying recovered work must not repeat completion writes.")


def test_peer_logout_before_queue_recovery_preserves_result_without_restoring_old_progress(state_cli):
    server = state_cli
    survivor, peer = server.login()["session_token"], server.login()["session_token"]
    attempt = server.attempt(survivor)
    pause_owned_worker(server)
    require(server.upload(survivor, attempt).status == 202,
            "Original-epoch input must be accepted before the peer logs out.")
    require(assessment(server, survivor, attempt)["state"] == "queued",
            "The paused product worker must not evaluate before logout.")
    require(server.request("DELETE", "/mock/v1/session", token=peer).status == 204,
            "One device logout must reset the shared tester epoch.")
    reset = programs(server, survivor)
    require(reset["progress_epoch"] != attempt["progress_epoch"] and slot(reset) == ("not_completed", 0),
            "Peer logout must reset shared progress while preserving the other session.")
    response = restart_with_queued_input(server, survivor, attempt)
    view = assessment(server, survivor, attempt)
    completed(view)
    require(view["progress_application"] == {
        "applied": False, "applied_epoch": None, "reason": "PROGRESS_RESET",
    }, "Late success from the previous epoch must remain readable without restoring its progress.")
    require(programs(server, survivor) == reset,
            "Automatic queue recovery must not change the reset epoch, counts or revision.")
    error(server.request("GET", "/mock/v1/session", token=peer), 403, "SESSION_REVOKED")
    require(server.upload(survivor, attempt).body == response.body
            and assessment(server, survivor, attempt) == view,
            "Retry after logout/reset must preserve the original committed result and assessment.")
    require(programs(server, survivor) == reset, "A retry must not reapply old-epoch completion.")
    fresh = server.attempt(survivor)
    require(fresh["progress_epoch"] == reset["progress_epoch"]
            and slot(programs(server, survivor)) == ("not_completed", 1),
            "The new epoch must permit a new selection despite the old successful result.")

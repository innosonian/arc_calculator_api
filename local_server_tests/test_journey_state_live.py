"""Actual default CLI /api/v2 state acceptance using recorded measurement uploads.

Only the test-owned spawned worker is paused. HTTP acceptance, durable queue
recovery, calculation and progress writes all use the unmodified product path.
These tests do not claim physical app/manikin or external ARC acceptance.
"""

import os
import signal
import subprocess
import time

import pytest

from local_server_tests.test_journey_runtime import JourneyServer, owned_db_client
from local_server_tests.test_live_server import dummy_course, error, require

# Real loopback sockets: opted out of the directory network guard (conftest.py).
pytestmark = pytest.mark.loopback


@pytest.fixture
def state_cli(tmp_path):
    server = JourneyServer(tmp_path / "state-journey")
    try:
        server.start()
        yield server
    finally:
        server.stop()
        server.assert_private_logs()


def _plain(item):
    from boto3.dynamodb.types import TypeDeserializer
    deserializer = TypeDeserializer()
    return {key: deserializer.deserialize(value) for key, value in item.items()}


def stored_row(server, pk, sk):
    """Read-only, consistent read of one row from this server's owned DB child.

    The same loopback DB child and table the CLI uses; no write is ever issued.
    Returns None when the row is absent.
    """
    db, client = owned_db_client(server)
    try:
        item = client.get_item(TableName=db.TABLE_NAME, ConsistentRead=True,
                               Key={"PK": {"S": pk}, "SK": {"S": sk}}).get("Item")
    finally:
        client.close()
    return None if item is None else _plain(item)


def stored_attempt(server, attempt):
    """The attempt's stored ATTEMPT row (read-only).

    The /api/v2 attempt view has no epoch or evaluation fields, so the stored row
    is the only place that shows the attempt's own progress epoch.
    """
    item = stored_row(server, f'ATTEMPT#{attempt["attemptId"]}', "META")
    require(item is not None, "The started attempt must have its stored ATTEMPT row.")
    return item


def stored_user(server, principal):
    item = stored_row(server, f"USER#{principal}", "STATE")
    require(item is not None and item.get("principal") == principal, "The tester must have its stored USER row.")
    return item


def stored_course_epoch_rows(server, scope_key, epoch):
    """All COURSE#scope rows of one progress epoch (HEAD, FINAL, ITEM, ...), read-only, sorted by SK."""
    db, client = owned_db_client(server)
    rows, start = [], None
    try:
        while True:
            request = {"TableName": db.TABLE_NAME, "ConsistentRead": True,
                       "KeyConditionExpression": "PK = :pk AND begins_with(SK, :sk)",
                       "ExpressionAttributeValues": {":pk": {"S": f"COURSE#{scope_key}"},
                                                     ":sk": {"S": f"EPOCH#{epoch}#"}}}
            if start:
                request["ExclusiveStartKey"] = start
            page = client.query(**request)
            rows.extend(_plain(item) for item in page.get("Items", []))
            start = page.get("LastEvaluatedKey")
            if not start:
                break
    finally:
        client.close()
    return sorted(rows, key=lambda row: row["SK"])


def completed(result):
    require(result["calculationStatus"] == "succeeded", "The product worker must commit its evaluation.")
    evaluation = result["evaluation"]
    require(evaluation["goal"]["status"] == "evaluated" and evaluation["goal"]["met"] is True
            and evaluation["score"]["decision"] == "pass" and evaluation["program_completed"] is True,
            "Recorded Only measurements must meet the fixed target and real tester Pass.")


def pause_owned_worker(server):
    worker = server.owned_worker_pid()
    parent = server.process.pid
    os.kill(worker, signal.SIGSTOP)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        checked = subprocess.run(["ps", "-p", str(worker), "-o", "ppid=", "-o", "stat="],
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
    course, ventilation = dummy_course(), dummy_course("mock-ventilation-only")
    first, peer = server.token(), server.token()
    error(server.start_attempt_reply(first, course, course.final_link_id), 409, "PREREQUISITES_NOT_COMPLETED")
    attempt = server.start_attempt(first, course)
    # Independent expected value: the epoch the attempt is pinned to at start,
    # read before any upload or Worker write (the old create-response
    # progress_epoch). It must equal the shared USER epoch at that moment.
    pinned = stored_attempt(server, attempt)
    pinned_epoch = pinned["epoch"]
    require(type(pinned_epoch) is str and pinned_epoch
            and pinned["course_binding"]["epoch"] == pinned_epoch
            and stored_user(server, pinned["principal"])["epoch"] == pinned_epoch,
            "The started attempt must be pinned to the shared progress epoch at start.")
    require(server.item(peer, course)["isCompleted"] is False, "An unsubmitted training is not completion.")
    # An active attempt belongs to its bound session, not to shared progress.
    error(server.attempt(peer, attempt), 404, "NOT_FOUND")
    require(server.upload(first, attempt).status in (200, 202),
            "The actual recorded upload must be accepted or already committed.")
    committed = server.result(first, attempt)
    result = committed.data()
    completed(result)
    stored = stored_attempt(server, attempt)
    require(stored["epoch"] == pinned_epoch and stored["course_binding"]["epoch"] == pinned_epoch,
            "Finalize must not rewrite the attempt's own progress epoch.")
    require(result["progressApplication"] == {"applied": True, "applied_epoch": pinned_epoch, "reason": "APPLIED"},
            "The first completed attempt must apply to exactly the epoch pinned at its start.")
    require(stored["progress_application"] == result["progressApplication"],
            "The wire progress application must be the stored one.")
    shared = server.course(peer, course)
    require([(item["isCompleted"], item["isPassed"]) for item in shared["courseItems"]] == [(True, True), (False, None)],
            "Peer must see the completed practice and a not-started final assessment.")
    # D130: the completed training may be practised again by any session; the
    # new attempt does not touch the shared completion (checked below).
    repeat = server.start_attempt(peer, course)
    require(repeat["attemptId"] != attempt["attemptId"] and repeat["role"] == "training",
            "A completed training must accept a new start (D130).")
    require(server.course(peer, course) == shared, "A repeat start must not change the shared progress.")
    # The whole attempt view (not only its state) must survive the retry. Only
    # the envelope's per-response "timestamp" may differ, so stable_body strips
    # exactly that; createdAt is the stored creation time and is compared too.
    view = server.attempt(first, attempt)
    require(view.data()["state"] == "evaluated" and view.data()["attemptId"] == attempt["attemptId"],
            "The committed attempt view must be evaluated.")
    require(server.upload(first, attempt).stable_body() == committed.stable_body(),
            "Retrying the completed upload must return the same stored calculation bytes.")
    require(server.calculation(first, attempt).stable_body() == committed.stable_body()
            and server.course(peer, course) == shared,
            "A repeat start and a stored-result retry must not apply progress twice.")
    require(server.attempt(first, attempt).stable_body() == view.stable_body()
            and stored_attempt(server, attempt) == stored,
            "A stored-result retry must not change the attempt view or its stored row.")
    # The shared completion satisfies the final assessment prerequisite.
    final = server.start_attempt(peer, course, course.final_link_id)
    require(final["role"] == "final_assessment", "The completed practice must unlock the final assessment.")
    require(server.request("POST", f'/api/v2/attempts/{final["attemptId"]}/cancel/', token=peer,
                           body={"reason": "user_cancelled"}).status == 204, "The final assessment must cancel.")

    next_attempt = server.start_attempt(peer, ventilation)
    cancel_path = f'/api/v2/attempts/{next_attempt["attemptId"]}/cancel/'
    require(server.request("POST", cancel_path, token=peer, body={"reason": "user_cancelled"}).status == 204,
            "Early stop before submission must cancel the selected training.")
    cancelled = server.attempt(peer, next_attempt).data()
    require(cancelled["state"] == "cancelled", "An early stop must cancel without inventing a calculation.")
    require(list(cancelled) == ["attemptId", "state", "courseId", "enrollmentId", "courseItemLinkId",
                                "definitionHash", "createdAt", "role", "condition"],
            "The attempt view never carries a calculation, evaluation or progress application.")
    error(server.calculation(peer, next_attempt), 409, "INVALID_STATE")
    stopped = stored_attempt(server, next_attempt)
    require(stopped["state"] == "cancelled" and stopped.get("evaluation") is None
            and stopped.get("progress_application") is None and stopped.get("result_ref") is None
            and stopped.get("job_id") is None,
            "An early stop must not store a calculation, evaluation or progress application.")
    require(server.item(first, course)["isCompleted"] is True and server.item(first, ventilation)["isCompleted"] is False,
            "Cancellation must preserve the previous completed course and not complete its own.")
    replacement = server.start_attempt(peer, ventilation)
    require(replacement["attemptId"] != next_attempt["attemptId"],
            "An incomplete cancelled training must allow a fresh start.")


def test_accepted_queue_automatically_finishes_after_same_installation_restart(state_cli):
    server = state_cli
    course = dummy_course()
    token = server.token()
    attempt = server.start_attempt(token, course)
    # Epoch pinned at start, read before upload/restart/Worker writes.
    pinned = stored_attempt(server, attempt)
    pinned_epoch = pinned["epoch"]
    require(type(pinned_epoch) is str and pinned_epoch
            and stored_user(server, pinned["principal"])["epoch"] == pinned_epoch,
            "The started attempt must be pinned to the shared progress epoch at start.")
    pause_owned_worker(server)
    require(server.upload(token, attempt).status == 202,
            "Upload must durably accept input even while the owned worker is paused.")
    require(server.attempt(token, attempt).data()["state"] == "queued",
            "No test helper may execute the paused worker's accepted queue.")
    pending = server.calculation(token, attempt)
    require(pending.status == 202 and pending.data(202)["calculationStatus"] == "pending",
            "Accepted but unexecuted work must remain pending before restart.")
    recovered = restart_with_queued_input(server, token, attempt)
    result = recovered.data()
    completed(result)
    require(result["progressApplication"] == {"applied": True, "applied_epoch": pinned_epoch, "reason": "APPLIED"},
            "Restart must automatically complete work in the epoch pinned at its start.")
    require(stored_attempt(server, attempt)["epoch"] == pinned_epoch,
            "Restart recovery must not rewrite the attempt's own progress epoch.")
    shared = server.course(token, course)
    require(shared["courseItems"][0]["isCompleted"] is True, "Recovered work must complete its course item once.")
    # As in the first test, the whole recovered attempt view and its stored row
    # must survive the retry; stable_body strips only the envelope timestamp.
    view, stored = server.attempt(token, attempt), stored_attempt(server, attempt)
    require(view.data()["state"] == "evaluated" and view.data()["attemptId"] == attempt["attemptId"],
            "The recovered attempt view must be evaluated.")
    require(stored["progress_application"] == result["progressApplication"],
            "The recovered wire progress application must be the stored one.")
    require(server.upload(token, attempt).stable_body() == recovered.stable_body()
            and server.calculation(token, attempt).stable_body() == recovered.stable_body(),
            "Recovery and HTTP retry must converge to the same committed result bytes.")
    require(server.course(token, course) == shared,
            "Reading or retrying recovered work must not repeat completion writes.")
    require(server.attempt(token, attempt).stable_body() == view.stable_body()
            and stored_attempt(server, attempt) == stored,
            "A retry after recovery must not change the attempt view or its stored row.")


def test_peer_logout_before_queue_recovery_preserves_result_without_restoring_old_progress(state_cli):
    server = state_cli
    course = dummy_course()
    survivor, peer = server.token(), server.token()
    attempt = server.start_attempt(survivor, course)
    pause_owned_worker(server)
    require(server.upload(survivor, attempt).status == 202,
            "Original-epoch input must be accepted before the peer logs out.")
    require(server.attempt(survivor, attempt).data()["state"] == "queued",
            "The paused product worker must not evaluate before logout.")
    require(server.request("DELETE", "/api/v2/session/", token=peer).status == 204,
            "One device logout must reset the shared tester epoch.")
    require(server.request("GET", "/api/v2/session/", token=survivor).data()["learningAvailability"]
            == {"state": "waiting", "reason": "arc_progress_unavailable"},
            "Peer logout must reset shared progress while preserving the other session.")
    # Snapshot the reset epoch before recovery: the USER row (epoch, revision)
    # and every COURSE row of the new epoch for this course scope.
    original = stored_attempt(server, attempt)
    scope = original["course_binding"]["scope_key"]
    reset_user = stored_user(server, original["principal"])
    reset_epoch = reset_user["epoch"]
    require(type(reset_epoch) is str and reset_epoch and reset_epoch != original["epoch"],
            "Peer logout must move the shared tester to a new progress epoch.")
    reset_rows = stored_course_epoch_rows(server, scope, reset_epoch)
    recovered = restart_with_queued_input(server, survivor, attempt)
    result = recovered.data()
    completed(result)
    require(result["progressApplication"] == {"applied": False, "applied_epoch": None, "reason": "PROGRESS_RESET"},
            "Late success from the previous epoch must remain readable without restoring its progress.")
    require(stored_attempt(server, attempt)["epoch"] == original["epoch"],
            "Recovered work must stay bound to its own previous epoch.")
    require(stored_user(server, original["principal"]) == reset_user
            and stored_course_epoch_rows(server, scope, reset_epoch) == reset_rows,
            "Automatic queue recovery must not change the reset epoch, its revision or its course rows.")
    require(server.refresh(survivor)["learningAvailability"] == {"state": "ready", "reason": None},
            "The surviving session must recover the new epoch by refresh.")
    reset = server.course(survivor, course)
    require([item["isCompleted"] for item in reset["courseItems"]] == [False, False],
            "Automatic queue recovery must not restore old-epoch completion.")
    error(server.request("GET", "/api/v2/session/", token=peer), 403, "SESSION_REVOKED")
    read_rows = stored_course_epoch_rows(server, scope, reset_epoch)
    # Old "assessment == view" check: the whole recovered attempt view and its
    # stored row must survive the retry (stable_body strips only the timestamp).
    view, stored = server.attempt(survivor, attempt), stored_attempt(server, attempt)
    require(view.data()["state"] == "evaluated" and view.data()["attemptId"] == attempt["attemptId"]
            and stored["progress_application"] == result["progressApplication"],
            "The recovered old-epoch attempt must be evaluated with its stored PROGRESS_RESET application.")
    require(server.upload(survivor, attempt).stable_body() == recovered.stable_body()
            and server.calculation(survivor, attempt).stable_body() == recovered.stable_body(),
            "Retry after logout/reset must preserve the original committed result bytes and assessment.")
    require(server.course(survivor, course) == reset, "A retry must not reapply old-epoch completion.")
    require(stored_user(server, original["principal"]) == reset_user
            and stored_course_epoch_rows(server, scope, reset_epoch) == read_rows,
            "A retry of old-epoch work must not write the reset epoch's USER or course rows.")
    require(server.attempt(survivor, attempt).stable_body() == view.stable_body()
            and stored_attempt(server, attempt) == stored,
            "A retry of old-epoch work must not change its attempt view or stored row.")
    fresh = server.start_attempt(survivor, course)
    require(fresh["attemptId"] != attempt["attemptId"] and fresh["state"] == "created",
            "The new epoch must permit a new start despite the old successful result.")
    # Old "fresh.progress_epoch == reset.progress_epoch": the new start binds to
    # the reset epoch, never to the old attempt's epoch.
    fresh_row = stored_attempt(server, fresh)
    require(fresh_row["epoch"] == reset_epoch and fresh_row["course_binding"]["epoch"] == reset_epoch,
            "A start after logout/reset must bind to the reset epoch.")

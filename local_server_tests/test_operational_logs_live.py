"""Actual default CLI writes API + spawned-worker logs to its owned local DB."""

from datetime import datetime, timezone
import json
import subprocess
import time
import uuid

import pytest

from local_server_tests.test_live_server import ROOT, PYTHON, dummy_course, require
from local_server_tests.test_journey_runtime import JourneyServer

# Real loopback sockets: opted out of the directory network guard (conftest.py).
pytestmark = pytest.mark.loopback


def read_page(server, *, limit=100, after=None):
    command = [str(PYTHON), str(ROOT / "scripts/read_local_logs.py"), "--data-dir", str(server.data),
               "--db-port", str(server.db_port), "--date", datetime.now(timezone.utc).strftime("%Y-%m-%d"),
               "--limit", str(limit)]
    if after:
        command += ["--after", after]
    output = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=15)
    require(output.returncode == 0, "The readonly local log command failed.")
    require(not output.stderr, "The log reader unexpectedly emitted diagnostics.")
    return json.loads(output.stdout)


def wait_for(server, predicate, explanation, *, seconds=15):
    deadline = time.monotonic() + seconds
    while True:
        rows = read_page(server, limit=500)["records"]
        if predicate(rows):
            return rows
        require(time.monotonic() < deadline, explanation)
        time.sleep(0.1)


def test_real_api_and_worker_logs_survive_restart_without_exposing_secrets(tmp_path):
    server = JourneyServer(tmp_path / "operational-logs")
    try:
        server.start()
        marker = "PRIVATE-OPERATIONAL-LIVE-PASSWORD"
        require(server.request("POST", "/api/v2/sessions/", body={
            "loginId": "test@test.com", "password": marker}).status == 401, "Bad login should still be rejected.")
        require(server.request("POST", "/api/v2/sessions/", body={
            "loginId": marker, "password": marker}).status == 503, "Non-Dummy login waits for the ARC contract.")
        token = server.token()
        request_id = str(uuid.uuid4())
        attempt = server.start_attempt(token, request_id=request_id)
        repeat = server.start_attempt_reply(token, dummy_course(), request_id=request_id)
        require(repeat.status == 200 and repeat.data()["attemptId"] == attempt["attemptId"],
                "Start replay must still use the same attempt.")
        require(server.upload(token, attempt).status in (200, 202), "Real measurement must be accepted.")
        original = server.result(token, attempt).stable_body()
        require(server.upload(token, attempt).stable_body() == original, "Logs must not change result replay bytes.")
        cancelled = server.start_attempt(token, dummy_course("mock-ventilation-only"))
        require(server.request("POST", f'/api/v2/attempts/{cancelled["attemptId"]}/cancel/', token=token,
                               body={"reason": "user_cancelled"}).status == 204, "Cancellation must succeed.")
        expected = {"login_failed", "login_succeeded", "calculation_accepted", "calculation_replayed",
                    "calculation_started", "calculation_completed", "progress_application", "attempt_cancelled",
                    "calc_start", "calc_complete", "parse_complete"}
        rows = wait_for(server, lambda rows: expected <= {r["event"] for r in rows},
                        "Expected API/worker operational logs were not persisted.")
        encoded = json.dumps(rows)
        require(marker not in encoded and token not in encoded and attempt["resumeCredential"] not in encoded,
                "Credentials must not be stored in operational records.")
        require("chart_dataset_url" not in encoded and "rawHexBPfile" not in encoded,
                "Chart capabilities and request bodies are not log fields.")
        require(len([r for r in rows if r["event"] == "login_failed"]) == 2
                and {r["fields"].get("error_code") for r in rows if r["event"] == "login_failed"}
                == {"LOGIN_FAILED", "CONTRACT_PENDING"}, "Each rejected login must be one fixed-code record.")
        require({r["role"] for r in rows} == {"api", "worker"}, "The spawned worker must use its own log writer.")
        worker_rows = [r for r in rows if r["role"] == "worker"]
        require(all(r["fields"].get("attempt_id") == attempt["attemptId"] for r in worker_rows),
                "Worker diagnostics must retain only the matching attempt context.")
        health = server.request("GET", "/healthz").json()["operational_logs"]
        require(health["scope"] == "api_process" and health["stored"] > 0 and health["unconfirmed"] == 0,
                "Local health must expose truthful API log counters.")
        first = read_page(server, limit=2)
        second = read_page(server, limit=2, after=first["next_cursor"])
        require(not ({r["log_id"] for r in first["records"]} & {r["log_id"] for r in second["records"]}),
                "Log pagination must not repeat or change records.")
        original_records = {r["log_id"]: r for r in rows}
        server.stop(); server.start()
        require(server.calculation(token, attempt).stable_body() == original,
                "Enabling logs must preserve the existing result bytes across restart.")
        after = {r["log_id"]: r for r in read_page(server, limit=500)["records"]}
        require(all(after.get(ident) == row for ident, row in original_records.items()),
                "Existing DB logs must survive restart unchanged.")
        require(server.request("DELETE", "/api/v2/session/", token=token).status == 204, "Logout must succeed.")
        require(server.request("DELETE", "/api/v2/session/", token=token).status == 204, "Logout replay must succeed.")
        # One more fixed-code record after both logouts: the single bounded
        # writer persists in order, so once it is stored both logouts are.
        require(server.request("POST", "/api/v2/sessions/", body={
            "loginId": "test@test.com", "password": marker}).status == 401, "Sentinel login must be rejected.")
        rows = wait_for(server, lambda rows: len([r for r in rows if r["event"] == "login_failed"]) == 3,
                        "Operation records after logout did not arrive.")
        require(len([r for r in rows if r["event"] == "progress_reset"]) == 1,
                "Logout receipt replay must not claim a second progress reset.")
        require(marker not in json.dumps(rows), "Credentials must not be stored in operational records.")
        server.assert_private_logs()
    finally:
        server.stop()

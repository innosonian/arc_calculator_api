"""Actual default CLI writes API + spawned-worker logs to its owned local DB."""

from datetime import datetime, timezone
import json
import subprocess
import time
import uuid

from local_server_tests.test_live_server import ROOT, PYTHON, require
from local_server_tests.test_journey_runtime import JourneyServer


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


def test_real_api_and_worker_logs_survive_restart_without_exposing_secrets(tmp_path):
    server = JourneyServer(tmp_path / "operational-logs")
    try:
        server.start()
        marker = "PRIVATE-OPERATIONAL-LIVE-PASSWORD"
        require(server.request("POST", "/mock/v1/sessions", body={
            "login_id": marker, "password": marker}).status == 401, "Bad login should still be rejected.")
        token = server.login()["session_token"]
        request_id = str(uuid.uuid4())
        attempt = server.attempt(token, request_id=request_id)
        repeat = server.request("POST", "/mock/v1/attempts", token=token, body={
            "client_request_id": request_id, "catalog_version": "mock-catalog-v1",
            "program_id": "mock-compression-only", "target": "adult"})
        require(repeat.status == 200, "Create replay must still use the same attempt.")
        require(server.upload(token, attempt).status in (200, 202), "Real measurement must be accepted.")
        original = server.result(token, attempt)
        require(server.upload(token, attempt).body == original.body, "Logs must not change result replay.")
        cancelled = server.attempt(token, program="mock-ventilation-only")
        require(server.request("POST", f'/mock/v1/attempts/{cancelled["attempt_id"]}/cancel', token=token,
                               body={"reason": "user_stopped"}).status == 204, "Cancellation must succeed.")
        expected = {"login_failed", "login_succeeded", "attempt_created", "attempt_create_replayed",
                    "calculation_accepted", "calculation_replayed", "calculation_started", "calculation_completed",
                    "progress_application", "attempt_cancelled", "calc_start", "calc_complete", "parse_complete"}
        deadline = time.monotonic() + 15
        while True:
            page = read_page(server, limit=500)
            rows = page["records"]
            if expected <= {r["event"] for r in rows}: break
            require(time.monotonic() < deadline, "Expected API/worker operational logs were not persisted.")
            time.sleep(0.1)
        encoded = json.dumps(rows)
        require(marker not in encoded and token not in encoded and attempt["resume_credential"] not in encoded,
                "Credentials must not be stored in operational records.")
        require("chart_dataset_url" not in encoded and "rawHexBPfile" not in encoded,
                "Chart capabilities and request bodies are not log fields.")
        require({r["role"] for r in rows} == {"api", "worker"}, "The spawned worker must use its own log writer.")
        worker_rows = [r for r in rows if r["role"] == "worker"]
        require(all(r["fields"].get("attempt_id") == attempt["attempt_id"] for r in worker_rows),
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
        require(server.request("GET", attempt["calculation_path"], token=token).body == original.body,
                "Enabling logs must preserve the existing result across restart.")
        after = {r["log_id"]: r for r in read_page(server, limit=500)["records"]}
        require(all(after.get(ident) == row for ident, row in original_records.items()),
                "Existing DB logs must survive restart unchanged.")
        require(server.request("DELETE", "/mock/v1/session", token=token).status == 204, "Logout must succeed.")
        require(server.request("DELETE", "/mock/v1/session", token=token).status == 204, "Logout replay must succeed.")
        deadline = time.monotonic() + 10
        while True:
            rows = read_page(server, limit=500)["records"]
            if len([r for r in rows if r["event"] == "logout_succeeded"]) == 2: break
            require(time.monotonic() < deadline, "Logout operation records did not arrive.")
        require(len([r for r in rows if r["event"] == "progress_reset"]) == 1,
                "Logout receipt replay must not claim a second progress reset.")
        server.assert_private_logs()
    finally:
        server.stop()

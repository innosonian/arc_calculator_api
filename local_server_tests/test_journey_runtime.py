"""L3 real default CLI smoke: owned DB, worker, private files and HTTP.

No service/storage/calculator is injected into the child. Fixtures are recorded
measurement files, not evidence of an iPad/physical manikin acceptance run.
"""

import base64
import hashlib
import http.client
import json
import os
import signal
import subprocess
import time
from urllib.parse import urlsplit
import uuid

import pytest

from local_server_tests.test_live_server import LiveServer, Reply, ROOT, require
from tests._synth import multipart_event


class JourneyServer(LiveServer):
    def command(self):
        # The legacy acceptance harness explicitly opts into control-only.
        # This class exercises the production default without that switch.
        return [arg for arg in super().command() if arg != "--control-only"]

    def request(self, method, path, body=None, token=None, headers=None):
        fields = dict(headers or {})
        if body is not None and type(body) is not bytes:
            body = json.dumps(body, allow_nan=False).encode()
            fields.setdefault("Content-Type", "application/json")
        if token:
            fields["Authorization"] = "Bearer " + token
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            connection.request(method, path, body=body, headers=fields)
            response = connection.getresponse()
            content = response.read(8_100_001)
            require(len(content) <= 8_100_000, "Test response exceeded its explicit bound.")
            return Reply(response.status, dict(response.getheaders()), content)
        finally:
            connection.close()

    def attempt(self, token, *, program="mock-compression-only", target="adult", request_id=None):
        reply = self.request("POST", "/mock/v1/attempts", token=token, body={
            "client_request_id": request_id or str(uuid.uuid4()),
            "catalog_version": "mock-catalog-v1", "program_id": program, "target": target,
        })
        require(reply.status == 201, "A valid program selection must create an executable attempt.")
        value = reply.json()
        self.secrets.append(value["resume_credential"])
        return value

    def upload(self, token, attempt, *, data=None, aed=None, alias=True):
        if data is None:
            data = (ROOT / "tests/dataset/cco_1.bin").read_bytes()
        parts = {"rawHexBPfile": data, "condition": json.dumps(attempt["condition"])}
        if aed is not None:
            parts["aedHexBPfile"] = aed
        event = multipart_event(parts)
        headers = {**event["headers"], "X-Attempt-ID": attempt["attempt_id"]}
        return self.request("POST", "/cpr-analysis" if alias else attempt["calculation_path"],
                            body=base64.b64decode(event["body"]), token=token, headers=headers)

    def result(self, token, attempt, *, seconds=30):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            reply = self.request("GET", attempt["calculation_path"], token=token)
            if reply.status == 200:
                return reply
            require(reply.status == 202, "The real worker returned an unexpected terminal error.")
            time.sleep(0.1)
        raise AssertionError("The default CLI did not automatically calculate within this test wait budget.")

    def chart(self, url):
        value = urlsplit(url)
        require(value.scheme == "http" and value.netloc == f"127.0.0.1:{self.port}"
                and not value.query and not value.fragment, "Chart must use this owned local listener.")
        self.secrets.append(value.path.rsplit("/", 1)[-1])
        return self.request("GET", value.path)

    def owned_worker_pid(self):
        children = subprocess.run(["pgrep", "-P", str(self.process.pid)],
                                  capture_output=True, text=True, timeout=3)
        matches = []
        for value in children.stdout.split():
            require(value.isdecimal(), "Unexpected owned process inventory.")
            identity = subprocess.run(["ps", "-p", value, "-o", "ppid=,args="],
                                      capture_output=True, text=True, timeout=3)
            parts = identity.stdout.strip().split(None, 1)
            if (identity.returncode == 0 and len(parts) == 2 and parts[0] == str(self.process.pid)
                    and "multiprocessing.spawn" in parts[1]):
                matches.append(int(value))
        require(len(matches) == 1, "Refusing to signal any process without proven test ownership.")
        return matches[0]


@pytest.fixture
def journey_cli(tmp_path):
    server = JourneyServer(tmp_path / "runtime")
    try:
        server.start()
        yield server
        server.assert_private_logs()
    finally:
        server.stop()


def test_default_cli_automatically_calculates_and_persists_real_chart(journey_cli):
    server = journey_cli
    health = server.request("GET", "/healthz").json()
    require(health.get("calculator_available") is True, "Ready must reflect the actual owned worker.")
    token = server.login()["session_token"]
    attempt = server.attempt(token)
    require(server.upload(token, attempt).status in (200, 202), "The first HTTP upload must be durably accepted.")
    response = server.result(token, attempt)
    result = response.json()
    require(result.get("submit_arc") == {"status": "disabled", "ok": False, "error": "arc_contract_pending"},
            "Local calculation must not claim ARC submission.")
    require("submit_hstm" not in result, "The retired submission field must not return.")
    require(result["action_count"]["comp"] >= 60, "Recorded compression data must reach the real calculator.")
    chart = server.chart(result["chart_dataset_url"])
    require(chart.status == 200 and type(chart.json()) in (dict, list), "The returned chart must actually download.")
    require(chart.headers.get("Cache-Control") == "no-store", "Chart response must preserve privacy headers.")
    assessment = server.request("GET", f'/mock/v1/attempts/{attempt["attempt_id"]}', token=token).json()
    require(assessment["evaluation"]["program_completed"] is True, "Known Only target and real Pass must complete.")
    files = sorted((server.data / "objects").glob("*.object"))
    require(len(files) >= 6, "The product must persist raw input, candidates and final artifacts.")
    stored = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in files}
    server.stop()
    server.start()
    require(server.request("GET", attempt["calculation_path"], token=token).body == response.body,
            "A restart must preserve the committed calculation bytes.")
    require(server.chart(result["chart_dataset_url"]).body == chart.body,
            "The same unexpired chart capability must survive a restart.")
    require(server.upload(token, attempt, alias=False).body == response.body,
            "Matching input retry must return the committed result.")
    after = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
             for path in (server.data / "objects").glob("*.object")}
    require(after == stored, "Restart/read/retry must not create another calculation artifact.")


def test_control_installation_can_enable_journey_without_losing_session(tmp_path):
    old = LiveServer(tmp_path / "migration")
    new = None
    try:
        old.start()
        token = old.login()["session_token"]
        programs = old.request("GET", "/mock/v1/programs", token=token).json()
        old.stop()
        new = JourneyServer(old.work)
        new.port, new.db_port = old.port, old.db_port
        new.logs = list(old.logs)
        new.secrets.append(token)
        new.start()
        restored = new.request("GET", "/mock/v1/programs", token=token)
        require(restored.status == 200 and restored.json() == programs,
                "Additive journey initialization must preserve session and shared progress.")
        attempt = new.attempt(token)
        require(new.upload(token, attempt).status in (200, 202), "Migrated owned DB must accept real calculation.")
        require(new.result(token, attempt).status == 200, "The due index must support automatic worker execution.")
        new.assert_private_logs()
        old.assert_private_logs()
    finally:
        old.stop()
        if new is not None:
            new.stop()


def test_dead_owned_worker_stops_acceptance_and_restart_recovers(journey_cli):
    server = journey_cli
    token = server.login()["session_token"]
    attempt = server.attempt(token)
    worker = server.owned_worker_pid()
    os.kill(worker, signal.SIGTERM)
    require(server.process.wait(timeout=30) != 0, "An unexpectedly dead worker must cause a visible CLI failure.")
    server.stop()
    server.start()
    require(server.upload(token, attempt).status in (200, 202), "A restart must preserve the original session and attempt.")
    require(server.result(token, attempt).status == 200, "A newly supervised worker must process preserved state.")

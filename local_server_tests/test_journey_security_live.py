"""L4 actual default CLI: chart capabilities, private files and quota recovery.

Every HTTP operation reaches the owned product CLI/DB/worker. No application,
calculator, clock, signer or file client is injected. The input is a recorded
binary fixture, not evidence that an iPad/physical manikin was used.
"""

import hashlib
import json
import os
import stat
import struct
from urllib.parse import urlsplit

import pytest

from local_server_tests.test_journey_runtime import DUMMY_EXCLUDED, JourneyServer
from local_server_tests.test_live_server import ROOT, dummy_course, error, require

# Real loopback sockets: opted out of the directory network guard (conftest.py).
pytestmark = pytest.mark.loopback


class QuotaJourneyServer(JourneyServer):
    quota_bytes = None

    def command(self):
        value = super().command()
        if self.quota_bytes is not None:
            value += ["--storage-quota-bytes", str(self.quota_bytes)]
        return value


def objects_snapshot(server):
    """Inspect only this test's private objects; suppress all operands on fail."""
    directory = server.data / "objects"
    info = directory.lstat()
    require(stat.S_ISDIR(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o700
            and info.st_uid == os.getuid(), "Object directory must remain private.")
    snapshot, records = {}, []
    for path in directory.glob("*.object"):
        info = path.lstat()
        require(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600
                and info.st_uid == os.getuid() and info.st_nlink == 1,
                "Objects must be private, owned regular files with a single link.")
        require(info.st_size <= 8_000_000 + 16_384 + len(b"ARCLOCAL1\x00") + 4,
                "Stored object exceeded the local envelope bound.")
        body = path.read_bytes()
        require(body.startswith(b"ARCLOCAL1\x00"), "Private object envelope is invalid.")
        prefix = len(b"ARCLOCAL1\x00")
        require(len(body) >= prefix + 4, "Private object envelope is truncated.")
        length = struct.unpack("!I", body[prefix:prefix + 4])[0]
        require(1 <= length <= 16_384, "Private object header exceeds its bound.")
        try:
            header = json.loads(body[prefix + 4:prefix + 4 + length])
        except (ValueError, UnicodeError):
            raise AssertionError("Private object header is invalid; contents suppressed.") from None
        raw = body[prefix + 4 + length:]
        require(header.get("size") == len(raw)
                and header.get("sha256") == hashlib.sha256(raw).hexdigest(),
                "Private object's stored size/hash must match its complete bytes.")
        snapshot[path.name] = hashlib.sha256(body).hexdigest()
        records.append((path.name, header, raw))
    require(len(records) >= 6, "The actual journey must persist its calculation artifacts.")
    return snapshot, records


def material_snapshot(server):
    snapshot = {}
    for filename, secret_field in (("installation.json", "resume_key"),
                                   ("object-storage.json", "chart_key")):
        path = server.data / filename
        info = path.lstat()
        require(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600
                and info.st_uid == os.getuid() and info.st_nlink == 1,
                "Installation material must remain private and owned.")
        raw = path.read_bytes()
        require(len(raw) <= 4096, "Installation material exceeded its size bound.")
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError):
            raise AssertionError("Installation material is invalid; contents suppressed.") from None
        require(type(value.get(secret_field)) is str, "Expected private key material is missing.")
        server.secrets.append(value[secret_field])
        snapshot[filename] = hashlib.sha256(raw).hexdigest()
    return snapshot


def complete(server):
    token = server.token()
    attempt = server.start_attempt(token)
    require(server.upload(token, attempt).status in (200, 202),
            "Recorded binary must be accepted or already calculated by the actual worker.")
    result = server.result(token, attempt).data()
    require(result["submit_arc"] == DUMMY_EXCLUDED, "A local result must not claim actual ARC submission.")
    chart = server.chart(result["calculation"]["chart_dataset_url"])
    require(chart.status == 200, "The default runtime must serve its own signed chart.")
    return token, attempt, result, chart


def test_actual_cli_chart_is_narrow_and_survives_logout_without_exposing_originals(tmp_path):
    server = JourneyServer(tmp_path / "chart-boundary")
    try:
        server.start()
        token, attempt, result, chart = complete(server)
        files, records = objects_snapshot(server)
        material = material_snapshot(server)
        original = (ROOT / "tests/dataset/cco_1.bin").read_bytes()
        raw_records = [(name, header, body) for name, header, body in records
                       if header["key"].endswith(".bin") and "/_artifacts/" not in header["key"]]
        require(len(raw_records) == 1 and raw_records[0][2] == original,
                "Stored original measurement must exactly match the uploaded recorded bytes.")
        require(chart.headers.get("Cache-Control") == "no-store"
                and chart.headers.get("X-Content-Type-Options") == "nosniff"
                and chart.headers.get("Content-Type") == "application/json",
                "Signed charts must retain the privacy and type headers.")
        path = urlsplit(result["calculation"]["chart_dataset_url"]).path
        replacement = "A" if path[-1] != "A" else "B"
        invalid = server.request("GET", path[:-1] + replacement)
        require(invalid.status == 404 and invalid.json().get("error", {}).get("code") == "NOT_FOUND",
                "A modified chart capability must fail with a fixed not-found response.")
        require(path.rsplit("/", 1)[-1].encode() not in invalid.body,
                "A failed chart request must not echo the capability.")
        raw_name, raw_header, _ = raw_records[0]
        for target in ("/objects/", "/objects/" + raw_name, "/" + raw_header["key"],
                       "/local/v1/charts/" + raw_name, "/installation.json", "/object-storage.json"):
            denied = server.request("GET", target)
            require(denied.status == 404, "Private files and directories must have no HTTP download route.")
            require(original not in denied.body, "Original measurement bytes leaked through an invalid route.")
        chart_token = path.rsplit("/", 1)[-1]
        for protected in (server.calculation_path(attempt), f'/api/v2/attempts/{attempt["attemptId"]}/chart-link/'):
            error(server.request("GET", protected), 401, "SESSION_REQUIRED")
            error(server.request("GET", protected, token=chart_token), 401, "SESSION_REQUIRED")
        require(server.request("DELETE", "/api/v2/session/", token=token).status == 204,
                "The actual session must log out.")
        error(server.calculation(token, attempt), 403, "SESSION_REVOKED")
        require(server.chart(result["calculation"]["chart_dataset_url"]).body == chart.body,
                "An already issued chart must remain readable until its existing expiry after logout.")
        after, _ = objects_snapshot(server)
        require(after == files, "HTTP reads and logout must not alter stored measurement or result objects.")
        require(material_snapshot(server) == material, "Logout must not rotate the stable installation keys.")
        server.assert_private_logs()
    finally:
        server.stop()
        server.assert_private_logs()


def test_actual_cli_lower_quota_restart_keeps_reads_and_replay_but_rejects_new_upload(tmp_path):
    server = QuotaJourneyServer(tmp_path / "quota-restart")
    try:
        server.start()
        token, attempt, result, chart = complete(server)
        committed = server.calculation(token, attempt).stable_body()
        before, _ = objects_snapshot(server)
        material = material_snapshot(server)
        server.stop()
        server.quota_bytes = 1
        server.start()
        require(server.request("GET", "/healthz").status == 200,
                "A storage quota does not invalidate already readable files and owned workers.")
        require(server.calculation(token, attempt).stable_body() == committed,
                "Lowering only storage quota must preserve the existing calculation bytes.")
        require(server.chart(result["calculation"]["chart_dataset_url"]).body == chart.body,
                "Existing unexpired charts must remain readable with a full quota.")
        replay = server.upload(token, attempt)
        require(replay.stable_body() == committed,
                "A matching committed input replay must not require a new object write.")
        pending = server.start_attempt(token, dummy_course("mock-ventilation-only"))
        failure = server.upload(token, pending, data=(ROOT / "tests/dataset/adult_vo_1.bin").read_bytes())
        error(failure, 503, "TEMPORARILY_UNAVAILABLE")
        status = server.attempt(token, pending).data()
        require(status["state"] == "created", "A storage rejection must not create an accepted calculation.")
        error(server.calculation(token, pending), 409, "INVALID_STATE")
        after, _ = objects_snapshot(server)
        require(after == before, "Quota rejection must not overwrite or delete existing objects.")
        require(material_snapshot(server) == material, "Quota configuration must not regenerate installation keys.")
        require(server.calculation(token, attempt).stable_body() == committed,
                "A failed new upload must leave the prior result bytes available.")
        server.assert_private_logs()
    finally:
        server.stop()
        server.assert_private_logs()

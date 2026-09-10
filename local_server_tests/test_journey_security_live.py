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

from local_server_tests.test_journey_runtime import JourneyServer
from local_server_tests.test_live_server import ROOT, require


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
    token = server.login()["session_token"]
    attempt = server.attempt(token)
    require(server.upload(token, attempt).status in (200, 202),
            "Recorded binary must be accepted or already calculated by the actual worker.")
    response = server.result(token, attempt)
    result = response.json()
    require(result.get("submit_arc") == {"status": "disabled", "ok": False, "error": "arc_contract_pending"},
            "A local result must not claim actual ARC submission.")
    chart = server.chart(result["chart_dataset_url"])
    require(chart.status == 200, "The default runtime must serve its own signed chart.")
    return token, attempt, response, result, chart


def test_actual_cli_chart_is_narrow_and_survives_logout_without_exposing_originals(tmp_path):
    server = JourneyServer(tmp_path / "chart-boundary")
    try:
        server.start()
        token, attempt, response, result, chart = complete(server)
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
        path = urlsplit(result["chart_dataset_url"]).path
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
        require(server.request("GET", attempt["calculation_path"]).status == 401,
                "A chart capability must not authorize calculation-result access.")
        require(server.request("DELETE", "/mock/v1/session", token=token).status == 204,
                "The actual session must log out.")
        require(server.request("GET", attempt["calculation_path"], token=token).status == 403,
                "The revoked session must not authorize result access.")
        require(server.chart(result["chart_dataset_url"]).body == chart.body,
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
        token, attempt, response, result, chart = complete(server)
        before, _ = objects_snapshot(server)
        material = material_snapshot(server)
        server.stop()
        server.quota_bytes = 1
        server.start()
        require(server.request("GET", "/healthz").status == 200,
                "A storage quota does not invalidate already readable files and owned workers.")
        require(server.request("GET", attempt["calculation_path"], token=token).body == response.body,
                "Lowering only storage quota must preserve existing calculation response bytes.")
        require(server.chart(result["chart_dataset_url"]).body == chart.body,
                "Existing unexpired charts must remain readable with a full quota.")
        replay = server.upload(token, attempt, alias=False)
        require(replay.status == 200 and replay.body == response.body,
                "A matching committed input replay must not require a new object write.")
        pending = server.attempt(token, program="mock-ventilation-only")
        failure = server.upload(token, pending, data=(ROOT / "tests/dataset/adult_vo_1.bin").read_bytes())
        require(failure.status == 503 and failure.json().get("error", {}).get("code") == "TEMPORARILY_UNAVAILABLE",
                "A new upload beyond quota must fail safely instead of claiming a calculated result.")
        status = server.request("GET", "/mock/v1/attempts/" + pending["attempt_id"], token=token).json()
        require(status.get("state") == "created" and status.get("evaluation") is None,
                "A storage rejection must not create a completed or accepted calculation.")
        after, _ = objects_snapshot(server)
        require(after == before, "Quota rejection must not overwrite or delete existing objects.")
        require(material_snapshot(server) == material, "Quota configuration must not regenerate installation keys.")
        require(server.request("GET", attempt["calculation_path"], token=token).body == response.body,
                "A failed new upload must leave the prior result available.")
        server.assert_private_logs()
    finally:
        server.stop()
        server.assert_private_logs()

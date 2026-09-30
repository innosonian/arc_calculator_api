"""L4 /api/v2 measurement wire compatibility against the actual default CLI and owned worker.

Recorded measurement fixtures cross TCP; no parser, service, storage or worker
is replaced. These checks are not evidence of an iPad/manikin acceptance run.
"""

import base64
import hashlib
import json
from urllib.parse import quote, urlencode

import pytest

from local_server_tests.test_journey_runtime import JourneyServer
from local_server_tests.test_live_server import ROOT, error, multipart, require

# Real loopback sockets: opted out of the directory network guard (conftest.py).
pytestmark = pytest.mark.loopback


@pytest.fixture
def transport_cli(tmp_path):
    server = JourneyServer(tmp_path / "transport")
    try:
        server.start()
        yield server
    finally:
        server.stop()
        server.assert_private_logs()


def _form_upload(server, token, attempt, binary, *, content_type):
    # Preserve the existing encoded-form contract. This is not an ordinary
    # JSON body and the HTTP adapter must not base64-encode it a second time.
    fields = {"condition": json.dumps(attempt["condition"]),
              "cpr_b64_data": base64.urlsafe_b64encode(binary).decode("ascii")}
    body = base64.urlsafe_b64encode(quote(urlencode(fields), safe="").encode("utf-8"))
    headers = {} if content_type is None else {"Content-Type": content_type}
    require(len(body) > 16 * 1024, "The recorded form must exercise the calculation-sized HTTP boundary.")
    return server.request("POST", server.calculation_path(attempt), body=body, token=token, headers=headers)


def _stored_fingerprints(server):
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (server.data / "objects").glob("*.object")}


@pytest.mark.parametrize("content_type", ["application/x-www-form-urlencoded", None])
def test_actual_base64_form_and_multipart_retry_share_one_result(transport_cli, content_type):
    server = transport_cli
    token = server.token()
    attempt = server.start_attempt(token)
    binary = (ROOT / "tests/dataset/cco_1.bin").read_bytes()
    first = _form_upload(server, token, attempt, binary, content_type=content_type)
    require(first.status in (200, 202), "A valid encoded form must reach durable calculation acceptance.")
    committed = server.result(token, attempt)
    result = committed.data()
    require(result["calculation"]["action_count"]["comp"] >= 60, "The recorded bytes must reach the real calculator.")
    before = _stored_fingerprints(server)
    require(bool(before), "A committed calculation must have private stored artifacts.")
    same_form = _form_upload(server, token, attempt, binary, content_type=content_type)
    same_multipart = server.upload(token, attempt, data=binary)
    require(same_form.status == same_multipart.status == 200,
            "A committed input retry through either wire format must return HTTP 200.")
    require(same_form.stable_body() == same_multipart.stable_body() == committed.stable_body(),
            "Wire format changes must not change committed calculation bytes.")
    different = server.upload(token, attempt, data=(ROOT / "tests/dataset/cpr_1.bin").read_bytes())
    error(different, 409, "ATTEMPT_INPUT_CONFLICT")
    require(server.calculation(token, attempt).stable_body() == committed.stable_body(),
            "A conflicting measurement must not replace the committed result bytes.")
    require(_stored_fingerprints(server) == before,
            "Retry and rejected conflicting input must not create or replace stored artifacts.")


def test_actual_upload_authenticates_and_checks_ownership_before_rejecting_invalid_measurement(transport_cli):
    server = transport_cli
    owner, other = server.token(), server.token()
    attempt = server.start_attempt(owner)
    body, content_type = multipart({"condition": json.dumps(attempt["condition"])})
    path, headers = server.calculation_path(attempt), {"Content-Type": content_type}
    error(server.request("POST", path, body=body, headers=headers), 401, "SESSION_REQUIRED")
    error(server.request("POST", path, body=body, headers=headers, token=other), 404, "NOT_FOUND")
    require(server.attempt(owner, attempt).data()["state"] == "created",
            "Unauthorized input must not accept, fail, or cancel another session's attempt.")
    accepted = server.upload(owner, attempt)
    require(accepted.status in (200, 202), "The owner must still be able to submit the original attempt.")
    committed = server.result(owner, attempt)
    error(server.request("GET", path), 401, "SESSION_REQUIRED")
    error(server.calculation(other, attempt), 404, "NOT_FOUND")
    error(server.upload(other, attempt), 404, "NOT_FOUND")
    error(server.request("GET", f'/api/v2/attempts/{attempt["attemptId"]}/chart-link/', token=other), 404, "NOT_FOUND")
    require(server.calculation(owner, attempt).stable_body() == committed.stable_body(),
            "Foreign session requests must not alter the owner's stored result bytes.")


def test_actual_malformed_and_oversize_uploads_leave_attempt_recoverable(transport_cli):
    server = transport_cli
    token = server.token()
    attempt = server.start_attempt(token)
    # Login/start already stored the private course definition snapshots.
    before = _stored_fingerprints(server)
    truncated = (ROOT / "tests/dataset/cco_1.bin").read_bytes()[:-1]
    error(server.upload(token, attempt, data=truncated), 422, "MEASUREMENT_INPUT_INVALID")
    # Declared size exceeds the default 1,000,000-byte wire cap. Send no body:
    # the real parser must reject before buffering an oversized measurement.
    request = (f"POST {server.calculation_path(attempt)} HTTP/1.1\r\nHost: 127.0.0.1:{server.port}\r\n"
               f"Authorization: Bearer {token}\r\n"
               "Content-Type: multipart/form-data; boundary=oversized\r\n"
               "Content-Length: 1000001\r\nConnection: close\r\n\r\n").encode("ascii")
    require(server.raw(request).status == 413, "The real HTTP parser must reject an oversized calculation body.")
    # The one-byte-smaller exact wire cap is admitted by the parser (and then
    # rejected as an invalid measurement, never as a size violation).
    exact = b"A" * 1_000_000
    error(server.request("POST", server.calculation_path(attempt), body=exact, token=token,
                         headers={"Content-Type": "multipart/form-data; boundary=oversized"}),
          422, "MEASUREMENT_INPUT_INVALID")
    require(server.attempt(token, attempt).data()["state"] == "created",
            "Invalid or oversized input must leave this unsubmitted attempt recoverable.")
    require(_stored_fingerprints(server) == before, "Rejected input must not create private calculation artifacts.")
    require(server.request("GET", "/healthz").status == 200, "Invalid input must not kill the actual runtime.")
    accepted = server.upload(token, attempt)
    require(accepted.status in (200, 202), "The same attempt must accept a later valid measurement.")
    require(server.result(token, attempt).status == 200, "The owned worker must calculate after rejected requests.")

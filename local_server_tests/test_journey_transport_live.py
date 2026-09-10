"""L4 wire compatibility against the actual default CLI and owned worker.

Recorded measurement fixtures cross TCP; no parser, service, storage or worker
is replaced. These checks are not evidence of an iPad/manikin acceptance run.
"""

import base64
import hashlib
import json
from urllib.parse import quote, urlencode

import pytest

from local_server_tests.test_journey_runtime import JourneyServer
from local_server_tests.test_live_server import ROOT, error, require
from tests._synth import multipart_event


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
    headers = {"X-Attempt-ID": attempt["attempt_id"]}
    if content_type is not None:
        headers["Content-Type"] = content_type
    require(len(body) > 16 * 1024, "The recorded form must exercise the calculation-sized HTTP boundary.")
    return server.request("POST", "/cpr-analysis", body=body, token=token, headers=headers)


def _stored_fingerprints(server):
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (server.data / "objects").glob("*.object")}


@pytest.mark.parametrize("content_type", ["application/x-www-form-urlencoded", None])
def test_actual_base64_form_and_multipart_retry_share_one_result(transport_cli, content_type):
    server = transport_cli
    token = server.login()["session_token"]
    attempt = server.attempt(token)
    binary = (ROOT / "tests/dataset/cco_1.bin").read_bytes()
    first = _form_upload(server, token, attempt, binary, content_type=content_type)
    require(first.status in (200, 202), "A valid encoded form must reach durable calculation acceptance.")
    result = server.result(token, attempt)
    require(result.json()["action_count"]["comp"] >= 60, "The recorded bytes must reach the real calculator.")
    before = _stored_fingerprints(server)
    require(bool(before), "A committed calculation must have private stored artifacts.")
    same_form = _form_upload(server, token, attempt, binary, content_type=content_type)
    same_multipart = server.upload(token, attempt, data=binary, alias=False)
    require(same_form.status == same_multipart.status == 200,
            "A committed input retry through either wire format must return HTTP 200.")
    require(same_form.body == same_multipart.body == result.body,
            "Wire format and route changes must not change a committed calculation result.")
    different = server.upload(token, attempt, data=(ROOT / "tests/dataset/cpr_1.bin").read_bytes())
    error(different, 409, "ATTEMPT_INPUT_CONFLICT")
    require(server.request("GET", attempt["calculation_path"], token=token).body == result.body,
            "A conflicting measurement must not replace the committed result.")
    require(_stored_fingerprints(server) == before,
            "Retry and rejected conflicting input must not create or replace stored artifacts.")


def test_actual_alias_authenticates_before_rejecting_invalid_measurement(transport_cli):
    server = transport_cli
    owner, other = server.login()["session_token"], server.login()["session_token"]
    attempt = server.attempt(owner)
    malformed = multipart_event({"condition": json.dumps(attempt["condition"])})
    body = base64.b64decode(malformed["body"])
    headers = {**malformed["headers"], "X-Attempt-ID": attempt["attempt_id"]}
    error(server.request("POST", "/cpr-analysis", body=body, headers=headers), 401, "SESSION_REQUIRED")
    error(server.request("POST", "/cpr-analysis", body=body, headers=headers, token=other), 404, "NOT_FOUND")
    created = server.request("GET", f'/mock/v1/attempts/{attempt["attempt_id"]}', token=owner)
    require(created.status == 200 and created.json()["state"] == "created",
            "Unauthorized input must not accept, fail, or cancel another session's attempt.")
    accepted = server.upload(owner, attempt)
    require(accepted.status in (200, 202), "The owner must still be able to submit the original attempt.")
    result = server.result(owner, attempt)
    error(server.request("GET", attempt["calculation_path"]), 401, "SESSION_REQUIRED")
    error(server.request("GET", attempt["calculation_path"], token=other), 404, "NOT_FOUND")
    error(server.upload(other, attempt), 404, "NOT_FOUND")
    require(server.request("GET", attempt["calculation_path"], token=owner).body == result.body,
            "Foreign session requests must not alter the owner's stored result.")


def test_actual_malformed_and_oversize_uploads_leave_attempt_recoverable(transport_cli):
    server = transport_cli
    token = server.login()["session_token"]
    attempt = server.attempt(token)
    truncated = (ROOT / "tests/dataset/cco_1.bin").read_bytes()[:-1]
    malformed = server.upload(token, attempt, data=truncated)
    require(malformed.status == 400 and malformed.json() == {
        "type": "client_error", "message": "Invalid request data.",
    }, "Alias malformed binary must retain the sanitized legacy HTTP 400 contract.")
    error(server.upload(token, attempt, data=truncated, alias=False), 422, "MEASUREMENT_INPUT_INVALID")
    # Declared size exceeds the default 1,000,000-byte wire cap. Send no body:
    # the real parser must reject before buffering an oversized measurement.
    request = (f"POST /cpr-analysis HTTP/1.1\r\nHost: 127.0.0.1:{server.port}\r\n"
               f"Authorization: Bearer {token}\r\nX-Attempt-ID: {attempt['attempt_id']}\r\n"
               "Content-Type: multipart/form-data; boundary=oversized\r\n"
               "Content-Length: 1000001\r\nConnection: close\r\n\r\n").encode("ascii")
    require(server.raw(request).status == 413, "The real HTTP parser must reject an oversized calculation body.")
    created = server.request("GET", f'/mock/v1/attempts/{attempt["attempt_id"]}', token=token)
    require(created.status == 200 and created.json()["state"] == "created",
            "Invalid or oversized input must leave this unsubmitted attempt recoverable.")
    require(not _stored_fingerprints(server), "Rejected input must not create private calculation artifacts.")
    require(server.request("GET", "/healthz").status == 200, "Invalid input must not kill the actual runtime.")
    accepted = server.upload(token, attempt)
    require(accepted.status in (200, 202), "The same attempt must accept a later valid measurement.")
    require(server.result(token, attempt).status == 200, "The owned worker must calculate after rejected requests.")

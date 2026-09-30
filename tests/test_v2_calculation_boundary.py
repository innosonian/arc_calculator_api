"""Authenticated /api/v2 calculation boundary; no sockets, AWS, or running HTTP server.

Moved from tests/test_authenticated_calculation_http.py (the removed v1 route
and its alias) onto the public v2 routes:

    POST /api/v2/attempts/{attemptId}/calculation/   (multipart measurement)
    GET  /api/v2/attempts/{attemptId}/calculation/   (stored result, read-only)

The application, sessions, started attempt and the "nothing accepted"/"never
read" checks are the shared boundary harness (tests/v2_boundary_support.py):
the real course_v2 composition on the in-memory DynamoDB client, prepared once
per module and forked for every test.

Only current /api/v2 behavior is asserted. The multi-value Content-Type
normalization and ambiguity rejection ported from the v1 route are asserted in
tests/test_v2_measurement_content_type.py. X-Attempt-ID/alias selection and the
alias's legacy error body were v1-only and are not carried over. The stored
final bodies the removed v1 snapshot composer refused (not one strict JSON
object) are asserted here on the v2 GET as a fixed 503 without details.
"""

import base64
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

import lambda_handler
from mock_journey import typed
from tests.journey_support import measurement_for
from tests.v2_boundary_support import (  # noqa: F401 (prepared_world, world fixtures)
    SECRET, WRITE_OPERATIONS, assert_error, assert_nothing_accepted, attempt_reads, attempt_row, call, forbid_parser,
    job_rows, new_objects, prepared_world, upload_event, world, writes,
)


CONTEXT = SimpleNamespace(aws_request_id="c1234567-1234-4234-9234-123456789abc")


@pytest.mark.parametrize("method", ["POST", "GET"])
def test_missing_bearer_stops_before_body_or_attempt_access(world, method):
    value = upload_event(world, method=method)
    value["headers"].pop("Authorization")
    value["body"] = object()  # Any body access before authentication would fail differently.
    assert_error(call(world, value), 401, "SESSION_REQUIRED")
    assert attempt_reads(world) == []
    assert_nothing_accepted(world)


# (single header value, multiValueHeaders value, expected status, expected code). "{T}" is the
# owner's valid token, so only the ambiguity -- never an unknown token -- causes the rejection.
AMBIGUOUS_AUTHORIZATION = [
    ("Basic {T}", None, 401, "SESSION_REQUIRED"),
    ("Bearer ", None, 401, "SESSION_REQUIRED"),
    ("Bearer  {T}", None, 401, "SESSION_REQUIRED"),
    ("Bearer {T}, Bearer {T}", None, 401, "SESSION_REQUIRED"),
    (["Bearer {T}"], None, 400, "INVALID_REQUEST"),
    ("Bearer {T}", ["Bearer {T}", "Bearer {T}"], 400, "INVALID_REQUEST"),
    ("Bearer {T}", ["Bearer other"], 400, "INVALID_REQUEST"),
    ("Bearer {T}", [], 400, "INVALID_REQUEST"),
]


@pytest.mark.parametrize("single,multi,status,code", AMBIGUOUS_AUTHORIZATION)
def test_ambiguous_authorization_never_reaches_a_calculation(world, single, multi, status, code):
    def fill(value):
        return value.replace("{T}", world.owner.token)

    value = upload_event(world)
    value["headers"]["Authorization"] = [fill(item) for item in single] if type(single) is list else fill(single)
    if multi is not None:
        value["multiValueHeaders"] = {"Authorization": [fill(item) for item in multi]}
    assert_error(call(world, value), status, code)
    assert attempt_reads(world) == []
    assert_nothing_accepted(world)


def test_case_duplicated_authorization_headers_are_rejected_even_when_equal(world):
    value = upload_event(world)
    value["headers"]["authorization"] = value["headers"]["Authorization"]
    assert_error(call(world, value), 400, "INVALID_REQUEST")
    assert attempt_reads(world) == []
    assert_nothing_accepted(world)


@pytest.mark.parametrize("attack", ["body", "authorizer_claim", "alternate_header", "worker_event"])
def test_credentials_and_worker_markers_outside_bearer_do_not_authorize(world, attack):
    value = upload_event(world)
    value["headers"].pop("Authorization")
    token = world.owner.token
    if attack == "body":
        value.update(isBase64Encoded=False, body=json.dumps({
            "access_token": token, "session_token": token, "attempt_id": world.attempt_id, "password": "2222",
        }))
    elif attack == "authorizer_claim":
        value["requestContext"] = {"authorizer": {"principalId": "dummy-tester", "session_token": token}}
    elif attack == "alternate_header":
        value["headers"]["X-User-Token"] = token
    else:
        value["Records"] = [{"body": json.dumps({"job_id": world.attempt_id})}]
        value["job_id"] = world.attempt_id
    assert_error(call(world, value), 401, "SESSION_REQUIRED")
    assert attempt_reads(world) == []
    assert_nothing_accepted(world)


@pytest.mark.parametrize("authenticated", [False, True])
@pytest.mark.parametrize("query_field", ["queryStringParameters", "multiValueQueryStringParameters"])
def test_query_values_cannot_supply_credentials_or_attempt_selection(world, query_field, authenticated):
    value = upload_event(world)
    if not authenticated:
        value["headers"].pop("Authorization")
    query = {"session_token": world.owner.token, "attemptId": world.attempt_id}
    value[query_field] = {key: [item] for key, item in query.items()} if query_field.startswith("multi") else query
    assert_error(call(world, value), 400, "INVALID_REQUEST")
    assert attempt_reads(world) == []
    assert_nothing_accepted(world)


@pytest.mark.parametrize("method", ["POST", "GET"])
@pytest.mark.parametrize("state", ["expired", "revoked"])
def test_stale_tokens_are_rejected_before_body_or_attempt_access(world, method, state):
    if state == "expired":
        world.h.advance(86400)  # SESSION_SECONDS from the login instant.
    else:
        world.h.logout(world.owner.token)
    value = upload_event(world, method=method)
    value["body"] = object()
    reply = call(world, value)
    assert_error(reply, *((401, "SESSION_EXPIRED") if state == "expired" else (403, "SESSION_REVOKED")))
    assert attempt_reads(world) == []
    assert_nothing_accepted(world)


@pytest.mark.parametrize("method", ["POST", "GET"])
def test_same_dummy_other_session_cannot_parse_submit_or_read_this_attempt(world, method, monkeypatch):
    forbid_parser(monkeypatch, "Foreign attempt reached measurement parsing.")
    value = upload_event(world, token=world.other.token, method=method)
    assert_error(call(world, value), 404, "NOT_FOUND")
    # Positive control for attempt_reads(): the ownership check itself read the row.
    assert attempt_reads(world)
    assert_nothing_accepted(world)


@pytest.mark.parametrize("body", [object(), "%%not-base64%%"], ids=["non_string", "bad_base64"])
def test_malformed_body_is_rejected_the_same_way_before_any_attempt_is_read(world, body, monkeypatch):
    """Current v2 order: the POST body is decoded before the ownership check.

    The removed v1 route answered 404 to a foreign session without touching the
    body; v2 decodes base64 in the route dispatcher first, so a malformed body is
    400 for every caller. The 400 is decided before any ATTEMPT read, so it is the
    same for the owner, another session and an unknown attempt id (no existence
    oracle), and the measurement parser is never reached.
    """
    forbid_parser(monkeypatch, "Malformed body reached measurement parsing.")
    unknown = "00000000-0000-4000-8000-000000000000"
    for token, attempt_id in ((world.other.token, world.attempt_id), (world.owner.token, world.attempt_id),
                              (world.other.token, unknown)):
        value = upload_event(world, token=token)
        value["path"] = f"/api/v2/attempts/{attempt_id}/calculation/"
        value["body"] = body
        assert_error(call(world, value), 400, "INVALID_REQUEST")
        assert attempt_reads(world) == [] and [operation for operation, request in world.requests
                                               if unknown in repr(request)] == []
    assert_nothing_accepted(world)


def test_matching_single_and_multi_authorization_keeps_the_existing_input_bytes(world):
    value = upload_event(world)
    value["multiValueHeaders"] = {"authorization": [value["headers"]["Authorization"]]}
    reply = call(world, value)
    assert reply.status == 202 and reply.data["calculationStatus"] == "pending"
    saved = {key[1]: world.h.objects.objects[key]["Body"] for key in new_objects(world)}
    assert world.measurement in saved.values()
    request = next(body for key, body in saved.items() if key.endswith(".request.json"))
    manifest = typed.parse_json(request)
    assert typed.canonical_bytes(manifest["payload"]["calculation_input"]["condition"]) == typed.canonical_bytes(
        world.condition)


def test_repeated_submission_shares_one_accepted_job_and_typed_input_identity(world):
    first = call(world, upload_event(world))
    stored = {key: world.h.objects.objects[key]["Body"] for key in new_objects(world)}
    second = call(world, upload_event(world))
    assert first.status == second.status == 202
    assert first.data == second.data
    jobs = job_rows(world)
    assert len(jobs) == 1
    assert {key: world.h.objects.objects[key]["Body"] for key in new_objects(world)} == stored
    row = attempt_row(world)
    assert row["state"] == "queued" and row["job_id"] == jobs[0]["job_id"]
    assert jobs[0]["input_digest"] == row["input_digest"]
    different = call(world, upload_event(world, data=measurement_for(world.condition, count=61)))
    assert_error(different, 409, "ATTEMPT_INPUT_CONFLICT")
    assert len(job_rows(world)) == 1 and attempt_row(world)["input_digest"] == row["input_digest"]


@pytest.mark.parametrize("field,value", [("is_2rescuers", 0), ("cpr_cycle_type", 302), ("is_2rescuers", None)])
def test_profile_type_change_is_not_accepted_as_the_same_condition(world, field, value):
    changed = {**world.condition, field: value}
    assert_error(call(world, upload_event(world, condition=changed)), 409, "PROFILE_MISMATCH")
    assert_nothing_accepted(world)


def test_missing_measurement_file_is_a_sanitized_input_error(world):
    value = upload_event(world)
    part = (b'--arc-integration-boundary\r\nContent-Disposition: form-data; name="condition"\r\n\r\n'
            + json.dumps(world.condition).encode() + b"\r\n--arc-integration-boundary--\r\n")
    value["body"] = base64.b64encode(part).decode()
    assert_error(call(world, value), 422, "MEASUREMENT_INPUT_INVALID")
    assert_nothing_accepted(world)


def test_session_expiry_during_input_save_does_not_report_a_successful_acceptance(world, monkeypatch):
    storage = world.h.api.calculation.storage
    original, saved = storage.save_input, []

    def expire_then_save(*args, **kwargs):
        world.h.now[0] = world.h.now[0] + 86400
        saved.append(original(*args, **kwargs))
        return saved[-1]

    monkeypatch.setattr(storage, "save_input", expire_then_save)
    assert_error(call(world, upload_event(world)), 401, "SESSION_EXPIRED")
    assert len(saved) == 1 and new_objects(world)  # The private input was written once...
    assert_nothing_accepted(world, input_saved=True)  # ...but no job was accepted or reported.


@pytest.mark.parametrize("stage", ["test", "local", "dev", "beta", "prod"])
def test_public_lambda_entrypoint_has_no_environment_or_context_auth_bypass(world, stage, monkeypatch):
    import main
    import mock_journey.runtime as runtime

    def forbidden(*args, **kwargs):
        pytest.fail("Public entrypoint bypassed the authenticated attempt service.")

    monkeypatch.setenv("STAGE", stage)
    monkeypatch.setenv("ARC_MOCK_ENABLED", "true")
    monkeypatch.setattr(runtime, "get_application", lambda: world.h.api)
    monkeypatch.setattr(lambda_handler, "_run_trusted_calculation", forbidden)
    monkeypatch.setattr(main, "run_calculator", forbidden)
    value = upload_event(world)
    value["headers"].pop("Authorization")
    value["body"] = object()
    world.requests.clear()
    response = lambda_handler.run(value, None)
    assert response["statusCode"] == 401
    assert json.loads(response["body"])["error"]["code"] == "SESSION_REQUIRED"
    assert attempt_reads(world) == []
    assert_nothing_accepted(world)


def test_public_lambda_missing_runtime_is_fail_closed_without_sdk_or_legacy_fallback(world, monkeypatch):
    import main
    import mock_journey.runtime as runtime

    def forbidden(*args, **kwargs):
        pytest.fail("Missing runtime fell back to an unauthenticated calculation.")

    monkeypatch.setattr(runtime, "_application", None)
    monkeypatch.delenv("ARC_MOCK_ENABLED", raising=False)
    monkeypatch.setenv("STAGE", "test")
    monkeypatch.setattr(lambda_handler, "_run_trusted_calculation", forbidden)
    monkeypatch.setattr(main, "run_calculator", forbidden)
    # The global test fixture rejects every actual AWS client construction.
    response = lambda_handler.run(upload_event(world), None)
    assert response["statusCode"] == 503
    assert json.loads(response["body"])["error"]["code"] == "TEMPORARILY_UNAVAILABLE"
    assert_nothing_accepted(world)


def test_public_lambda_accepts_a_valid_authenticated_attempt_via_durable_service(world, monkeypatch):
    import mock_journey.runtime as runtime

    def forbidden(*args, **kwargs):
        pytest.fail("Public request reached unauthenticated compatibility helper.")

    monkeypatch.setattr(runtime, "get_application", lambda: world.h.api)
    monkeypatch.setattr(lambda_handler, "_run_trusted_calculation", forbidden)
    response = lambda_handler.run(upload_event(world), CONTEXT)
    assert response["statusCode"] == 202
    data = json.loads(response["body"])["data"]
    assert data["attemptId"] == world.attempt_id and data["calculationStatus"] == "pending"
    assert len(job_rows(world)) == 1 and attempt_row(world)["state"] == "queued"


def test_measurement_exception_never_echoes_a_parser_message(world, monkeypatch, capsys):
    import mock_journey.legacy_bridge as bridge

    def malicious_message(*args, **kwargs):
        raise bridge.ClientError(SECRET)

    # The real bridge turns the existing validator's ClientError into the fixed
    # MeasurementInputError; no parser message is kept on the error.
    monkeypatch.setattr(bridge, "_validate_request", malicious_message)
    reply = call(world, upload_event(world))
    assert_error(reply, 422, "MEASUREMENT_INPUT_INVALID")
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err + json.dumps(world.events.records)
    assert_nothing_accepted(world)


def test_unexpected_parser_exception_is_sanitized_and_never_accepted(world, monkeypatch, capsys):
    import mock_journey.legacy_bridge as bridge

    def failed(*args, **kwargs):
        raise RuntimeError(SECRET)

    monkeypatch.setattr(bridge, "parse_measurement", failed)
    reply = call(world, upload_event(world))
    assert_error(reply, 503, "TEMPORARILY_UNAVAILABLE")
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err + json.dumps(world.events.records)
    assert_nothing_accepted(world)


def test_stored_result_get_is_read_only_and_keeps_stored_types(world, monkeypatch):
    world.h.upload(world.owner.token, world.attempt_id, world.condition)
    assert world.h.work(world.attempt_id) is True
    forbid_parser(monkeypatch, "Read-only result request parsed a measurement.")

    def forbidden(*args, **kwargs):
        pytest.fail("Read-only result request submitted measurement again.")

    monkeypatch.setattr(world.h.api.calculation, "submit", forbidden)
    rows_before = world.h.store.rows()
    objects_before = deepcopy(world.h.objects.objects)
    reply = call(world, upload_event(world, method="GET"))
    assert reply.status == 200
    data = reply.data
    assert data["attemptId"] == world.attempt_id and data["calculationStatus"] == "succeeded"
    assert data["submit_arc"]["ok"] is False and data["submit_arc"]["status"] in ("disabled", "excluded")
    # The only submission state on the wire is the top-level submit_arc receipt.
    assert list(data) == ["attemptId", "calculationStatus", "calculation", "evaluation", "progressApplication",
                          "submit_arc"]
    job = next(row for row in job_rows(world) if row["attempt_id"] == world.attempt_id)
    final = typed.parse_json(world.h.objects.objects[(job["final_ref"]["bucket"], job["final_ref"]["key"])]["Body"])
    # Submission state stays outside the stored calculation the Worker writes.
    assert [key for key in final if key.startswith("submit_")] == []
    # Typed equality: 1 and 1.0, null and missing, bool and int stay distinct.
    assert typed.canonical_bytes(data["calculation"]) == typed.canonical_bytes(final)
    assert world.h.store.rows() == rows_before and world.h.objects.objects == objects_before
    assert writes(world) == []


STALE_SUBMISSION = {"ok": True, "status": "sent", "response": {"code": 200}}


def test_stale_submission_fields_of_a_stored_final_are_left_out_of_the_response(world, monkeypatch):
    """2026-09-28 user decision (D103 note): the only submission state on the wire is
    the top-level ``submit_arc`` receipt.

    The current Worker never stores submit_* keys, so only a result in an older
    stored format could carry one. GET and the POST replay leave every top-level
    ``submit_*`` key out of ``calculation`` (the removed v1 snapshot overlay popped
    ``submit_hstm`` the same way), keep every other field and value unchanged,
    and never rewrite the stored final.
    """
    storage = world.h.worker.storage
    save_final = storage.save_final

    def with_stale_submission(response_bytes, binding, fence, chart_publication):
        stored = json.loads(response_bytes)
        assert not [key for key in stored if key.startswith("submit_")]
        stored["submit_hstm"] = STALE_SUBMISSION
        stored["submit_arc"] = {"ok": True, "status": "sent"}
        return save_final(json.dumps(stored).encode("utf-8"), binding, fence, chart_publication)

    monkeypatch.setattr(storage, "save_final", with_stale_submission)
    world.h.upload(world.owner.token, world.attempt_id, world.condition)
    assert world.h.work(world.attempt_id) is True
    job = next(row for row in job_rows(world) if row["attempt_id"] == world.attempt_id)
    stored_bytes = world.h.objects.objects[(job["final_ref"]["bucket"], job["final_ref"]["key"])]["Body"]
    final = typed.parse_json(stored_bytes)
    assert final["submit_hstm"] == STALE_SUBMISSION and final["submit_arc"]["ok"] is True
    expected = {key: value for key, value in final.items() if not key.startswith("submit_")}
    for reply in (call(world, upload_event(world, method="GET")), call(world, upload_event(world))):
        assert reply.status == 200
        data = reply.data
        assert [key for key in data["calculation"] if key.startswith("submit_")] == []
        assert list(data["calculation"]) == list(expected)
        assert typed.canonical_bytes(data["calculation"]) == typed.canonical_bytes(expected)
        assert data["submit_arc"]["ok"] is False and data["submit_arc"]["status"] in ("disabled", "excluded")
    # Storage is read only: the stored final keeps its stale fields byte for byte.
    assert world.h.objects.objects[(job["final_ref"]["bucket"], job["final_ref"]["key"])]["Body"] == stored_bytes


# Stored final bodies that passed the storage binding checks (sha256, size, metadata) but are not
# one strict JSON object. The removed v1 snapshot composer refused the same table; /api/v2 parses the
# stored final with typed.parse_json in course_wiring._calculation_record.
MALFORMED_FINALS = [
    b"", b"{", b"{} trailing", b"\xff", b'{"text":"\xff"}',
    b'{"metric":1,"metric":2}', b'{"nested":{"metric":1,"metric":2}}',
    b'{"submit_arc":{"ok":true},"submit_arc":{"ok":false}}',
    b'{"n":NaN}', b'{"n":Infinity}', b'{"n":-Infinity}', b'{"n":1e400}', b'{"nested":[-1e400]}',
    ('{"secret":"%s","secret":"%s"}' % (SECRET, SECRET)).encode(), ('{"%s":NaN}' % SECRET).encode(),
]
NON_OBJECT_FINALS = [b"null", b"true", b"123", b'"object-looking string"', b"[]", ('["%s"]' % SECRET).encode()]


@pytest.mark.parametrize("body,code", [(body, "TEMPORARILY_UNAVAILABLE") for body in MALFORMED_FINALS]
                         + [(body, "STORED_INPUT_INVALID") for body in NON_OBJECT_FINALS])
def test_stored_final_that_is_not_one_json_object_is_a_fixed_503_without_details(
        world, monkeypatch, capsys, body, code):
    world.h.upload(world.owner.token, world.attempt_id, world.condition)
    assert world.h.work(world.attempt_id) is True
    storage = world.h.api.calculation.storage
    real = storage.read_final
    reads = []

    def stored(*args, **kwargs):
        reads.append(real(*args, **kwargs))  # the real binding checks still run first
        return body

    monkeypatch.setattr(storage, "read_final", stored)
    rows_before = world.h.store.rows()
    objects_before = deepcopy(world.h.objects.objects)
    seen = len(world.events.records)
    reply = call(world, upload_event(world, method="GET"))
    assert_error(reply, 503, code)
    assert set(reply.body) == {"success", "error", "timestamp"} and reply.error["details"] is None
    assert len(reads) == 1
    assert world.h.store.rows() == rows_before and world.h.objects.objects == objects_before
    assert writes(world) == []
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err + json.dumps(world.events.records[seen:])


def test_write_request_recording_sees_an_accepted_submission(world):
    """Positive control for writes(): an accepted POST is recorded as a transaction write."""
    reply = call(world, upload_event(world))
    assert reply.status == 202
    assert "TransactWriteItems" in writes(world)
    recorded = {operation for operation, _ in world.requests}
    assert recorded and recorded <= set(WRITE_OPERATIONS) | {"GetItem", "Query", "Scan", "TransactGetItems"}, recorded

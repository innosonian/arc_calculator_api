"""Real TCP → authenticated /api/v2 → real bundled calculator → real DynamoDB.

The API role is ``build_course_application`` over the Dummy Dev catalog with
the local course limits, the worker is ``build_worker`` with the bundled
InternalCalculator, and ``LocalJobRunner`` delivers durable wakes, exactly as
local_server.runtime assembles them. All numeric capacities, clock adjustments
and storage names below are explicit test settings, not deployment defaults.
Chart bytes persist in a test file store; external/local chart download is not
faked.
"""

import base64
import contextlib
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import http.client
import json
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from urllib.parse import quote, urlencode
import uuid

import boto3
from botocore.config import Config
import pytest

from http_pipeline_tests.support import FileObjects, FileLegacyBindings
from local_server.execution import LocalJobRunner
from local_server.http import make_application, create_server
from local_server.runtime import local_course_settings
from mock_journey import typed
from mock_journey.dev_course import validate_dummy_catalog
from mock_journey.execution_definitions import execution_catalog
from tests._synth import multipart_event
from tests.calculator_doubles import TrackedCalculator
from tests.journey_support import (
    HARNESS_LIMITS, HARNESS_LOCAL_ENVIRONMENT, HARNESS_LOCAL_KEYS, HARNESS_LOCAL_STAGE, JourneyStore, compose,
    dummy_course,
)
from tests.loopback_port_support import unused_loopback_port


DATA = Path(__file__).parents[1] / "tests/dataset"
WIRE_LIMIT = 1_000_000
COMPRESSION = dummy_course("mock-compression-only", "adult")
VENTILATION = dummy_course("mock-ventilation-only", "adult")
DUMMY_EXCLUDED = {"status": "excluded", "ok": False, "error": None, "exclusionReasons": ["dummy"]}


def create_table(client, name):
    client.create_table(
        TableName=name,
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": key, "AttributeType": kind} for key, kind in (
            ("PK", "S"), ("SK", "S"), ("GSI1PK", "S"), ("GSI1SK", "N"),
        )],
        GlobalSecondaryIndexes=[{"IndexName": "GSI1", "KeySchema": [
            {"AttributeName": "GSI1PK", "KeyType": "HASH"}, {"AttributeName": "GSI1SK", "KeyType": "RANGE"},
        ], "Projection": {"ProjectionType": "ALL"}}], BillingMode="PAY_PER_REQUEST",
    )


def world(client, table, files, allowed, *, now=None, calls=None):
    """API and Worker on the ``HARNESS_LOCAL_*`` values (tests/journey_support.compose) over a file store."""
    now = now if now is not None else [int(time.time())]
    calls = calls if calls is not None else []
    execution = execution_catalog()
    course_settings = local_course_settings()
    provider = validate_dummy_catalog(course_settings, execution=execution,
                                      artifact_bytes=HARNESS_LIMITS.artifact_bytes)
    objects = FileObjects(files)
    clock = lambda: now[0]  # noqa: E731
    composition = compose(
        JourneyStore(client, table), provider=provider, objects=objects, bindings=FileLegacyBindings(objects),
        keys=HARNESS_LOCAL_KEYS, environment=HARNESS_LOCAL_ENVIRONMENT, stage=HARNESS_LOCAL_STAGE,
        execution=execution, course_settings=course_settings, mapping_document=provider.mapping_document,
        clock=clock,
    )
    adapter = TrackedCalculator(stage=composition.stage,
                                before_calculate=lambda loaded, binding: calls.append(deepcopy(binding)))
    service = composition.build_api()
    worker = composition.build_worker([adapter])
    runner = LocalJobRunner(worker.jobs, worker, lease_seconds=60, retry_seconds=5, page_size=20, max_pages=5,
                            clock=clock)
    return SimpleNamespace(client=client, table=table, files=files, objects=objects, now=now, calls=calls,
                           service=service, worker=worker, runner=runner, allowed=allowed)


@pytest.fixture
def journey(dynamodb_client, tmp_path, loopback_network_guard):
    table = "arc_internal_http_" + uuid.uuid4().hex
    create_table(dynamodb_client, table)
    try:
        yield world(dynamodb_client, table, tmp_path / "objects", loopback_network_guard)
    finally:
        dynamodb_client.delete_table(TableName=table)


def unused_port(allowed):
    port = unused_loopback_port()
    allowed.add(("127.0.0.1", port))
    return port


@contextlib.contextmanager
def gateway(journey):
    port = unused_port(journey.allowed)
    app = make_application(
        journey.service,
        lambda: journey.client.describe_table(TableName=journey.table)["Table"]["TableStatus"] == "ACTIVE",
        "127.0.0.1", port, ["127.0.0.1"], calculation_body_limit=WIRE_LIMIT,
    )
    server = create_server(app, "127.0.0.1", port)
    thread = threading.Thread(target=server.run)
    thread.start()
    try:
        yield port
    finally:
        from waitress import wasyncore
        server.trigger.pull_trigger(lambda: wasyncore.close_all(map=server._map))
        thread.join(timeout=5)
        server.task_dispatcher.shutdown(timeout=3)
        assert not thread.is_alive()


def request(port, method, path, *, token=None, body=None, headers=None):
    fields = dict(headers or {})
    if token is not None:
        fields["Authorization"] = "Bearer " + token
    if type(body) is dict:
        fields["Content-Type"] = "application/json"
        body = json.dumps(body).encode()
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request(method, path, body=body, headers=fields)
        response = connection.getresponse()
        data = response.read()
        assert response.getheader("Cache-Control") == "no-store"
        return response.status, json.loads(data) if data else None
    finally:
        connection.close()


def data(result, status=200):
    code, body = result
    assert code == status, body
    assert body["success"] is True
    return body["data"]


def login(port):
    value = data(request(port, "POST", "/api/v2/sessions/", body={"loginId": "test@test.com", "password": "2222"}), 201)
    return value["accessToken"]


def course_detail(port, token, course=COMPRESSION):
    return data(request(port, "GET", f"/api/v2/courses/{course.course_id}/progress/?enrollmentId={course.enrollment_id}",
                        token=token))


def start(port, token, course=COMPRESSION):
    definition_hash = course_detail(port, token, course)["definitionHash"]
    return data(request(port, "POST", "/api/v2/attempts/", token=token, body={
        "clientRequestId": str(uuid.uuid4()), "courseId": course.course_id, "enrollmentId": course.enrollment_id,
        "courseItemLinkId": course.practice_link_id, "definitionHash": definition_hash,
    }), 201)


def calculation_path(attempt):
    return f'/api/v2/attempts/{attempt["attemptId"]}/calculation/'


def upload(port, token, attempt, *, mode="multipart", binary=None):
    binary = binary if binary is not None else (DATA / "cco_1.bin").read_bytes()
    fields = {"condition": json.dumps(attempt["condition"])}
    if mode == "multipart":
        event = multipart_event({"rawHexBPfile": binary, **fields})
        body = base64.b64decode(event["body"])
        headers = dict(event["headers"])
    else:
        fields["cpr_b64_data"] = base64.urlsafe_b64encode(binary).decode()
        body = base64.urlsafe_b64encode(quote(urlencode(fields), safe="").encode())
        headers = {} if mode == "base64-no-type" else {"Content-Type": "application/x-www-form-urlencoded"}
    assert len(body) > 16 * 1024  # Exercise the actual previously blocked payload size.
    return request(port, "POST", calculation_path(attempt), token=token, body=body, headers=headers)


def finish(journey, port, token, attempt):
    for _ in range(30):
        journey.runner.run_once()
        status, result = request(port, "GET", calculation_path(attempt), token=token)
        if status != 202:
            assert status == 200, result
            return result["data"]
        time.sleep(0.02)
    pytest.fail("The explicit local runner did not finish its accepted job.")


@pytest.mark.parametrize("mode", ["multipart", "base64", "base64-no-type"])
def test_http_login_binary_calculation_shared_completion_retry_and_next_course(journey, mode, monkeypatch):
    with gateway(journey) as port:
        token, peer = login(port), login(port)
        attempt = start(port, token)
        assert upload(port, token, attempt, mode=mode)[0] == 202
        result = finish(journey, port, token, attempt)
        assert result["submit_arc"] == DUMMY_EXCLUDED
        calculation = result["calculation"]
        assert "submit_hstm" not in calculation and "submit_arc" not in calculation
        # Compare the genuine network result with the preserved parser/core
        # regression boundary. Only the newly published chart URL is variable.
        from lambda_handler import _run_trusted_calculation
        monkeypatch.setenv("STAGE", "test")
        reference = _run_trusted_calculation(multipart_event({
            "rawHexBPfile": (DATA / "cco_1.bin").read_bytes(), "condition": json.dumps(attempt["condition"]),
        }), None)
        assert reference["statusCode"] == 200
        expected, actual = json.loads(reference["body"]), deepcopy(calculation)
        expected.pop("chart_dataset_url")
        actual.pop("chart_dataset_url")
        # The trusted Lambda response carries its own ARC overlay; /api/v2
        # reports submission separately (result["submit_arc"] above).
        assert expected.pop("submit_arc")["ok"] is False
        assert typed.canonical_bytes(actual) == typed.canonical_bytes(expected)
        assert result["evaluation"]["goal"]["observed"] == calculation["action_count"]["comp"]
        assert result["evaluation"]["program_completed"] is True
        assert data(request(port, "GET", f'/api/v2/attempts/{attempt["attemptId"]}/', token=token))["state"] == "evaluated"
        assert course_detail(port, peer)["courseItems"][0]["isCompleted"] is True
        before = sorted(journey.objects.keys())
        assert upload(port, token, attempt, mode=mode)[0] == 200
        reread = data(request(port, "GET", calculation_path(attempt), token=token))
        assert typed.canonical_bytes(reread) == typed.canonical_bytes(result)
        assert sorted(journey.objects.keys()) == before and len(journey.calls) == 1
        assert len([key for key in before if key.endswith(".request.json")]) == 1
        assert all(call[2] == 300 for call in journey.objects.sign_calls)
        following = start(port, token, VENTILATION)
        assert upload(port, token, following, binary=(DATA / "adult_vo_1.bin").read_bytes())[0] == 202
        assert finish(journey, port, token, following)["calculation"]["action_count"]["vent"] >= 8


def test_pending_http_job_survives_new_api_worker_instances_and_keeps_old_epoch_out_of_progress(journey):
    with gateway(journey) as port:
        token, peer = login(port), login(port)
        attempt = start(port, token)
        assert upload(port, token, attempt)[0] == 202
        assert request(port, "DELETE", "/api/v2/session/", token=peer)[0] == 204
    restored = world(journey.client, journey.table, journey.files, journey.allowed, now=journey.now, calls=journey.calls)
    with gateway(restored) as port:
        result = finish(restored, port, token, attempt)
        assert result["evaluation"]["program_completed"] is True
        assert result["progressApplication"]["reason"] == "PROGRESS_RESET"
        refreshed = data(request(port, "POST", "/api/v2/session/refresh/", token=token, body={}))
        assert refreshed["learningAvailability"] == {"state": "ready", "reason": None}
        assert course_detail(port, token)["courseItems"][0]["isCompleted"] is False
        assert len(restored.calls) == 1


def test_expired_session_can_reauthorize_same_attempt_and_submit_original_measurement(journey):
    with gateway(journey) as port:
        token = login(port)
        attempt = start(port, token)
        journey.now[0] += 86400
        status, error = upload(port, token, attempt)
        assert status == 401 and error["error"]["code"] == "SESSION_EXPIRED"
        fresh = login(port)
        assert request(port, "GET", f'/api/v2/attempts/{attempt["attemptId"]}/', token=fresh)[0] == 404
        reauthorized = data(request(port, "POST", f'/api/v2/attempts/{attempt["attemptId"]}/reauthorize/', token=fresh,
                                    body={"resumeCredential": attempt["resumeCredential"]}))
        assert reauthorized["attemptId"] == attempt["attemptId"] and reauthorized["state"] == "created"
        assert upload(port, fresh, attempt)[0] == 202
        assert finish(journey, port, fresh, attempt)["calculation"]["action_count"]["comp"] >= 60


def test_received_body_is_not_accepted_without_session_or_with_other_attempt_owner(journey):
    with gateway(journey) as port:
        owner, peer = login(port), login(port)
        attempt = start(port, owner)
        # Login/start stored the private course definition snapshots only.
        before = sorted(journey.objects.keys())
        status, error = upload(port, None, attempt)
        assert status == 401 and error["error"]["code"] == "SESSION_REQUIRED"
        # The existing ownership contract conceals another session's attempt.
        status, error = upload(port, peer, attempt)
        assert status == 404 and error["error"]["code"] == "NOT_FOUND"
        assert sorted(journey.objects.keys()) == before and not journey.calls
        assert data(request(port, "GET", f'/api/v2/attempts/{attempt["attemptId"]}/', token=owner))["state"] == "created"


def test_due_runner_continues_outside_request_and_can_be_supervised(journey):
    stop = threading.Event()
    errors = []

    def supervise():
        try:
            journey.runner.run(stop, poll_interval=0.02)
        except BaseException as error:
            errors.append(type(error).__name__)

    with gateway(journey) as port:
        token = login(port)
        attempt = start(port, token)
        assert upload(port, token, attempt)[0] == 202
        thread = threading.Thread(target=supervise)
        thread.start()
        try:
            for _ in range(100):
                status, result = request(port, "GET", calculation_path(attempt), token=token)
                if status != 202:
                    break
                time.sleep(0.02)
            assert status == 200, result
        finally:
            stop.set()
            thread.join(timeout=5)
        assert not thread.is_alive() and not errors and len(journey.calls) == 1


def test_concurrent_http_retries_commit_one_job_and_reject_changed_binary(journey):
    with gateway(journey) as port:
        token = login(port)
        attempt = start(port, token)
        barrier = threading.Barrier(2)

        def send():
            barrier.wait(timeout=5)
            return upload(port, token, attempt)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(send) for _ in range(2)]
            results = [future.result(timeout=10) for future in futures]
        assert [status for status, _ in results] == [202, 202]
        items = journey.client.scan(TableName=journey.table, ConsistentRead=True)["Items"]
        assert len([item for item in items if item["PK"]["S"].startswith("JOB#")]) == 1
        assert len([item for item in items if item["PK"]["S"].startswith("OUTBOX#")]) == 1
        changed = bytearray((DATA / "cco_1.bin").read_bytes())
        changed[1] ^= 1
        status, error = upload(port, token, attempt, binary=bytes(changed))
        assert status == 409 and error["error"]["code"] == "ATTEMPT_INPUT_CONFLICT"
        finish(journey, port, token, attempt)
        assert len(journey.calls) == 1


def test_job_and_raw_files_survive_actual_owned_database_process_restart(tmp_path, loopback_network_guard):
    from local_server.cli import verify_distribution, start_database, stop_database
    home_value = os.environ.get("ARC_TEST_DYNAMODB_HOME")
    if not home_value:
        raise pytest.UsageError("Run through scripts/validate_local_integration.py for owned DB restart verification.")
    home = verify_distribution(Path(home_value))
    root = tmp_path / "owned-restart"
    root.mkdir(mode=0o700)
    dbdir = root / "db"
    dbdir.mkdir(mode=0o700)
    port = unused_port(loopback_network_guard)
    endpoint = f"http://127.0.0.1:{port}"

    def client():
        return boto3.client("dynamodb", endpoint_url=endpoint, region_name="us-east-2",
                            aws_access_key_id="LocalTest", aws_secret_access_key="LocalTest",
                            config=Config(proxies={}, retries={"total_max_attempts": 1}, connect_timeout=2, read_timeout=5))

    child = start_database(home, dbdir, port, root)
    connection = client()
    try:
        table = "arc_restart_" + uuid.uuid4().hex
        create_table(connection, table)
        first = world(connection, table, root / "objects", loopback_network_guard)
        with gateway(first) as api:
            token = login(api)
            attempt = start(api, token)
            assert upload(api, token, attempt)[0] == 202
        connection.close()
        stop_database(child)
        child = start_database(home, dbdir, port, root)
        connection = client()
        restored = world(connection, table, root / "objects", loopback_network_guard, now=first.now, calls=first.calls)
        with gateway(restored) as api:
            assert finish(restored, api, token, attempt)["calculation"]["action_count"]["comp"] >= 60
            assert len(restored.calls) == 1
    finally:
        connection.close()
        stop_database(child)

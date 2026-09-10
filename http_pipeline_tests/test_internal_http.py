"""Real TCP → authenticated API → real bundled calculator → real DynamoDB.

All numeric capacities, clock adjustments and execution definitions below are
explicit test settings. They are not registered as deployment defaults. Chart
bytes persist in a test file store; external/local chart download is not faked.
"""

import base64
import contextlib
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import http.client
import json
import os
from pathlib import Path
import socket
import threading
import time
from types import SimpleNamespace
from urllib.parse import quote, urlencode
import uuid

import boto3
from botocore.config import Config
import pytest

from http_pipeline_tests.support import FileObjects, FileLegacyBindings
from integration_tests.test_mock_journey import LocalDefinitions
from local_server.execution import build_local_execution
from local_server.http import make_application, create_server
from mock_journey import typed
from mock_journey.assembly import ExecutionCatalog
from mock_journey.catalog import PROGRAMS, TARGETS, Catalog, slot_key
from mock_journey.internal_calculator import InternalCalculator
from mock_journey.projection import ProjectionSchema
from mock_journey.settings import StateSettings, StorageSettings, ApiSettings, WorkerSettings
from tests._synth import multipart_event


DATA = Path(__file__).parents[1] / "tests/dataset"
WIRE_LIMIT = 1_000_000


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
    now = now if now is not None else [int(time.time())]
    calls = calls if calls is not None else []
    definitions = {}
    for program, *_ in PROGRAMS:
        for target in TARGETS:
            value = LocalDefinitions().get_definition(program, target)
            value.update(adapter_version="internal-v1", projection_version="internal-projection-v1", profile_version="tester-v1")
            definitions[slot_key(program, target)] = value
    catalog = ExecutionCatalog(definitions, {"internal-projection-v1": ProjectionSchema("internal-projection-v1", {})})
    objects = FileObjects(files)
    legacy = FileLegacyBindings(objects)
    state = StateSettings(table, 4)
    storage = StorageSettings("local-validation", legacy.bucket, legacy.directory, 1_000_000, 8_000_000)
    adapter = InternalCalculator(version="internal-v1", projection_version="internal-projection-v1", stage=storage.stage)
    calculate = adapter.calculate

    def counted(loaded, binding, heartbeat):
        calls.append(deepcopy(binding))
        return calculate(loaded, binding, heartbeat)

    adapter.calculate = counted
    execution = build_local_execution(
        ApiSettings(state, storage, "local-validation", 2_000_000), WorkerSettings(state, storage, 60, 5),
        dynamodb_client=client, s3_client=objects, legacy_bindings=legacy, resume_keys={"test-v1": b"T" * 32},
        current_key_version="test-v1", execution=catalog, adapters=[adapter],
        relay_lease_seconds=60, relay_retry_seconds=5, page_size=20, max_pages=5, clock=lambda: now[0],
    )
    return SimpleNamespace(client=client, table=table, files=files, objects=objects, now=now, calls=calls,
                           execution=execution, allowed=allowed)


@pytest.fixture
def journey(dynamodb_client, tmp_path, loopback_network_guard):
    table = "arc_internal_http_" + uuid.uuid4().hex
    create_table(dynamodb_client, table)
    try:
        yield world(dynamodb_client, table, tmp_path / "objects", loopback_network_guard)
    finally:
        dynamodb_client.delete_table(TableName=table)


def unused_port(allowed):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    allowed.add(("127.0.0.1", port))
    return port


@contextlib.contextmanager
def gateway(journey):
    port = unused_port(journey.allowed)
    app = make_application(
        journey.execution.service,
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


def login(port):
    status, data = request(port, "POST", "/mock/v1/sessions", body={"login_id": "test@test.com", "password": "2222"})
    assert status == 201
    return data["session_token"]


def create(port, token, *, program="mock-compression-only", target="adult", request_id=None):
    status, attempt = request(port, "POST", "/mock/v1/attempts", token=token, body={
        "client_request_id": request_id or str(uuid.uuid4()), "catalog_version": Catalog.version,
        "program_id": program, "target": target,
    })
    assert status == 201, attempt
    return attempt


def upload(port, token, attempt, *, mode="multipart", binary=None, path="/cpr-analysis"):
    binary = binary if binary is not None else (DATA / "cco_1.bin").read_bytes()
    fields = {"condition": json.dumps(attempt["condition"])}
    if mode == "multipart":
        event = multipart_event({"rawHexBPfile": binary, **fields})
        body = base64.b64decode(event["body"])
        headers = event["headers"]
    else:
        fields["cpr_b64_data"] = base64.urlsafe_b64encode(binary).decode()
        body = base64.urlsafe_b64encode(quote(urlencode(fields), safe="").encode())
        headers = {} if mode == "base64-no-type" else {"Content-Type": "application/x-www-form-urlencoded"}
    assert len(body) > 16 * 1024  # Exercise the actual previously blocked payload size.
    headers["X-Attempt-ID"] = attempt["attempt_id"]
    return request(port, "POST", path, token=token, body=body, headers=headers)


def finish(journey, port, token, attempt):
    for _ in range(30):
        journey.execution.runner.run_once()
        status, result = request(port, "GET", attempt["calculation_path"], token=token)
        if status != 202:
            assert status == 200, result
            return result
        time.sleep(0.02)
    pytest.fail("The explicit local runner did not finish its accepted job.")


@pytest.mark.parametrize("mode", ["multipart", "base64", "base64-no-type"])
def test_http_login_binary_calculation_shared_completion_retry_and_next_program(journey, mode, monkeypatch):
    with gateway(journey) as port:
        token, peer = login(port), login(port)
        attempt = create(port, token)
        assert upload(port, token, attempt, mode=mode)[0] == 202
        result = finish(journey, port, token, attempt)
        assert result["submit_arc"] == {"status": "disabled", "ok": False, "error": "arc_contract_pending"}
        assert "submit_hstm" not in result
        # Compare the genuine network result with the preserved parser/core
        # regression boundary. Only the newly published chart URL is variable.
        from lambda_handler import _run_trusted_calculation
        monkeypatch.setenv("STAGE", "test")
        reference = _run_trusted_calculation(multipart_event({
            "rawHexBPfile": (DATA / "cco_1.bin").read_bytes(), "condition": json.dumps(attempt["condition"]),
        }), None)
        assert reference["statusCode"] == 200
        expected, actual = json.loads(reference["body"]), deepcopy(result)
        expected.pop("chart_dataset_url")
        actual.pop("chart_dataset_url")
        assert typed.canonical_bytes(actual) == typed.canonical_bytes(expected)
        status, evaluated = request(port, "GET", f'/mock/v1/attempts/{attempt["attempt_id"]}', token=token)
        assert status == 200
        assert evaluated["evaluation"]["goal"]["observed"] == result["action_count"]["comp"]
        assert evaluated["evaluation"]["program_completed"] is True
        programs = request(port, "GET", "/mock/v1/programs", token=peer)[1]["programs"]
        selected = next(item for item in programs if item["id"] == "mock-compression-only")
        assert selected["progress_by_target"]["adult"] == "completed"
        before = sorted(journey.objects.keys())
        assert upload(port, token, attempt, mode=mode, path=attempt["calculation_path"])[0] == 200
        reread = request(port, "GET", attempt["calculation_path"], token=token)
        assert typed.canonical_bytes(reread[1]) == typed.canonical_bytes(result)
        assert sorted(journey.objects.keys()) == before and len(journey.calls) == 1
        assert len([key for key in before if key.endswith(".request.json")]) == 1
        assert all(call[2] == 300 for call in journey.objects.sign_calls)
        following = create(port, token, program="mock-ventilation-only")
        assert upload(port, token, following, binary=(DATA / "adult_vo_1.bin").read_bytes())[0] == 202
        assert finish(journey, port, token, following)["action_count"]["vent"] >= 8


def test_pending_http_job_survives_new_api_worker_instances_and_keeps_old_epoch_out_of_progress(journey):
    with gateway(journey) as port:
        token, peer = login(port), login(port)
        attempt = create(port, token)
        assert upload(port, token, attempt)[0] == 202
        assert request(port, "DELETE", "/mock/v1/session", token=peer)[0] == 204
    restored = world(journey.client, journey.table, journey.files, journey.allowed, now=journey.now, calls=journey.calls)
    with gateway(restored) as port:
        finish(restored, port, token, attempt)
        evaluation = request(port, "GET", f'/mock/v1/attempts/{attempt["attempt_id"]}', token=token)[1]
        assert evaluation["evaluation"]["program_completed"] is True
        assert evaluation["progress_application"]["reason"] == "PROGRESS_RESET"
        programs = request(port, "GET", "/mock/v1/programs", token=token)[1]["programs"]
        selected = next(item for item in programs if item["id"] == "mock-compression-only")
        assert selected["progress_by_target"]["adult"] == "not_completed"
        assert len(restored.calls) == 1


def test_expired_session_can_reauthorize_same_attempt_and_submit_original_measurement(journey):
    with gateway(journey) as port:
        token = login(port)
        attempt = create(port, token)
        journey.now[0] += 86400
        assert upload(port, token, attempt)[0] == 401
        fresh = login(port)
        status, _ = request(port, "POST", f'/mock/v1/attempts/{attempt["attempt_id"]}/reauthorize', token=fresh,
                            body={"resume_credential": attempt["resume_credential"]})
        assert status == 200
        assert upload(port, fresh, attempt)[0] == 202
        assert finish(journey, port, fresh, attempt)["action_count"]["comp"] >= 60


def test_received_body_is_not_accepted_without_session_or_with_other_attempt_owner(journey):
    with gateway(journey) as port:
        owner, peer = login(port), login(port)
        attempt = create(port, owner)
        assert upload(port, None, attempt)[0] == 401
        # The existing ownership contract conceals another session's attempt.
        status, error = upload(port, peer, attempt)
        assert status == 404 and error["error"]["code"] == "NOT_FOUND"
        assert not journey.objects.keys() and not journey.calls


def test_due_runner_continues_outside_request_and_can_be_supervised(journey):
    stop = threading.Event()
    errors = []
    def supervise():
        try:
            journey.execution.runner.run(stop, poll_interval=0.02)
        except BaseException as error:
            errors.append(type(error).__name__)
    with gateway(journey) as port:
        token = login(port)
        attempt = create(port, token)
        assert upload(port, token, attempt)[0] == 202
        thread = threading.Thread(target=supervise)
        thread.start()
        try:
            for _ in range(100):
                status, result = request(port, "GET", attempt["calculation_path"], token=token)
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
        attempt = create(port, token)
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
            attempt = create(api, token)
            assert upload(api, token, attempt)[0] == 202
        connection.close()
        stop_database(child)
        child = start_database(home, dbdir, port, root)
        connection = client()
        restored = world(connection, table, root / "objects", loopback_network_guard, now=first.now, calls=first.calls)
        with gateway(restored) as api:
            assert finish(restored, api, token, attempt)["action_count"]["comp"] >= 60
            assert len(restored.calls) == 1
    finally:
        connection.close()
        stop_database(child)

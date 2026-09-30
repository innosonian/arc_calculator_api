"""L3 real default CLI smoke: owned DB, worker, private files and /api/v2 HTTP.

No service/storage/calculator is injected into the child. Fixtures are recorded
measurement files, not evidence of an iPad/physical manikin acceptance run.
"""

from contextlib import contextmanager
import hashlib
import json
import os
import signal
import time
import uuid

from boto3.dynamodb.types import TypeSerializer
import pytest

from local_server_tests.test_live_server import (
    DYNAMODB_HOME, PROGRAMS, STARTUP_SECONDS, TARGETS, LiveServer, dummy_course, error, objects_fingerprint, require,
)

# Real loopback sockets: opted out of the directory network guard (conftest.py).
pytestmark = pytest.mark.loopback


class JourneyServer(LiveServer):
    """The default CLI; kept as the name the journey acceptance files share."""


DUMMY_EXCLUDED = {"status": "excluded", "ok": False, "error": None, "exclusionReasons": ["dummy"]}


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
    require(health.get("calculator_available") is True and health.get("mode") == "course_v2",
            "Ready must reflect the actual owned worker and the /api/v2 scope.")
    token = server.token()
    attempt = server.start_attempt(token)
    require(attempt["state"] == "created" and attempt["role"] == "training"
            and attempt["condition"]["training_type"] == "compression_only", "Practice must pin its definition.")
    require(server.upload(token, attempt).status in (200, 202), "The first HTTP upload must be durably accepted.")
    response = server.result(token, attempt)
    result = response.data()
    require(result["calculationStatus"] == "succeeded", "The worker must commit a successful calculation.")
    require(result["submit_arc"] == DUMMY_EXCLUDED, "Local Dummy results must be excluded from ARC submission.")
    calculation = result["calculation"]
    require("submit_hstm" not in calculation and "submit_arc" not in calculation,
            "Submission state must stay outside the stored calculation.")
    require(calculation["action_count"]["comp"] >= 60, "Recorded compression data must reach the real calculator.")
    require(result["evaluation"]["program_completed"] is True, "Known Only target and real Pass must complete.")
    chart = server.chart(calculation["chart_dataset_url"])
    require(chart.status == 200 and type(chart.json()) in (dict, list), "The returned chart must actually download.")
    require(chart.headers.get("Cache-Control") == "no-store", "Chart response must preserve privacy headers.")
    link = server.request("GET", f'/api/v2/attempts/{attempt["attemptId"]}/chart-link/', token=token).data()
    require(set(link) == {"url", "expiresAt"} and server.chart(link["url"]).body == chart.body,
            "A re-issued chart link must serve the same stored chart.")
    view = server.attempt(token, attempt).data()
    require(view["state"] == "evaluated" and view["attemptId"] == attempt["attemptId"], "Attempt must be evaluated.")
    require(server.item(token, dummy_course())["isCompleted"] is True, "Completion must reach course progress.")
    files = objects_fingerprint(server)
    require(len(files) >= 6, "The product must persist raw input, candidates and final artifacts.")
    server.stop()
    server.start()
    committed = response.stable_body()
    require(server.calculation(token, attempt).stable_body() == committed,
            "A restart must preserve the committed calculation bytes.")
    require(server.chart(calculation["chart_dataset_url"]).body == chart.body,
            "The same unexpired chart capability must survive a restart.")
    require(server.upload(token, attempt).stable_body() == committed,
            "Matching input retry must return the committed result bytes.")
    require(objects_fingerprint(server) == files, "Restart/read/retry must not create another calculation artifact.")


def _serialized(row):
    serializer = TypeSerializer()
    return {key: serializer.serialize(value) for key, value in row.items()}


def owned_db_client(server):
    """(database module, fresh client) for this server's owned loopback DB child.

    The caller closes the client. Shared by the live files that read rows of
    the CLI's own DB; no write is issued by the readers.
    """
    from local_server import database as db
    return db, db._new_client(f"http://127.0.0.1:{server.db_port}")


@contextmanager
def _owned_database(data, db_port):
    """Start the verified DB distribution on this installation, as the CLI does."""
    from local_server.cli import start_database, stop_database, verify_distribution
    from local_server import database as db
    previous = os.umask(0o077)  # The CLI's private file mode for the DB child.
    try:
        material = db.prepare_material(data)
        child = start_database(verify_distribution(DYNAMODB_HOME), material.db_dir, db_port, data)
    finally:
        os.umask(previous)
    client = None
    try:
        client = db._new_client(f"http://127.0.0.1:{db_port}")
        yield material, client
    finally:
        if client is not None:
            client.close()
        stop_database(child)


def seed_installation(server, *, journey_schema):
    """Raw seed of an initialized installation with one active session (no CLI runs).

    With ``journey_schema`` the table is exactly the journey schema (PK/SK plus
    the GSI1 due index) that every installation has; otherwise it is the
    PK/SK-only table of the removed control-only CLI (D122: no such
    installation remains, so the server must refuse it). Both hold this
    installation's sentinel, the shared Dummy USER with its 15 program slots,
    one active SESSION and the initialized installation record. Returns that
    session's bearer token.
    """
    from local_server import database as db
    server.data.mkdir(mode=0o700)
    session_id, epoch, now = str(uuid.uuid4()), str(uuid.uuid4()), int(time.time())
    token = f"s1.{session_id}.{'Q' * 43}"
    with _owned_database(server.data, server.db_port) as (material, client):
        if journey_schema:
            client.create_table(TableName=db.TABLE_NAME, KeySchema=db._KEY_SCHEMA,
                                AttributeDefinitions=db._ATTRIBUTES + db._JOB_ATTRIBUTES,
                                GlobalSecondaryIndexes=[db._JOB_INDEX], BillingMode="PAY_PER_REQUEST")
        else:
            client.create_table(TableName=db.TABLE_NAME, KeySchema=db._KEY_SCHEMA,
                                AttributeDefinitions=db._ATTRIBUTES, BillingMode="PAY_PER_REQUEST")
        table = client.describe_table(TableName=db.TABLE_NAME)["Table"]
        require(len(table.get("GlobalSecondaryIndexes", [])) == (1 if journey_schema else 0),
                "Seed must create exactly the requested schema.")
        client.put_item(TableName=db.TABLE_NAME, Item=db._sentinel(material))
        slots = {f"{program}:{target}": {"completed": False, "completed_by_attempt": None,
                                         "completed_at": None, "open_attempts": 0}
                 for program, *_ in PROGRAMS for target in TARGETS}
        for row in (
            {"PK": "USER#dummy-tester", "SK": "STATE", "principal": "dummy-tester", "epoch": epoch,
             "revision": 0, "slots": slots, "updated_at": now},
            {"PK": f"SESSION#{session_id}", "SK": "AUTH", "session_id": session_id, "principal": "dummy-tester",
             "token_hash": hashlib.sha256(token.encode()).hexdigest(), "issued_at": now,
             "expires_at": now + 86400, "status": "active", "revision": 0},
        ):
            client.put_item(TableName=db.TABLE_NAME, Item=_serialized(row),
                            ConditionExpression="attribute_not_exists(PK)")
    record = json.loads((server.data / db.MATERIAL_FILENAME).read_text())
    record["database_initialized"] = True
    db._write_record(server.data / db.MATERIAL_FILENAME, record, replace=True)
    server.secrets.append(token)
    return token


def _table_and_rows(server):
    """Read-only description and consistent scan of the seeded table (sorted by key)."""
    db, client = owned_db_client(server)
    try:
        table = client.describe_table(TableName=db.TABLE_NAME)["Table"]
        rows = client.scan(TableName=db.TABLE_NAME, ConsistentRead=True)
        require("LastEvaluatedKey" not in rows, "The seeded table must fit one scan page.")
        indexes = [(index["IndexName"], index.get("IndexStatus")) for index in table.get("GlobalSecondaryIndexes", [])]
        return indexes, sorted(rows["Items"], key=lambda item: (item["PK"]["S"], item["SK"]["S"]))
    finally:
        client.close()


def test_existing_journey_installation_keeps_its_session_and_shared_epoch(tmp_path):
    server = JourneyServer(tmp_path / "existing")
    try:
        token = seed_installation(server, journey_schema=True)
        material_before = (server.data / "installation.json").read_bytes()
        server.start()
        session = server.request("GET", "/api/v2/session/", token=token).data()
        require(session["learningAvailability"] == {"state": "waiting", "reason": "arc_progress_unavailable"},
                "A pre-existing session has no course inventory until it refreshes.")
        require(server.refresh(token)["learningAvailability"] == {"state": "ready", "reason": None},
                "Starting on an existing installation must preserve the existing session.")
        require(len(server.courses(token)["results"]) == 15, "The existing installation must serve the Dummy courses.")
        attempt = server.start_attempt(token)
        require(server.upload(token, attempt).status in (200, 202), "The existing owned DB must accept real calculation.")
        require(server.result(token, attempt).status == 200, "The due index must support automatic worker execution.")
        require((server.data / "installation.json").read_bytes() == material_before,
                "Startup must not rewrite the installation key record.")
        fresh = server.token()
        require(server.item(fresh, dummy_course())["isCompleted"] is True,
                "The preserved shared epoch must show progress to a new Dummy session.")
        server.assert_private_logs()
    finally:
        server.stop()


def test_table_without_the_journey_schema_is_refused_without_any_write(tmp_path):
    # D122: the removed control-only schema (no GSI1) is neither served nor
    # migrated. The CLI fails at the DB connection phase, before any worker
    # or listener exists; the table, its rows and the key record are untouched.
    server = JourneyServer(tmp_path / "refused")
    try:
        seed_installation(server, journey_schema=False)
        material_before = (server.data / "installation.json").read_bytes()
        with _owned_database(server.data, server.db_port):
            before = _table_and_rows(server)
        require(before[0] == [] and len(before[1]) == 3, "Precondition: PK/SK-only table with three seeded rows.")
        server.launch()
        require(server.process.wait(timeout=STARTUP_SECONDS) == 1, "A foreign table schema must end the CLI with status 1.")
        server.stop()
        output = server.logs[0].read_text()
        require("Local server failed to connect the owned local database." in output,
                "The refusal must name the DB connection phase only.")
        require("ARC local course API ready" not in output, "A refused schema must not serve.")
        require((server.data / "installation.json").read_bytes() == material_before,
                "A refused schema must not rewrite the installation key record.")
        with _owned_database(server.data, server.db_port):
            require(_table_and_rows(server) == before, "A refused schema must receive no index or row write.")
    finally:
        server.stop()


def test_dead_owned_worker_stops_acceptance_and_restart_recovers(journey_cli):
    server = journey_cli
    token = server.token()
    attempt = server.start_attempt(token)
    worker = server.owned_worker_pid()
    os.kill(worker, signal.SIGTERM)
    require(server.process.wait(timeout=30) != 0, "An unexpectedly dead worker must cause a visible CLI failure.")
    server.stop()
    server.start()
    require(server.upload(token, attempt).status in (200, 202), "A restart must preserve the original session and attempt.")
    require(server.result(token, attempt).status == 200, "A newly supervised worker must process preserved state.")
    # D130: the completed training may be started again after the restart.
    require(server.start_attempt_reply(token, dummy_course()).status == 201,
            "A completed training must accept a new start after the restart (D130).")

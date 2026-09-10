"""Real local files + DynamoDB + internal worker; no default CLI/socket claim."""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

from botocore.exceptions import ClientError
import pytest

from integration_tests.test_local_completion_state import PendingDefinitions
from integration_tests.test_mock_journey import api, create, login, stored_attempt, submit
from local_server.database import prepare_material
from mock_journey.auth import AuthManager
from mock_journey.calculation import CalculationService
from mock_journey.catalog import Catalog
from mock_journey.contracts import CalculatorRegistry
from mock_journey.errors import JourneyError
from mock_journey.internal_calculator import InternalCalculator, PENDING_GOAL_ADAPTER_VERSION
from mock_journey.jobs import DynamoJobRepository
from mock_journey.projection import ProjectionSchema
from mock_journey.service import JourneyService
from mock_journey.state import DynamoStateRepository
from mock_journey.storage import JourneyStorage
from mock_journey.worker import JourneyWorker, call_binding, input_binding
from tests._synth import comp_session, cpr_session


ARTIFACT_LIMIT = 2_000_000
QUOTA_BYTES = 16_000_000
BUCKET = "local-filesystem-integration"
DIRECTORY = "calculator_result/interpreted_rtdata/arc"


def _open_world(db_client, table, material, now, clients, *, quota=QUOTA_BYTES):
    from local_server.charts import LocalChartService
    from local_server.object_storage import LocalLegacyBindings, LocalObjectClient, prepare_object_material

    objects = LocalObjectClient(prepare_object_material(material), bucket=BUCKET, directory=DIRECTORY,
                               stage="development", artifact_limit=ARTIFACT_LIMIT, quota_bytes=quota)
    clients.append(objects)
    charts = LocalChartService(objects, base_url="http://127.0.0.1:8000", clock=lambda: now[0])
    bindings = LocalLegacyBindings(objects, charts)
    storage = JourneyStorage(objects, legacy_bindings=bindings, stage="development",
                             limits={"input_bytes": 1_000_000, "artifact_bytes": ARTIFACT_LIMIT})
    state = DynamoStateRepository(db_client, table, clock=lambda: now[0])
    auth = AuthManager(state, material.environment, {material.key_version: material.resume_key},
                       material.key_version, clock=lambda: now[0])
    jobs = DynamoJobRepository(state)
    calculation = CalculationService(state, jobs, storage,
                                     {"test-projection": ProjectionSchema("test-projection", {})},
                                     payload_limit=2_000_000, clock=lambda: now[0])
    service = JourneyService(state, auth, Catalog(PendingDefinitions()), calculation)
    adapter = InternalCalculator(version=PENDING_GOAL_ADAPTER_VERSION, projection_version="test-projection",
                                 stage="development", allow_pending_cycle_goal=True)
    worker = JourneyWorker(jobs, storage, CalculatorRegistry([adapter]), lease_seconds=60,
                           retry_seconds=5, clock=lambda: now[0])
    return SimpleNamespace(objects=objects, charts=charts, storage=storage, state=state, auth=auth,
                           jobs=jobs, service=service, adapter=adapter, worker=worker, now=now,
                           material=material, db_client=db_client, table=table, clients=clients)


@pytest.fixture
def files_journey(dynamodb_client, dynamodb_table, tmp_path):
    directory = tmp_path.resolve() / "private-installation"
    directory.mkdir(mode=0o700)
    material = prepare_material(directory)
    clients = []
    world = _open_world(dynamodb_client, dynamodb_table, material, [1_800_000_000], clients)
    try:
        yield world
    finally:
        for client in reversed(clients):
            client.close()


def reopen(world, *, quota=QUOTA_BYTES):
    world.objects.close()
    world.clients.remove(world.objects)
    material = prepare_material(world.material.data_dir)
    assert material == world.material
    return _open_world(world.db_client, world.table, material, world.now, world.clients, quota=quota)


def body(world, key):
    response = world.objects.get_object(Bucket=BUCKET, Key=key)
    stream = response["Body"]
    try:
        value = stream.read(ARTIFACT_LIMIT + 1)
    finally:
        stream.close()
    assert len(value) == response["ContentLength"] <= ARTIFACT_LIMIT
    return value


def chart_bytes(world, url):
    parts = urlsplit(url)
    assert parts.scheme == "http" and parts.netloc == "127.0.0.1:8000" and not parts.query and not parts.fragment
    return world.charts.read_path(parts.path)


def corrupt_file(world, key):
    ident = world.objects._key(BUCKET, key)
    path = world.objects.material.object_dir / (ident + ".object")
    original = path.read_bytes()
    assert original
    path.write_bytes(original[:-1] + bytes((original[-1] ^ 1,)))
    return path, original


def result(world, token, attempt):
    return api(world, "GET", "attempts/" + attempt["attempt_id"] + "/calculation", token=token)


def accepted(world, *, program="mock-cpr", data=None):
    token = login(world)
    attempt = create(world, token, program=program)
    measurement = data if data is not None else cpr_session([(30, 2)] * 3)
    assert submit(world, token, attempt, data=measurement)["statusCode"] == 202
    saved = stored_attempt(world, token, attempt)
    return token, attempt, saved["job_id"], measurement


@pytest.mark.parametrize("program,data,completed,status", [
    ("mock-cpr", cpr_session([(30, 2)] * 3), False, "pending_policy"),
    ("mock-compression-only", comp_session(60), True, "evaluated"),
], ids=("cpr-pending", "only-completed"))
def test_real_file_pipeline_reopens_without_recalculation(files_journey, program, data, completed, status, monkeypatch):
    world = files_journey
    token, attempt, job_id, measurement = accepted(world, program=program, data=data)
    assert world.worker.process(job_id)
    job = world.jobs.get_job(job_id)
    loaded = world.storage.load_input(job["input_manifest_ref"], input_binding(job))
    assert loaded.projected.cpr_bytes == body(world, loaded.raw_base + ".bin") == measurement
    assert json.loads(body(world, loaded.raw_base + ".request.json"))["raw_base"] == loaded.raw_base
    assert type(json.loads(body(world, loaded.raw_base + ".meta.json"))) is dict
    raw = world.storage.load_calculation(job["planned_candidate_ref"], call_binding(job))
    assert json.loads(raw)["goal"]["status"] == status
    publication = job["chart_publication"]
    published = body(world, publication["key"])
    assert hashlib.sha256(published).hexdigest() == publication["published_body_sha256"]
    final = world.storage.read_final(job["final_ref"], call_binding(job), publication)
    assert "submit_arc" not in json.loads(final)
    response = result(world, token, attempt)
    assert response["statusCode"] == 200
    calculation = json.loads(response["body"])
    assert calculation["submit_arc"] == {"status": "disabled", "ok": False, "error": "arc_contract_pending"}
    url = calculation["chart_dataset_url"]
    assert chart_bytes(world, url) == published
    saved = stored_attempt(world, token, attempt)
    assert saved["evaluation"]["program_completed"] is completed
    assert saved["evaluation"]["goal"]["status"] == status
    progress = world.state.get_progress(world.auth.authenticate(token))
    before = {str(path.relative_to(world.material.data_dir)): hashlib.sha256(path.read_bytes()).hexdigest()
              for path in world.material.data_dir.rglob("*") if path.is_file()}
    restarted = reopen(world)
    monkeypatch.setattr(restarted.adapter, "calculate", lambda *a, **k: pytest.fail("A committed file result was recalculated."))
    assert restarted.worker.process(job_id)
    assert result(restarted, token, attempt) == response
    assert submit(restarted, token, attempt, data=measurement) == response
    assert chart_bytes(restarted, url) == published
    assert stored_attempt(restarted, token, attempt) == saved
    assert restarted.state.get_progress(restarted.auth.authenticate(token)) == progress
    after = {str(path.relative_to(restarted.material.data_dir)): hashlib.sha256(path.read_bytes()).hexdigest()
             for path in restarted.material.data_dir.rglob("*") if path.is_file()}
    assert after == before


def test_link_expiry_refresh_and_logout_do_not_rewrite_the_calculation(files_journey):
    world = files_journey
    token, attempt, job_id, _ = accepted(world)
    assert world.worker.process(job_id)
    response = result(world, token, attempt)
    original_url = json.loads(response["body"])["chart_dataset_url"]
    published = chart_bytes(world, original_url)
    world.now[0] += 299
    assert chart_bytes(world, original_url) == published
    world.now[0] += 1
    with pytest.raises(JourneyError) as expired:
        chart_bytes(world, original_url)
    assert expired.value.code == "NOT_FOUND"
    refreshed = api(world, "GET", "attempts/" + attempt["attempt_id"] + "/chart-link", token=token)
    assert refreshed["statusCode"] == 200
    fresh_url = json.loads(refreshed["body"])["chart_dataset_url"]
    assert fresh_url != original_url and chart_bytes(world, fresh_url) == published
    assert result(world, token, attempt) == response
    assert api(world, "DELETE", "session", token=token)["statusCode"] == 204
    assert chart_bytes(world, fresh_url) == published
    assert api(world, "GET", "attempts/" + attempt["attempt_id"] + "/chart-link", token=token)["statusCode"] == 403
    restarted = reopen(world)
    assert chart_bytes(restarted, fresh_url) == published
    restarted.now[0] += 300
    with pytest.raises(JourneyError) as expired:
        chart_bytes(restarted, fresh_url)
    assert expired.value.code == "NOT_FOUND"


def test_full_quota_preserves_committed_reads_retries_and_existing_chart(files_journey):
    world = files_journey
    token, attempt, job_id, measurement = accepted(world)
    assert world.worker.process(job_id)
    response = result(world, token, attempt)
    url = json.loads(response["body"])["chart_dataset_url"]
    published = chart_bytes(world, url)
    # Lower only total quota; an artifact read must retain its original bound.
    restarted = reopen(world, quota=1)
    assert result(restarted, token, attempt) == response
    assert submit(restarted, token, attempt, data=measurement) == response
    assert chart_bytes(restarted, url) == published
    ref = restarted.jobs.get_job(job_id)["planned_candidate_ref"]
    old = restarted.objects.get_object(Bucket=BUCKET, Key=ref["key"])
    stream = old["Body"]
    try:
        raw = stream.read(ARTIFACT_LIMIT + 1)
    finally:
        stream.close()
    restarted.objects.put_object(Bucket=BUCKET, Key=ref["key"], Body=raw, Metadata=old["Metadata"])
    new = create(restarted, token, request_id="after-full")
    denied = submit(restarted, token, new, data=measurement)
    assert denied["statusCode"] == 503
    assert stored_attempt(restarted, token, new)["state"] == "created"
    assert result(restarted, token, attempt) == response


def test_selected_candidate_recovers_using_reopened_real_files(files_journey, monkeypatch):
    world = files_journey
    token, attempt, job_id, _ = accepted(world)

    def interrupted(*args, **kwargs):
        raise JourneyError("TEMPORARILY_UNAVAILABLE")

    monkeypatch.setattr(world.jobs, "mark_calculation_saved", interrupted)
    assert not world.worker.process(job_id)
    old = world.jobs.get_job(job_id)
    assert world.storage.load_calculation(old["planned_candidate_ref"], call_binding(old)) is not None
    restarted = reopen(world)
    restarted.now[0] = old["next_due_at"]
    monkeypatch.setattr(restarted.adapter, "calculate", lambda *a, **k: pytest.fail("A durable file candidate was recalculated."))
    assert restarted.worker.process(job_id)
    assert restarted.jobs.get_job(job_id)["call_id"] == old["call_id"]
    response = result(restarted, token, attempt)
    assert response["statusCode"] == 200
    assert chart_bytes(restarted, json.loads(response["body"])["chart_dataset_url"])
    assert stored_attempt(restarted, token, attempt)["evaluation"]["goal"]["status"] == "pending_policy"


def test_reusing_selected_chart_after_final_commit_interruption_is_idempotent(files_journey, monkeypatch):
    world = files_journey
    token, attempt, job_id, _ = accepted(world)

    def interrupted(*args, **kwargs):
        raise JourneyError("TEMPORARILY_UNAVAILABLE")

    monkeypatch.setattr(world.jobs, "finalize", interrupted)
    assert not world.worker.process(job_id)
    old = world.jobs.get_job(job_id)
    assert old["chart_snapshot"]["kind"] == "snapshot"
    raw_base = world.storage.load_input(old["input_manifest_ref"], input_binding(old)).raw_base
    published = body(world, raw_base + ".json")
    restarted = reopen(world)
    restarted.now[0] = old["next_due_at"]
    monkeypatch.setattr(restarted.adapter, "calculate", lambda *a, **k: pytest.fail("A selected chart forced recalculation."))
    assert restarted.worker.process(job_id)
    current = restarted.jobs.get_job(job_id)
    assert current["chart_snapshot"] == old["chart_snapshot"]
    assert body(restarted, current["chart_publication"]["key"]) == published
    assert result(restarted, token, attempt)["statusCode"] == 200


def test_conflicting_write_cannot_replace_committed_result_or_chart(files_journey):
    world = files_journey
    token, attempt, job_id, _ = accepted(world, program="mock-compression-only", data=comp_session(60))
    assert world.worker.process(job_id)
    job = world.jobs.get_job(job_id)
    response = result(world, token, attempt)
    saved_progress = world.state.get_progress(world.auth.authenticate(token))
    for key in (job["final_ref"]["key"], job["chart_publication"]["key"]):
        original = world.objects.get_object(Bucket=BUCKET, Key=key)
        stream = original["Body"]
        try:
            content = stream.read(ARTIFACT_LIMIT + 1)
        finally:
            stream.close()
        with pytest.raises(ClientError) as caught:
            world.objects.put_object(Bucket=BUCKET, Key=key, Body=content + b" ", Metadata=original["Metadata"])
        assert caught.value.response["Error"]["Code"] == "LocalStorageUnavailable"
        assert body(world, key) == content
    assert result(world, token, attempt) == response
    assert world.state.get_progress(world.auth.authenticate(token)) == saved_progress


@pytest.mark.parametrize("kind", ["final", "chart", "raw"])
def test_file_corruption_fails_closed_without_rewriting_committed_progress(files_journey, kind, monkeypatch):
    world = files_journey
    token, attempt, job_id, _ = accepted(world, program="mock-compression-only", data=comp_session(60))
    assert world.worker.process(job_id)
    saved_attempt = stored_attempt(world, token, attempt)
    saved_progress = world.state.get_progress(world.auth.authenticate(token))
    job = world.jobs.get_job(job_id)
    loaded = world.storage.load_input(job["input_manifest_ref"], input_binding(job))
    response = result(world, token, attempt)
    url = json.loads(response["body"])["chart_dataset_url"]
    key = {"final": job["final_ref"]["key"], "chart": job["chart_publication"]["key"],
           "raw": loaded.raw_base + ".bin"}[kind]
    path, original = corrupt_file(world, key)
    monkeypatch.setattr(world.adapter, "calculate", lambda *a, **k: pytest.fail("Corrupt committed data triggered recalculation."))
    if kind == "final":
        failed = result(world, token, attempt)
        assert failed["statusCode"] == 503
        assert json.loads(failed["body"])["error"]["code"] == "TEMPORARILY_UNAVAILABLE"
    elif kind == "chart":
        with pytest.raises(JourneyError) as denied:
            chart_bytes(world, url)
        assert denied.value.code == "NOT_FOUND"
        assert api(world, "GET", "attempts/" + attempt["attempt_id"] + "/chart-link", token=token)["statusCode"] == 503
        assert result(world, token, attempt) == response
    else:
        with pytest.raises(JourneyError) as invalid:
            world.storage.load_input(job["input_manifest_ref"], input_binding(job))
        assert invalid.value.code == "TEMPORARILY_UNAVAILABLE"
        assert result(world, token, attempt) == response
    assert world.worker.process(job_id)
    assert stored_attempt(world, token, attempt) == saved_attempt
    assert world.state.get_progress(world.auth.authenticate(token)) == saved_progress
    # A test-only exact restoration demonstrates that no automatic replacement
    # calculation or new progress was required to recover the original bytes.
    path.write_bytes(original)
    assert result(world, token, attempt) == response
    assert chart_bytes(world, url)


def test_corrupt_saved_candidate_is_not_treated_as_absent_and_recalculated(files_journey, monkeypatch):
    world = files_journey
    token, attempt, job_id, _ = accepted(world)
    mark = world.jobs.mark_calculation_saved

    def interrupted(*args, **kwargs):
        raise JourneyError("TEMPORARILY_UNAVAILABLE")

    monkeypatch.setattr(world.jobs, "mark_calculation_saved", interrupted)
    assert not world.worker.process(job_id)
    old = world.jobs.get_job(job_id)
    path, original = corrupt_file(world, old["planned_candidate_ref"]["key"])
    monkeypatch.setattr(world.jobs, "mark_calculation_saved", mark)
    monkeypatch.setattr(world.adapter, "calculate", lambda *a, **k: pytest.fail("Corrupt candidate was treated as missing input."))
    world.now[0] = old["next_due_at"]
    assert not world.worker.process(job_id)
    blocked = world.jobs.get_job(job_id)
    assert blocked["call_id"] == old["call_id"] and blocked["planned_candidate_ref"] == old["planned_candidate_ref"]
    assert stored_attempt(world, token, attempt)["evaluation"] is None
    assert world.state.get_progress(world.auth.authenticate(token))["slots"]["mock-cpr:adult"]["completed"] is False
    path.write_bytes(original)
    world.now[0] = blocked["next_due_at"]
    assert world.worker.process(job_id)
    assert result(world, token, attempt)["statusCode"] == 200


def test_product_storage_does_not_import_test_storage_or_global_sdk_clients():
    import ast

    root = Path(__file__).parents[1]
    for name in ("object_storage.py", "charts.py"):
        source = (root / "local_server" / name).read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(not item.name.startswith(("tests", "integration_tests", "http_pipeline_tests", "boto3")) for item in node.names)
            elif isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith(("tests", "integration_tests", "http_pipeline_tests", "boto3"))
        assert "MemoryLegacyBindings" not in source and "FileObjects" not in source

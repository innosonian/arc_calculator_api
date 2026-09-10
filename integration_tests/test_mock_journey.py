"""Real DDB and stored-calculation fault boundaries, with a test calculator."""

import base64
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from mock_journey.auth import AuthManager
from mock_journey.calculation import CalculationService
from mock_journey.catalog import Catalog, PROGRAMS, TARGETS
from mock_journey.contracts import CalculatorRegistry, VerifiedCalculation, VerifiedChart
from mock_journey.errors import JourneyError
from mock_journey.handler import handle
from mock_journey.jobs import DynamoJobRepository
from mock_journey.projection import ProjectionSchema
from mock_journey.service import JourneyService
from mock_journey.state import DynamoStateRepository
from mock_journey.storage import JourneyStorage
from mock_journey.worker import JourneyWorker, call_binding
from tests.mock_storage_support import MemoryS3, MemoryLegacyBindings


MEASUREMENT = (Path(__file__).parents[1] / "tests/dataset/cpr_1.bin").read_bytes()


class LocalDefinitions:
    def get_definition(self, program_id, target):
        training = {"mock-compression-only": "compression_only", "mock-ventilation-only": "ventilation_only"}.get(program_id, "cpr")
        return {"condition": {
            "mode": "training", "target": target, "training_type": training,
            "guideline": "ARC2025", "cpr_cycle_type": "152" if target == "infant" else "302",
            "is_2rescuers": program_id in ("mock-two-rescuer-cpr", "mock-two-rescuer-aed"),
        }, "calculation_profile": {}, "profile_version": "test-profile", "adapter_version": "test-adapter",
            "projection_version": "test-projection"}


class LocalCalculator:
    """Only tests inject this adapter; values test state, not actual scoring."""
    version = "test-adapter"
    projection_version = "test-projection"

    def __init__(self):
        self.calls = []
        self.responses = {}
        self.overall = 90
        self.observed = None
        self.chart_kind = "no_chart"
        self.chart_calls = 0
        self.timeout = False
        self.on_calculate = lambda: None

    def calculate(self, loaded, binding, heartbeat):
        projected = loaded.projected
        assert projected.cpr_bytes == MEASUREMENT
        self.calls.append(deepcopy(binding))
        raw = json.dumps({"binding": binding, "score": self.overall,
                          "observed": projected.payload["definition"]["goal"]["required"] if self.observed is None else self.observed}).encode()
        self.responses[binding["call_id"]] = raw
        self.on_calculate()
        if self.timeout:
            raise TimeoutError("PRIVATE-CALCULATION-MARKER")
        return raw

    def validate_response(self, raw, projected, binding):
        value = json.loads(raw)
        if value["binding"] != binding:
            raise JourneyError("CALCULATOR_CONTRACT_MISMATCH")
        core = {"cpr_score": {"total_score": {"overall": value["score"], "score_rescue_vent": None}},
                "metrics": {"VentilationSpeed": {"%_Good": 100}}, "action_count": {"comp": 90, "vent": 6},
                "training_stats": {"cycle_count": 3, "elapsed_seconds": 90}, "guide_prompts": ["test-only coaching"],
                "type_preservation": [80, 80.0, None, False, "80"]}
        return VerifiedCalculation(core, projected.payload["definition"]["goal"]["kind"], value["observed"],
                                   self.chart_kind, "test-bound-chart" if self.chart_kind == "snapshot" else None)

    def get_chart(self, verified, binding, heartbeat):
        self.chart_calls += 1
        data = {"generation": self.chart_calls, "values": [1, 1.0, None]}
        return VerifiedChart(data, hashlib.sha256(json.dumps(data).encode()).hexdigest())


@pytest.fixture
def journey(dynamodb_client, dynamodb_table):
    now = [1000]
    state = DynamoStateRepository(dynamodb_client, dynamodb_table, clock=lambda: now[0])
    auth = AuthManager(state, "local-integration", {"v1": b"R" * 32}, "v1", clock=lambda: now[0])
    jobs = DynamoJobRepository(state)
    objects = MemoryS3()
    storage = JourneyStorage(objects, legacy_bindings=MemoryLegacyBindings(objects), stage="development",
                             limits={"input_bytes": 1000000, "artifact_bytes": 2000000})
    calculation = CalculationService(state, jobs, storage, {"test-projection": ProjectionSchema("test-projection", {})},
                                     payload_limit=2000000, clock=lambda: now[0])
    service = JourneyService(state, auth, Catalog(LocalDefinitions()), calculation)
    adapter = LocalCalculator()
    worker = JourneyWorker(jobs, storage, CalculatorRegistry([adapter]), lease_seconds=60, retry_seconds=5, clock=lambda: now[0])
    return SimpleNamespace(state=state, auth=auth, jobs=jobs, objects=objects, storage=storage, service=service,
                           adapter=adapter, worker=worker, now=now)


def api(journey, method, path, body=None, token=None, *, measurement=False):
    event = {"httpMethod": method, "path": "/mock/v1/" + path, "headers": {}}
    if token:
        event["headers"]["Authorization"] = "Bearer " + token
    if body is not None:
        event["body"] = body if type(body) is str else json.dumps(body)
    if measurement:
        event.update(isBase64Encoded=True)
        event["headers"]["Content-Type"] = "multipart/form-data; boundary=arc-integration-boundary"
    return handle(event, SimpleNamespace(aws_request_id="integration-request"), journey.service)


def login(journey):
    result = api(journey, "POST", "sessions", {"login_id": "test@test.com", "password": "2222"})
    assert result["statusCode"] == 201
    return json.loads(result["body"])["session_token"]


def create(journey, token, *, program="mock-cpr", target="adult", request_id="first"):
    result = api(journey, "POST", "attempts", {"client_request_id": request_id, "catalog_version": "mock-catalog-v1",
                                              "program_id": program, "target": target}, token)
    assert result["statusCode"] == 201, result
    return json.loads(result["body"])


def multipart(condition, *, extra=None, data=MEASUREMENT):
    parts = [("rawHexBPfile", data), ("condition", json.dumps(condition).encode())]
    parts += [(name, json.dumps(value).encode()) for name, value in (extra or {}).items()]
    body = b"".join(b'--arc-integration-boundary\r\nContent-Disposition: form-data; name="' + name.encode() +
                    b'"\r\n\r\n' + value + b"\r\n" for name, value in parts) + b"--arc-integration-boundary--\r\n"
    return base64.b64encode(body).decode()


def submit(journey, token, attempt, **kwargs):
    return api(journey, "POST", "attempts/" + attempt["attempt_id"] + "/calculation",
               multipart(attempt["condition"], **kwargs), token, measurement=True)


def stored_attempt(journey, token, attempt):
    return journey.state.get_attempt(journey.auth.authenticate(token), attempt["attempt_id"])


@pytest.mark.parametrize("program,target", [(program[0], target) for program in PROGRAMS for target in TARGETS])
def test_catalog_slot_accept_result_and_completion_contract_with_test_adapter(journey, program, target):
    token = login(journey)
    attempt = create(journey, token, program=program, target=target)
    accepted = submit(journey, token, attempt)
    assert accepted["statusCode"] == 202 and json.loads(accepted["body"])["wait_expired"] is False
    saved = stored_attempt(journey, token, attempt)
    assert journey.worker.process(saved["job_id"]) is True
    assert journey.worker.process(saved["job_id"]) is True
    assert len(journey.adapter.calls) == 1
    result = api(journey, "GET", "attempts/" + attempt["attempt_id"] + "/calculation", token=token)
    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    assert [type(value) for value in body["type_preservation"]] == [int, float, type(None), bool, str]
    assert "submit_hstm" not in body
    assert body["submit_arc"] == {"status": "disabled", "ok": False, "error": "arc_contract_pending"}
    assert "evaluation" not in body and "hstm_document" not in body
    evaluated = stored_attempt(journey, token, attempt)
    assert evaluated["evaluation"]["program_completed"] is True
    assert evaluated["progress_application"]["reason"] == "APPLIED"
    replay = submit(journey, token, attempt, extra={"access_token": "PRIVATE-TOKEN-MARKER"})
    assert replay == result
    assert not any(b"PRIVATE-TOKEN-MARKER" in value["Body"] for value in journey.objects.objects.values())
    assert len(journey.adapter.calls) == 1
    denied = api(journey, "POST", "attempts", {"client_request_id": "later", "catalog_version": "mock-catalog-v1",
                                             "program_id": program, "target": target}, token)
    assert denied["statusCode"] == 409


def test_response_certification_override_cannot_grant_shared_completion(journey):
    journey.adapter.overall = 70
    token = login(journey)
    attempt = create(journey, token)
    assert submit(journey, token, attempt, extra={"Custom": {"CertificateAdult": True}, "Open_Skill": {"Passing_Score": 0}})["statusCode"] == 202
    saved = stored_attempt(journey, token, attempt)
    assert journey.worker.process(saved["job_id"])
    status = stored_attempt(journey, token, attempt)
    assert status["evaluation"]["program_completed"] is False
    assert status["evaluation"]["reason_codes"] == ["SCORE_NOT_PASS"]
    result = api(journey, "GET", "attempts/" + attempt["attempt_id"] + "/calculation", token=token)
    assert json.loads(result["body"])["certification"]["Target"] == "adult"


def test_late_result_after_other_device_logout_is_stored_without_new_progress(journey):
    token, other = login(journey), login(journey)
    attempt = create(journey, token)
    assert submit(journey, token, attempt)["statusCode"] == 202
    assert api(journey, "DELETE", "session", token=other)["statusCode"] == 204
    journey.adapter.on_calculate = lambda: journey.now.__setitem__(0, journey.now[0] + 31)
    saved = stored_attempt(journey, token, attempt)
    assert journey.worker.process(saved["job_id"])
    result = stored_attempt(journey, token, attempt)
    assert result["evaluation"]["program_completed"] is True
    assert result["progress_application"] == {"applied": False, "applied_epoch": None, "reason": "PROGRESS_RESET"}
    assert api(journey, "GET", "attempts/" + attempt["attempt_id"] + "/calculation", token=token)["statusCode"] == 200


def test_saved_candidate_recovers_after_reference_commit_failure_without_recalculation(journey, monkeypatch):
    token = login(journey)
    attempt = create(journey, token)
    assert submit(journey, token, attempt)["statusCode"] == 202
    saved = stored_attempt(journey, token, attempt)
    original = journey.jobs.mark_calculation_saved
    def interrupted(*args, **kwargs):
        raise JourneyError("TEMPORARILY_UNAVAILABLE")
    monkeypatch.setattr(journey.jobs, "mark_calculation_saved", interrupted)
    assert journey.worker.process(saved["job_id"]) is False
    processing = stored_attempt(journey, token, attempt)
    assert processing["state"] == "processing" and processing["evaluation"] is None
    response = api(journey, "GET", "attempts/" + attempt["attempt_id"] + "/calculation", token=token)
    assert response["statusCode"] == 202
    job = journey.jobs.get_job(saved["job_id"])
    assert journey.storage.load_calculation(job["planned_candidate_ref"], call_binding(job)) is not None
    monkeypatch.setattr(journey.jobs, "mark_calculation_saved", original)
    journey.now[0] = job["next_due_at"]
    assert journey.worker.process(saved["job_id"])
    result = stored_attempt(journey, token, attempt)
    assert result["state"] == "evaluated" and "error_code" not in result
    assert result["evaluation"]["program_completed"] is True
    assert len(journey.adapter.calls) == 1


def test_missing_candidate_recalculates_under_new_call_and_old_late_write_is_isolated(journey):
    journey.adapter.timeout = True
    token = login(journey)
    attempt = create(journey, token)
    assert submit(journey, token, attempt)["statusCode"] == 202
    saved = stored_attempt(journey, token, attempt)
    assert journey.worker.process(saved["job_id"]) is False
    old = journey.jobs.get_job(saved["job_id"])
    assert old["call_phase"] == "started" and old["candidate_ref"] is None
    assert journey.storage.load_calculation(old["planned_candidate_ref"], call_binding(old)) is None
    journey.now[0] = old["next_due_at"]
    journey.adapter.timeout = False
    journey.adapter.overall = 91
    assert journey.worker.process(saved["job_id"])
    current = journey.jobs.get_job(saved["job_id"])
    assert current["call_id"] != old["call_id"]
    assert current["planned_candidate_ref"] != old["planned_candidate_ref"]
    assert current["execution_fence"] > old["execution_fence"]
    assert len(journey.adapter.calls) == 2
    original_result = api(journey, "GET", "attempts/" + attempt["attempt_id"] + "/calculation", token=token)
    assert json.loads(original_result["body"])["cpr_score"]["total_score"]["overall"] == 91
    journey.storage.save_calculation(old["planned_candidate_ref"], journey.adapter.responses[old["call_id"]], call_binding(old))
    assert api(journey, "GET", "attempts/" + attempt["attempt_id"] + "/calculation", token=token) == original_result
    assert journey.worker.process(saved["job_id"])
    assert len(journey.adapter.calls) == 2


def test_chart_selection_survives_final_commit_failure_and_late_writers(journey, monkeypatch):
    journey.adapter.chart_kind = "snapshot"
    token = login(journey)
    attempt = create(journey, token)
    assert submit(journey, token, attempt)["statusCode"] == 202
    saved = stored_attempt(journey, token, attempt)
    original_finalize = journey.jobs.finalize
    def fail_commit(*args, **kwargs):
        raise JourneyError("TEMPORARILY_UNAVAILABLE")
    monkeypatch.setattr(journey.jobs, "finalize", fail_commit)
    assert journey.worker.process(saved["job_id"]) is False
    selected = deepcopy(journey.jobs.get_job(saved["job_id"])["chart_snapshot"])
    assert selected["kind"] == "snapshot"
    journey.now[0] += 61
    monkeypatch.setattr(journey.jobs, "finalize", original_finalize)
    assert journey.worker.process(saved["job_id"])
    job = journey.jobs.get_job(saved["job_id"])
    assert job["chart_snapshot"] == selected and len(journey.adapter.calls) == 1 and journey.adapter.chart_calls == 1
    final_keys = [key for key in journey.objects.put_keys if "-final-" in key]
    assert len(final_keys) == 2 and len(set(final_keys)) == 2
    path = "attempts/" + attempt["attempt_id"]
    frozen_result = api(journey, "GET", path + "/calculation", token=token)
    assert frozen_result["statusCode"] == 200
    refresh = api(journey, "GET", path + "/chart-link", token=token)
    assert refresh["statusCode"] == 200 and journey.objects.sign_calls[-1][-1] == 300
    assert api(journey, "GET", path + "/calculation", token=token) == frozen_result
    assert api(journey, "DELETE", "session", token=token)["statusCode"] == 204
    key = job["chart_publication"]["key"]
    assert (journey.storage.bucket, key) in journey.objects.objects


def test_missing_retained_adapter_and_tampered_raw_never_invoke_a_fallback(journey):
    token = login(journey)
    attempt = create(journey, token)
    assert submit(journey, token, attempt)["statusCode"] == 202
    saved = stored_attempt(journey, token, attempt)
    journey.worker.adapters = CalculatorRegistry([])
    assert journey.worker.process(saved["job_id"]) is False
    assert journey.jobs.get_job(saved["job_id"])["call_phase"] == "not_started"
    journey.worker.adapters = CalculatorRegistry([journey.adapter])
    journey.now[0] += 61
    raw_key = next(key for key in journey.objects.objects if key[1].endswith(".bin") and "CPR-ACTION" in key[1])
    journey.objects.objects[raw_key]["Body"] = b"corrupted"
    assert journey.worker.process(saved["job_id"])
    failed = stored_attempt(journey, token, attempt)
    assert failed["state"] == "failed" and failed["error_code"] == "STORED_INPUT_INVALID"
    assert failed["evaluation"] is None and not journey.adapter.calls


def test_a_missing_previously_saved_response_is_integrity_failure_not_endless_unknown(journey, monkeypatch):
    token = login(journey)
    attempt = create(journey, token)
    assert submit(journey, token, attempt)["statusCode"] == 202
    saved = stored_attempt(journey, token, attempt)
    def fail_final(*args):
        raise JourneyError("TEMPORARILY_UNAVAILABLE")
    monkeypatch.setattr(journey.storage, "save_final", fail_final)
    assert journey.worker.process(saved["job_id"]) is False
    job = journey.jobs.get_job(saved["job_id"])
    assert job["call_phase"] == "candidate_saved" and job["candidate_ref"] is not None
    del journey.objects.objects[(job["candidate_ref"]["bucket"], job["candidate_ref"]["key"])]
    journey.now[0] += 61
    assert journey.worker.process(saved["job_id"])
    failed = stored_attempt(journey, token, attempt)
    assert failed["state"] == "failed" and failed["error_code"] == "STORED_INPUT_INVALID"
    assert len(journey.adapter.calls) == 1


@pytest.mark.parametrize("artifact", ["manifest", "raw", "meta"])
def test_confirmed_input_loss_terminates_once_without_calculator_execution(journey, artifact):
    token = login(journey)
    attempt = create(journey, token)
    assert submit(journey, token, attempt)["statusCode"] == 202
    saved = stored_attempt(journey, token, attempt)
    job = journey.jobs.get_job(saved["job_id"])
    ref = job["input_manifest_ref"]
    manifest = json.loads(journey.objects.objects[(ref["bucket"], ref["key"])]["Body"])
    key = ref["key"] if artifact == "manifest" else manifest["raw_base"] + {"raw": ".bin", "meta": ".meta.json"}[artifact]
    del journey.objects.objects[(ref["bucket"], key)]

    assert journey.worker.process(saved["job_id"]) is True
    failed = stored_attempt(journey, token, attempt)
    assert failed["state"] == "failed" and failed["error_code"] == "STORED_INPUT_INVALID"
    assert failed["evaluation"] is None and failed["active_counted"] is False
    before = journey.state.get_progress(journey.auth.authenticate(token))
    assert all(slot["open_attempts"] == 0 and slot["completed"] is False for slot in before["slots"].values())
    assert journey.worker.process(saved["job_id"]) is True
    assert journey.state.get_progress(journey.auth.authenticate(token)) == before
    assert stored_attempt(journey, token, attempt) == failed
    assert not journey.adapter.calls


@pytest.mark.parametrize("artifact", ["final", "chart"])
def test_confirmed_result_loss_returns_fixed_error_without_rewriting_completion(journey, artifact):
    journey.adapter.chart_kind = "snapshot"
    token = login(journey)
    attempt = create(journey, token)
    assert submit(journey, token, attempt)["statusCode"] == 202
    saved = stored_attempt(journey, token, attempt)
    assert journey.worker.process(saved["job_id"]) is True
    evaluated = stored_attempt(journey, token, attempt)
    before = journey.state.get_progress(journey.auth.authenticate(token))
    job = journey.jobs.get_job(saved["job_id"])
    ref = job["final_ref"] if artifact == "final" else job["chart_publication"]
    del journey.objects.objects[(journey.storage.bucket, ref["key"])]
    suffix = "/calculation" if artifact == "final" else "/chart-link"
    result = api(journey, "GET", "attempts/" + attempt["attempt_id"] + suffix, token=token)
    assert result["statusCode"] == 503 and "STORED_INPUT_INVALID" in result["body"]
    assert ref["key"] not in result["body"]
    assert stored_attempt(journey, token, attempt) == evaluated
    assert journey.state.get_progress(journey.auth.authenticate(token)) == before
    assert len(journey.adapter.calls) == 1


def test_confirmed_chart_snapshot_loss_cannot_refetch_or_publish_new_data(journey, monkeypatch):
    journey.adapter.chart_kind = "snapshot"
    token = login(journey)
    attempt = create(journey, token)
    assert submit(journey, token, attempt)["statusCode"] == 202
    saved = stored_attempt(journey, token, attempt)
    original_publish = journey.storage.publish_selected_chart
    def interrupted_publish(*args):
        raise JourneyError("TEMPORARILY_UNAVAILABLE")
    monkeypatch.setattr(journey.storage, "publish_selected_chart", interrupted_publish)
    assert journey.worker.process(saved["job_id"]) is False
    job = journey.jobs.get_job(saved["job_id"])
    ref = job["chart_snapshot"]["snapshot_ref"]
    del journey.objects.objects[(ref["bucket"], ref["key"])]
    monkeypatch.setattr(journey.storage, "publish_selected_chart", original_publish)
    journey.now[0] += 61
    assert journey.worker.process(saved["job_id"]) is True
    failed = stored_attempt(journey, token, attempt)
    assert failed["state"] == "failed" and failed["error_code"] == "STORED_INPUT_INVALID"
    assert failed["evaluation"] is None and failed["active_counted"] is False
    assert len(journey.adapter.calls) == 1 and journey.adapter.chart_calls == 1


def test_input_loss_in_concurrent_attempt_preserves_existing_slot_completion(journey):
    token = login(journey)
    first = create(journey, token, request_id="first")
    second = create(journey, token, request_id="concurrent")
    for attempt in (first, second):
        assert submit(journey, token, attempt)["statusCode"] == 202
    assert journey.worker.process(stored_attempt(journey, token, first)["job_id"]) is True
    before = journey.state.get_progress(journey.auth.authenticate(token))
    completed_key = next(key for key, slot in before["slots"].items() if slot["completed"])
    assert before["slots"][completed_key]["open_attempts"] == 1
    second_job = journey.jobs.get_job(stored_attempt(journey, token, second)["job_id"])
    ref = second_job["input_manifest_ref"]
    del journey.objects.objects[(ref["bucket"], ref["key"])]
    assert journey.worker.process(second_job["job_id"]) is True
    assert journey.worker.process(second_job["job_id"]) is True
    failed = stored_attempt(journey, token, second)
    assert failed["state"] == "failed" and failed["evaluation"] is None
    after = journey.state.get_progress(journey.auth.authenticate(token))
    assert after["slots"][completed_key] == {**before["slots"][completed_key], "open_attempts": 0}
    assert len(journey.adapter.calls) == 1

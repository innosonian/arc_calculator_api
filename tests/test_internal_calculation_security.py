"""Independent internal-calculation isolation and stale-writer counterexamples."""

from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
import hashlib
import json
import socket
import uuid

import boto3
import pytest

from mock_journey import typed
from mock_journey.errors import JourneyError
from mock_journey.jobs import DynamoJobRepository, JobLeaseLost
from mock_journey.projection import ProjectionSchema, project_input, typed_identity
from mock_journey.state import DynamoStateRepository
from mock_journey.storage import JourneyStorage
from tests._synth import comp_session, cpr_session, condition_json
from tests.mock_storage_support import MemoryLegacyBindings, MemoryS3
from tests.test_mock_state import ScriptedClient, item


VERSION = "security-test-internal-v1"
PROJECTION = "security-test-projection-v1"
SECRET = "P2_PRIVATE_CREDENTIAL_MARKER"


def _input(training="compression_only"):
    condition = json.loads(condition_json(training_type=training, guideline="ARC2025"))
    kind = {"compression_only": "compressions", "ventilation_only": "ventilations", "cpr": "cycles"}[training]
    definition = {
        "condition": deepcopy(condition), "calculation_profile": {},
        "goal": {"kind": kind, "required": 60 if kind == "compressions" else 8 if kind == "ventilations" else 3},
        "catalog_version": "mock-catalog-v1", "profile_version": "tester-v1",
        "adapter_version": VERSION, "projection_version": PROJECTION,
    }
    cpr = (comp_session(60) if training == "compression_only"
           else cpr_session([(0, 8)]) if training == "ventilation_only"
           else cpr_session([(30, 2)]))
    body = {
        "cpr_b64_data": cpr, "aed_b64_data": b"", "condition": condition,
        "vp_event_list": [], "access_token": SECRET, "client_secret": SECRET,
        "send_result_url": "https://invalid.example/" + SECRET,
    }
    projected = project_input(body, definition, ProjectionSchema(PROJECTION, {}))
    binding = {
        "attempt_id": str(uuid.uuid4()), "epoch": str(uuid.uuid4()),
        "input_digest": typed_identity(projected), "adapter_version": VERSION,
        "projection_version": PROJECTION,
    }
    objects = MemoryS3()
    store = JourneyStorage(objects, legacy_bindings=MemoryLegacyBindings(objects),
                           stage="development", limits={"input_bytes": 100_000, "artifact_bytes": 1_000_000})
    saved = store.save_input(projected, binding)
    loaded = store.load_input(saved["manifest_ref"], binding)
    return store, objects, loaded, {**binding, "job_id": str(uuid.uuid4()), "call_id": str(uuid.uuid4())}


def _adapter(**kwargs):
    from mock_journey.internal_calculator import InternalCalculator
    return InternalCalculator(version=VERSION, projection_version=PROJECTION,
                              stage="development", **kwargs)


def _expect_contract_error(operation):
    with pytest.raises(JourneyError) as raised:
        operation()
    assert raised.value.code == "CALCULATOR_CONTRACT_MISMATCH"
    assert SECRET not in str(raised.value)


def _candidate(training="compression_only", **kwargs):
    store, objects, loaded, binding = _input(training)
    adapter = _adapter(**kwargs)
    raw = adapter.calculate(loaded, binding, lambda: None)
    return store, objects, loaded, binding, adapter, raw


def test_real_internal_calculation_does_not_write_raw_or_publish_a_chart_early(monkeypatch, capsys):
    import main
    import data_handlers.chart_data as chart_module
    from util import uploader

    store, objects, loaded, binding = _input()
    before = deepcopy(objects.objects)
    original_payload = typed.canonical_bytes(loaded.projected.payload)

    def forbidden(*args, **kwargs):
        pytest.fail("Internal candidate calculation used an ambient external side effect.")

    monkeypatch.setattr(boto3, "client", forbidden)
    monkeypatch.setattr(boto3.session.Session, "client", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "sendto", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(main, "upload_raw_input", forbidden)
    monkeypatch.setattr(uploader, "upload_raw_input", forbidden)
    monkeypatch.setattr(chart_module, "upload_json_file", forbidden)
    monkeypatch.setattr(chart_module, "create_signed_url", forbidden)
    monkeypatch.setattr(uploader, "upload_json_file", forbidden)
    monkeypatch.setattr(uploader, "create_signed_url", forbidden)
    adapter = _adapter()
    raw = adapter.calculate(loaded, binding, lambda: None)
    verified = adapter.validate_response(raw, loaded.projected, binding)
    assert verified.observed == verified.core_result["action_count"]["comp"]
    assert type(verified.observed) is int
    assert verified.core_result["chart_dataset_url"] is None
    assert objects.objects == before
    assert objects.sign_calls == []
    assert typed.canonical_bytes(loaded.projected.payload) == original_payload
    assert SECRET.encode() not in raw
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err


@pytest.mark.parametrize("training", ["compression_only", "ventilation_only"])
def test_only_training_uses_existing_integer_action_counts(training):
    _, _, loaded, binding, adapter, raw = _candidate(training)
    verified = adapter.validate_response(raw, loaded.projected, binding)
    key = "comp" if training == "compression_only" else "vent"
    assert type(verified.observed) is int
    assert verified.observed == verified.core_result["action_count"][key]


def test_cpr_without_a_confirmed_resolver_stops_before_calculation(monkeypatch):
    import main
    _, _, loaded, binding = _input("cpr")

    def forbidden(*args, **kwargs):
        pytest.fail("Unconfigured CPR completion started a calculation.")

    monkeypatch.setattr(main, "run_calculator", forbidden)
    with pytest.raises(JourneyError) as raised:
        _adapter().calculate(loaded, binding, lambda: None)
    assert raised.value.code == "TEMPORARILY_UNAVAILABLE"


def test_preexisting_cpr_candidate_does_not_bypass_a_missing_completion_resolver():
    # This explicit test callback is not a production CPR completion policy.
    _, _, loaded, binding, _, raw = _candidate("cpr", cycle_goal_resolver=lambda evidence, definition: 0)
    with pytest.raises(JourneyError) as raised:
        _adapter().validate_response(raw, loaded.projected, binding)
    assert raised.value.code == "TEMPORARILY_UNAVAILABLE"


def test_cycle_observation_is_immutable_and_resolver_cannot_mutate_input_policy():
    snapshots = []

    def test_only_resolver(evidence, definition):
        snapshots.append(evidence)
        with pytest.raises(FrozenInstanceError):
            evidence.comp_count = 999
        assert type(evidence.cycles) is tuple
        if evidence.cycles:
            with pytest.raises(FrozenInstanceError):
                evidence.cycles[0].compression_action_count = 999
        definition["condition"]["guideline"] = SECRET
        return 0  # Only exercises the explicit callback; not a completion claim.

    _, _, loaded, binding = _input("cpr")
    before = typed.canonical_bytes(loaded.projected.payload)
    raw = _adapter(cycle_goal_resolver=test_only_resolver).calculate(loaded, binding, lambda: None)
    assert len(snapshots) == 1
    assert snapshots[0].comp_count == typed.parse_json(raw)["counts"]["comp"]
    assert typed.canonical_bytes(loaded.projected.payload) == before
    assert SECRET.encode() not in raw


@pytest.mark.parametrize("change", [
    {"cpr_sha256": "0" * 64}, {"cpr_size": 999},
    {"aed_sha256": "0" * 64}, {"aed_size": 1},
])
def test_raw_receipt_is_rechecked_before_parsing_or_falling_back_to_upload(change, monkeypatch):
    import main
    from services.calculation_context import AcceptedRaw, CalculationContextError, CalculationExecutionContext
    from util import uploader

    cpr = comp_session(2)
    receipt = AcceptedRaw(
        cpr_sha256=hashlib.sha256(cpr).hexdigest(), cpr_size=len(cpr),
        aed_sha256=hashlib.sha256(b"").hexdigest(), aed_size=0,
        key_stem=uploader.build_key_stem(), org="_no_org",
    )
    context = CalculationExecutionContext(accepted_raw=replace(receipt, **change),
                                           publish_chart=lambda value: None, observe=lambda value: None)

    def forbidden(*args, **kwargs):
        pytest.fail("Invalid raw receipt reached calculation or a fallback upload.")

    monkeypatch.setattr(main, "parse_data", forbidden)
    monkeypatch.setattr(main, "upload_raw_input", forbidden)
    with pytest.raises(CalculationContextError) as raised:
        main.run_calculator(cpr, b"", json.loads(condition_json(training_type="compression_only")),
                            stage="development", execution_context=context)
    assert str(raised.value) == "Accepted raw input mismatch"


@pytest.mark.parametrize("mutation", [
    lambda value: value["binding"].update(call_id=str(uuid.uuid4())),
    lambda value: value["binding"].update(input_digest="0" * 64),
    lambda value: value["binding"].update(adapter_version="different"),
    lambda value: value["counts"].update(comp=True),
    lambda value: value["core_result"]["action_count"].update(comp=60.0),
    lambda value: value["goal"].update(observed=True),
    lambda value: value["goal"].update(observed=-1),
    lambda value: value["goal"].update(observed=999999),
    lambda value: value["chart"].update(sha256="0" * 64),
    lambda value: value["chart"].update(data=[]),
    lambda value: value["core_result"].update(chart_dataset_url="https://invalid.example/" + SECRET),
    lambda value: value["core_result"].update(submit_arc={"ok": True}),
    lambda value: value["core_result"].pop("cpr_score"),
    lambda value: value["core_result"].pop("metrics"),
    lambda value: value["core_result"].pop("aed_score"),
    lambda value: value["core_result"].pop("training_stats"),
    lambda value: value["core_result"].pop("guide_prompts"),
    lambda value: value["core_result"].update(metrics=[]),
    lambda value: value["core_result"].update(cpr_score=[]),
    lambda value: value["core_result"].update(aed_score=None),
    lambda value: value["core_result"].update(training_stats=[]),
    lambda value: value["core_result"].update(guide_prompts=[123]),
    lambda value: value["core_result"].update(access_token=SECRET),
    lambda value: value["core_result"]["cpr_score"]["total_score"].update(overall=True),
    lambda value: value["core_result"]["training_stats"].update(cycle_count=1.0),
    lambda value: value.update(access_token=SECRET),
])
def test_corrupted_rebound_or_coerced_candidate_cannot_become_verified(mutation):
    _, _, loaded, binding, adapter, raw = _candidate()
    damaged = typed.parse_json(raw)
    mutation(damaged)
    _expect_contract_error(lambda: adapter.validate_response(typed.json_bytes(damaged), loaded.projected, binding))


@pytest.mark.parametrize("field", ["attempt_id", "job_id", "call_id", "input_digest", "adapter_version", "projection_version"])
def test_call_binding_mutation_is_detected_before_input_is_calculated(field):
    _, _, loaded, binding = _input()
    changed = {**binding, field: SECRET}
    _expect_contract_error(lambda: _adapter().calculate(loaded, changed, lambda: None))


@pytest.mark.parametrize("changed_base", [
    "../PRIVATE", "calculator_result/../PRIVATE", "https://invalid.example/PRIVATE",
    "calculator_result/interpreted_rtdata/arc/prod/_no_org/2026-09-09/PRIVATE",
])
def test_invalid_raw_location_cannot_suppress_a_verified_input_save(changed_base):
    _, _, loaded, binding = _input()
    _expect_contract_error(lambda: _adapter().calculate(replace(loaded, raw_base=changed_base), binding, lambda: None))


def test_projected_bytes_cannot_change_after_their_identity_was_bound():
    _, _, loaded, binding = _input()
    poisoned = replace(loaded, projected=replace(loaded.projected, cpr_bytes=loaded.projected.cpr_bytes + b"\0"))
    _expect_contract_error(lambda: _adapter().calculate(poisoned, binding, lambda: None))


def test_chart_is_bound_to_its_calculation_and_returned_data_is_detached():
    _, _, loaded, binding, adapter, raw = _candidate()
    verified = adapter.validate_response(raw, loaded.projected, binding)
    first = adapter.get_chart(verified, binding, lambda: None)
    first.data["injected_after_read"] = SECRET
    second = adapter.get_chart(verified, binding, lambda: None)
    assert "injected_after_read" not in second.data
    wrong = {**binding, "call_id": str(uuid.uuid4())}
    _expect_contract_error(lambda: adapter.get_chart(verified, wrong, lambda: None))


def _started_job(**changes):
    return {
        "PK": "JOB#job", "SK": "STATE", "job_id": "job", "attempt_id": "attempt",
        "input_digest": "a" * 64, "call_phase": "started", "call_id": "old-call",
        "execution_fence": 1, "fence": 2, "owner": "new-owner", "state": "running",
        "lease_until": 100, "revision": 3, "planned_candidate_ref": {"bucket": "b", "key": "old"},
        "candidate_ref": None, "chart_snapshot": {"kind": "unset", "revision": 0},
        **changes,
    }


def _jobs(job, *, writes=False):
    calls = [("get", {"Item": item(job)})] + ([("write", {})] if writes else [])
    client = ScriptedClient(calls)
    return DynamoJobRepository(DynamoStateRepository(client, "security-state", clock=lambda: 20)), client


@pytest.mark.parametrize("changes", [
    {"execution_fence": 2}, {"execution_fence": True},
    {"call_phase": "candidate_saved"}, {"candidate_ref": {"bucket": "b", "key": "saved"}},
    {"chart_snapshot": {"kind": "no_chart", "revision": 1}},
])
def test_recalculation_cannot_overwrite_an_ineligible_execution(changes):
    jobs, client = _jobs(_started_job(**changes))
    allowed, _ = jobs.begin_calculation("job", "new-owner", 2, "new-call",
                                        {"bucket": "b", "key": "new"}, previous_call_id="old-call")
    assert allowed is False
    assert [operation for operation, _ in client.calls] == ["get"]


def test_recalculation_selects_a_new_candidate_under_current_lease_conditions():
    original = _started_job()
    jobs, client = _jobs(original, writes=True)
    allowed, selected = jobs.begin_calculation("job", "new-owner", 2, "new-call",
                                              {"bucket": "b", "key": "new"}, previous_call_id="old-call")
    assert allowed is True
    assert selected["call_id"] == "new-call" and selected["execution_fence"] == 2
    assert selected["planned_candidate_ref"] != original["planned_candidate_ref"]
    write = client.calls[-1][1]["TransactItems"][0]["Put"]
    names = write["ExpressionAttributeNames"]
    assert {"owner", "fence", "lease_until", "call_id", "call_phase", "revision"} <= set(names.values())
    assert "old-call" in json.dumps(write["ExpressionAttributeValues"])


def test_old_owner_cannot_commit_its_candidate_after_a_new_lease_selected_another():
    current = _started_job(call_id="new-call", execution_fence=2,
                           planned_candidate_ref={"bucket": "b", "key": "new"})
    jobs, client = _jobs(current)
    with pytest.raises(JobLeaseLost):
        jobs.mark_calculation_saved("job", "old-owner", 1,
                                    {"bucket": "b", "key": "old", "sha256": "a" * 64, "size": 1})
    assert [operation for operation, _ in client.calls] == ["get"]


def test_old_writer_late_object_save_cannot_replace_the_new_call_candidate():
    store, objects, _, old_binding = _input()
    new_binding = {**old_binding, "call_id": str(uuid.uuid4())}
    old_ref = store.planned_calculation(old_binding)
    new_ref = store.planned_calculation(new_binding)
    assert old_ref["key"] != new_ref["key"]
    store.save_calculation(new_ref, b"new candidate", new_binding)
    store.save_calculation(old_ref, b"late old candidate", old_binding)
    assert store.load_calculation(new_ref, new_binding) == b"new candidate"
    assert objects.sign_calls == []
    with pytest.raises(JourneyError):
        store.load_calculation(old_ref, new_binding)


def test_worker_recovers_an_existing_candidate_without_recalculating(monkeypatch):
    from mock_journey.worker import JourneyWorker

    store, objects, loaded, binding, adapter, raw = _candidate()
    planned = store.planned_calculation(binding)
    store.save_calculation(planned, raw, binding)
    manifest_key = loaded.raw_base + ".request.json"
    manifest = objects.objects[(store.bucket, manifest_key)]["Body"]
    job = {**binding, "planned_candidate_ref": planned, "call_phase": "started",
           "candidate_ref": None, "fence": 2, "execution_fence": 1,
           "chart_snapshot": {"kind": "unset", "revision": 0},
           "input_manifest_ref": {"bucket": store.bucket, "key": manifest_key,
                                  "sha256": hashlib.sha256(manifest).hexdigest(), "size": len(manifest)}}

    class RecoveryJobs:
        """Orchestration double only; real DB atomicity is tested separately."""
        finalized = None

        def claim(self, job_id, owner, lease_seconds):
            assert job_id == binding["job_id"]
            return "recover", deepcopy(job)

        def renew_lease(self, *args):
            return None

        def mark_calculation_saved(self, job_id, owner, fence, reference):
            assert reference["key"] == planned["key"]
            job.update(call_phase="candidate_saved", candidate_ref=deepcopy(reference))
            return deepcopy(job)

        def get_job(self, job_id):
            return deepcopy(job)

        def pin_chart(self, job_id, owner, fence, selection):
            job["chart_snapshot"] = {**deepcopy(selection), "revision": 1}
            return deepcopy(job["chart_snapshot"])

        def finalize(self, job_id, owner, fence, final_ref, evaluation, publication):
            self.finalized = (deepcopy(final_ref), deepcopy(evaluation), deepcopy(publication))

        def mark_failed(self, *args):
            pytest.fail("Valid stored candidate was classified as a failure.")

    class Registry:
        def resolve(self, version, projection_version):
            assert (version, projection_version) == (VERSION, PROJECTION)
            return adapter

    def forbidden(*args, **kwargs):
        pytest.fail("Stored-candidate recovery started a new calculation.")

    jobs = RecoveryJobs()
    monkeypatch.setattr(adapter, "calculate", forbidden)
    monkeypatch.setattr(store, "planned_calculation", forbidden)
    worker = JourneyWorker(jobs, store, Registry(), lease_seconds=30, retry_seconds=1, clock=lambda: 20)
    assert worker.process(binding["job_id"]) is True
    assert jobs.finalized is not None
    _, evaluation, publication = jobs.finalized
    assert evaluation["goal"]["observed"] == 60
    assert publication["call_id"] == binding["call_id"]

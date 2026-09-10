"""Independent v1/v2 completion evidence and pending-state counterexamples."""

from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import socket
import uuid

import boto3
import pytest

from mock_journey import typed
from mock_journey.contracts import VerifiedCalculation
from mock_journey.errors import JourneyError
from mock_journey.internal_calculator import (
    InternalCalculator, PENDING_GOAL_ADAPTER_VERSION, PENDING_GOAL_PROFILE_VERSION,
)
from mock_journey.jobs import DynamoJobRepository
from mock_journey.projection import ProjectionSchema, project_input, typed_identity
from mock_journey.state import DynamoStateRepository
from mock_journey.storage import JourneyStorage
from mock_journey.worker import evaluate
from services.legacy_document import _is_pass
from tests._synth import comp_session, condition_json, cpr_session
from tests.mock_storage_support import MemoryLegacyBindings, MemoryS3
from tests.test_mock_state import ScriptedClient, item, snapshot


SECRET = "L1_INDEPENDENT_PRIVATE_MARKER"
PROJECTION = "security-only-projection"


def accepted(training="cpr", *, pending=True):
    version = PENDING_GOAL_ADAPTER_VERSION if pending else "security-legacy-calculator-v1"
    profile = PENDING_GOAL_PROFILE_VERSION if pending else "security-legacy-profile-v1"
    condition = json.loads(condition_json(training_type=training, guideline="ARC2025"))
    kind, required = {"cpr": ("cycles", 3), "compression_only": ("compressions", 60),
                      "ventilation_only": ("ventilations", 8)}[training]
    definition = {"condition": deepcopy(condition), "calculation_profile": {},
                  "goal": {"kind": kind, "required": required}, "catalog_version": "mock-catalog-v1",
                  "profile_version": profile, "adapter_version": version, "projection_version": PROJECTION}
    body = {"cpr_b64_data": comp_session(60) if training == "compression_only" else
            cpr_session([(0, 8)] if training == "ventilation_only" else [(30, 2)] * 3),
            "aed_b64_data": b"", "condition": condition, "vp_event_list": [],
            "access_token": SECRET, "client_secret": SECRET}
    projected = project_input(body, definition, ProjectionSchema(PROJECTION, {}))
    binding = {"attempt_id": str(uuid.uuid4()), "epoch": str(uuid.uuid4()),
               "input_digest": typed_identity(projected), "adapter_version": version,
               "projection_version": PROJECTION}
    objects = MemoryS3()
    storage = JourneyStorage(objects, legacy_bindings=MemoryLegacyBindings(objects), stage="local-test",
                             limits={"input_bytes": 100_000, "artifact_bytes": 1_000_000})
    saved = storage.save_input(projected, binding)
    loaded = storage.load_input(saved["manifest_ref"], binding)
    adapter = InternalCalculator(version=version, projection_version=PROJECTION, stage="local-test",
                                 allow_pending_cycle_goal=pending)
    return loaded, {**binding, "job_id": str(uuid.uuid4()), "call_id": str(uuid.uuid4())}, adapter, objects


def candidate(training="cpr", *, pending=True):
    loaded, binding, adapter, objects = accepted(training, pending=pending)
    raw = adapter.calculate(loaded, binding, lambda: None)
    verified = adapter.validate_response(raw, loaded.projected, binding)
    return loaded, binding, adapter, raw, verified, objects


def contract_error(operation):
    with pytest.raises(JourneyError) as caught:
        operation()
    assert caught.value.code == "CALCULATOR_CONTRACT_MISMATCH"
    assert SECRET not in str(caught.value)


def test_real_pending_calculation_preserves_score_and_chart_without_network_or_premature_publication(monkeypatch, capsys):
    import main
    import data_handlers.chart_data as charts
    from util import uploader

    loaded, binding, adapter, objects = accepted()
    before, identity = deepcopy(objects.objects), typed_identity(loaded.projected)

    def forbidden(*args, **kwargs):
        pytest.fail("Pending completion invoked a network or ambient publication side effect.")

    for owner, name in ((boto3, "client"), (boto3.session.Session, "client"),
                        (socket.socket, "connect"), (socket.socket, "sendto"),
                        (socket, "getaddrinfo"), (main, "upload_raw_input"),
                        (charts, "upload_json_file"), (charts, "create_signed_url"),
                        (uploader, "upload_raw_input"), (uploader, "create_signed_url")):
        monkeypatch.setattr(owner, name, forbidden)
    raw = adapter.calculate(loaded, binding, lambda: None)
    verified = adapter.validate_response(raw, loaded.projected, binding)
    evaluation = evaluate(verified.core_result, loaded.projected.payload["definition"], verified)
    assert verified.goal_status == "pending_policy" and verified.observed is None
    assert evaluation["goal"] == {"kind": "cycles", "required": 3, "status": "pending_policy",
                                  "observed": None, "met": None}
    assert evaluation["program_completed"] is False
    assert evaluation["score"]["decision"] == ("pass" if _is_pass(verified.core_result, None, None, None, "adult") else "fail")
    assert evaluation["reason_codes"][0] == "GOAL_POLICY_UNRESOLVED"
    assert verified.core_result["chart_dataset_url"] is None
    assert adapter.get_chart(verified, binding, lambda: None).data
    assert objects.objects == before and objects.sign_calls == []
    assert typed_identity(loaded.projected) == identity
    assert SECRET.encode() not in raw
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err


@pytest.mark.parametrize("target_pending", [False, True])
def test_schema_cannot_be_relabelled_by_rebinding_another_version_candidate(target_pending):
    source = candidate("compression_only", pending=not target_pending)
    loaded, binding, adapter, _, _, _ = candidate("compression_only", pending=target_pending)
    foreign = typed.parse_json(source[3])
    foreign["binding"] = deepcopy(binding)
    contract_error(lambda: adapter.validate_response(typed.json_bytes(foreign), loaded.projected, binding))


@pytest.mark.parametrize("version,pending,resolver", [
    (PENDING_GOAL_ADAPTER_VERSION, False, None),
    ("security-legacy-calculator-v1", True, None),
    (PENDING_GOAL_ADAPTER_VERSION, 1, None),
    (PENDING_GOAL_ADAPTER_VERSION, True, lambda *args: 3),
])
def test_constructor_cannot_change_a_version_meaning_or_choose_between_two_policies(version, pending, resolver):
    with pytest.raises(ValueError):
        InternalCalculator(version=version, projection_version=PROJECTION, stage="local-test",
                           allow_pending_cycle_goal=pending, cycle_goal_resolver=resolver)


@pytest.mark.parametrize("status", [None, "evaluated", "policy_pending", "", True, 0, [], {}])
def test_pending_cpr_candidate_cannot_disguise_another_goal_status(status):
    loaded, binding, adapter, raw, _, _ = candidate()
    damaged = typed.parse_json(raw)
    damaged["goal"]["status"] = status
    contract_error(lambda: adapter.validate_response(typed.json_bytes(damaged), loaded.projected, binding))


@pytest.mark.parametrize("observed", [0, 3, False, True, 3.0, "3", [], {}])
def test_pending_cpr_candidate_never_turns_a_count_into_unconfirmed_completion(observed):
    loaded, binding, adapter, raw, _, _ = candidate()
    damaged = typed.parse_json(raw)
    damaged["goal"]["observed"] = observed
    contract_error(lambda: adapter.validate_response(typed.json_bytes(damaged), loaded.projected, binding))


@pytest.mark.parametrize("training", ["compression_only", "ventilation_only"])
def test_only_count_cannot_be_hidden_as_pending_policy(training):
    loaded, binding, adapter, raw, verified, _ = candidate(training)
    assert verified.goal_status == "evaluated" and type(verified.observed) is int
    damaged = typed.parse_json(raw)
    damaged["goal"].update(status="pending_policy", observed=None)
    contract_error(lambda: adapter.validate_response(typed.json_bytes(damaged), loaded.projected, binding))


@pytest.mark.parametrize("mutation", [
    lambda row: row["goal"].pop("status"),
    lambda row: row["goal"].update(met=True),
    lambda row: row["binding"].update(epoch="different-epoch"),
    lambda row: row["binding"].update(call_id=str(uuid.uuid4())),
    lambda row: row["binding"].update(input_digest="0" * 64),
    lambda row: row["counts"].update(comp=True),
    lambda row: row["counts"].update(vent=6.0),
    lambda row: row["core_result"]["action_count"].update(comp=None),
    lambda row: row["core_result"].pop("metrics"),
    lambda row: row["core_result"].update(submit_arc={"ok": True}),
    lambda row: row["core_result"].update(access_token=SECRET),
    lambda row: row["core_result"].update(chart_dataset_url="http://invalid/" + SECRET),
    lambda row: row["chart"].update(sha256="0" * 64),
    lambda row: row["chart"].update(data=[]),
])
def test_pending_goal_does_not_relax_core_counts_chart_or_input_binding_checks(mutation):
    loaded, binding, adapter, raw, _, _ = candidate()
    damaged = typed.parse_json(raw)
    mutation(damaged)
    contract_error(lambda: adapter.validate_response(typed.json_bytes(damaged), loaded.projected, binding))


@pytest.mark.parametrize("status,kind,observed", [("pending_policy", "compressions", None),
    ("pending_policy", "ventilations", None), ("pending_policy", "cycles", False),
    ("evaluated", "cycles", None), (None, "cycles", None), (True, "cycles", 3),
    ("unknown", "cycles", 3), ("evaluated", "compressions", 60.0)])
def test_verified_evidence_constructor_rejects_coercion_or_unknown_contract(status, kind, observed):
    contract_error(lambda: VerifiedCalculation({}, kind, observed, "no_chart", goal_status=status))


def test_pending_chart_remains_detached_and_bound_to_its_own_call():
    _, binding, adapter, _, verified, _ = candidate()
    first = adapter.get_chart(verified, binding, lambda: None)
    first.data["changed"] = SECRET
    assert "changed" not in adapter.get_chart(verified, binding, lambda: None).data
    contract_error(lambda: adapter.get_chart(verified, {**binding, "call_id": str(uuid.uuid4())}, lambda: None))


@pytest.mark.parametrize("profile", ["security-legacy-profile-v1", None, True])
def test_v2_adapter_cannot_silently_use_another_profile_even_with_a_new_input_digest(profile, monkeypatch):
    import main
    loaded, binding, adapter, _ = accepted()
    payload = deepcopy(loaded.projected.payload)
    payload["definition"]["profile_version"] = profile
    projected = replace(loaded.projected, payload=payload)
    rebound = {**binding, "input_digest": typed_identity(projected)}
    monkeypatch.setattr(main, "run_calculator", lambda *a, **k: pytest.fail("Invalid profile reached calculator."))
    contract_error(lambda: adapter.calculate(replace(loaded, projected=projected), rebound, lambda: None))


@pytest.mark.parametrize("mutation", [
    lambda value: value.update(program_completed=True),
    lambda value: value.update(program_completed=0),
    lambda value: value["goal"].update(observed=0),
    lambda value: value["goal"].update(met=False),
    lambda value: value["goal"].update(met=True),
    lambda value: value["goal"].update(required=3.0),
    lambda value: value["goal"].update(status="evaluated"),
    lambda value: value["goal"].pop("status"),
    lambda value: value["score"].update(decision=True),
    lambda value: value.update(reason_codes=[]),
    lambda value: value["goal"].update(access_token=SECRET),
])
def test_finalization_cannot_accept_forged_completion_from_a_pending_evaluation(mutation):
    loaded, _, _, _, verified, _ = candidate()
    definition = loaded.projected.payload["definition"]
    evaluation = evaluate(verified.core_result, definition, verified)
    mutation(evaluation)
    contract_error(lambda: DynamoJobRepository._evaluation(evaluation, {"definition_json": json.dumps(definition)}))


def test_v1_definition_cannot_accept_a_v2_pending_evaluation():
    loaded, _, _, _, verified, _ = candidate()
    definition = loaded.projected.payload["definition"]
    evaluation = evaluate(verified.core_result, definition, verified)
    old = {**definition, "adapter_version": "security-legacy-calculator-v1", "profile_version": "legacy-v1"}
    contract_error(lambda: DynamoJobRepository._evaluation(evaluation, {"definition_json": json.dumps(old)}))


@pytest.mark.parametrize("epoch,completed,reason", [("same", False, "GOAL_POLICY_UNRESOLVED"),
    ("same", True, "GOAL_POLICY_UNRESOLVED"), ("changed", False, "PROGRESS_RESET")])
def test_pending_finalization_never_authorizes_progress_and_preserves_existing_completion(epoch, completed, reason):
    loaded, binding, _, _, verified, _ = candidate()
    definition = json.dumps(loaded.projected.payload["definition"])
    evaluation = evaluate(verified.core_result, loaded.projected.payload["definition"], verified)
    chart = {**binding, "kind": "no_chart", "revision": 1}
    publication = {**binding, "kind": "no_chart", "selection_revision": 1}
    job = {"PK": "JOB#" + binding["job_id"], "SK": "STATE", **binding, "principal": "dummy",
           "definition_sha256": hashlib.sha256(definition.encode()).hexdigest(), "state": "running",
           "call_phase": "candidate_saved", "owner": "worker", "fence": 1, "lease_until": 100,
           "revision": 4, "chart_snapshot": chart}
    attempt = {"PK": "ATTEMPT#" + binding["attempt_id"], "SK": "META", **binding, "principal": "dummy",
               "program_id": "mock-cpr", "target": "adult", "state": "processing", "revision": 2,
               "bound_session_id": "original-session", "active_counted": True, "definition_json": definition}
    slot = {"completed": completed, "completed_by_attempt": "previous" if completed else None,
            "completed_at": 10 if completed else None, "open_attempts": 1 if epoch == "same" else 0}
    user = {"PK": "USER#dummy", "SK": "STATE", "principal": "dummy", "revision": 3,
            "epoch": binding["epoch"] if epoch == "same" else "new-logout-epoch", "slots": {"mock-cpr:adult": slot}}
    client = ScriptedClient([("get", {"Item": item(job)}), ("read", snapshot(job, attempt, user)), ("write", {})])
    jobs = DynamoJobRepository(DynamoStateRepository(client, "security-state", clock=lambda: 20))
    final = {"bucket": "fixture", "key": "final", "sha256": "a" * 64, "size": 1}
    result = jobs.finalize(binding["job_id"], "worker", 1, final, evaluation, publication)
    assert result["state"] == "evaluated" and result["active_counted"] is False
    assert result["progress_application"] == {"applied": False, "applied_epoch": None, "reason": reason}
    actions = client.calls[-1][1]["TransactItems"]
    user_action = actions[-1]
    if epoch == "changed":
        assert "ConditionCheck" in user_action and "Put" not in user_action
    else:
        stored_slot = user_action["Put"]["Item"]["slots"]["M"]["mock-cpr:adult"]["M"]
        assert stored_slot["completed"] == {"BOOL": completed}
        assert stored_slot["open_attempts"] == {"N": "0"}
        assert stored_slot["completed_by_attempt"] == ({"S": "previous"} if completed else {"NULL": True})
    assert client.expected == []

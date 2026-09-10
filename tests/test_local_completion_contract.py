"""Independent L1 app-contract checks; real core, no DB/server/network claims.

The read-only doubles below hold an already committed result. They deliberately
have no write/worker API, so a status read or matching POST retry cannot silently
calculate, sign a chart, or apply shared progress again in these tests.
"""

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest

from main import run_calculator
from mock_journey import typed
from mock_journey.auth import AuthManager
from mock_journey.calculation import CalculationService
from mock_journey.catalog import Catalog, PROGRAMS, TARGETS
from mock_journey.contracts import VerifiedCalculation
from mock_journey.errors import JourneyError
from mock_journey.handler import handle
from mock_journey.internal_calculator import InternalCalculator
from mock_journey.jobs import DynamoJobRepository
from mock_journey.legacy_bridge import parse_measurement
from mock_journey.projection import LoadedInput, ProjectionSchema, project_input, typed_identity
from mock_journey.service import JourneyService
from mock_journey.state import DynamoStateRepository
from mock_journey.worker import evaluate
from services.legacy_document import _is_pass
from services.legacy_response import DocumentSelection, finalize_legacy_response
from tests._synth import multipart_event
from util.uploader import build_key_stem, date_prefix


DATA = Path(__file__).parent / "dataset"
PROJECTION = "local-contract-test-projection-v1"


def _pending_versions():
    # Import at use time so this independent file does not edit the S2 owner.
    from mock_journey.internal_calculator import (
        PENDING_GOAL_ADAPTER_VERSION, PENDING_GOAL_PROFILE_VERSION,
    )
    return PENDING_GOAL_ADAPTER_VERSION, PENDING_GOAL_PROFILE_VERSION


def _definition(program, target):
    ident, _, kind, required = program
    adapter_version, profile_version = _pending_versions()
    training = {"compressions": "compression_only", "ventilations": "ventilation_only"}.get(kind, "cpr")
    return {
        "condition": {"mode": "training", "target": target, "training_type": training,
                      "guideline": "ARC2025", "cpr_cycle_type": "152" if target == "infant" else "302",
                      "is_2rescuers": ident in ("mock-two-rescuer-cpr", "mock-two-rescuer-aed")},
        "calculation_profile": {}, "goal": {"kind": kind, "required": required},
        "catalog_version": "mock-catalog-v1", "profile_version": profile_version,
        "adapter_version": adapter_version, "projection_version": PROJECTION,
    }


def _real_case(program, target):
    definition = _definition(program, target)
    kind = definition["goal"]["kind"]
    filename = ("cco_1.bin" if kind == "compressions" else
                "vo_1.bin" if kind == "ventilations" and target == "infant" else
                "adult_vo_1.bin" if kind == "ventilations" else "cpr_1.bin")
    body = {"condition": deepcopy(definition["condition"]), "cpr_b64_data": (DATA / filename).read_bytes(),
            "aed_b64_data": (DATA / "aed_1.bin").read_bytes() if program[0] == "mock-two-rescuer-aed" else b"",
            "vp_event_list": []}
    schema = ProjectionSchema(PROJECTION, {})
    accepted = multipart_event({"rawHexBPfile": body["cpr_b64_data"], "aedHexBPfile": body["aed_b64_data"],
                                "condition": json.dumps(body["condition"])})
    # Match the actual first HTTP acceptance. Its existing parser records absent
    # response-context form fields as explicit null; typed digests preserve that
    # distinction instead of conflating a hand-made Python input with wire input.
    projected = project_input(parse_measurement(accepted), definition, schema)
    binding = {"attempt_id": str(uuid.uuid4()), "epoch": str(uuid.uuid4()),
               "input_digest": typed_identity(projected), "adapter_version": definition["adapter_version"],
               "projection_version": PROJECTION, "job_id": str(uuid.uuid4()), "call_id": str(uuid.uuid4())}
    stem = build_key_stem()
    loaded = LoadedInput(projected, f"calculator_result/interpreted_rtdata/arc/local-validation/_no_org/{date_prefix(stem)}/{stem}")
    adapter = InternalCalculator(version=definition["adapter_version"], projection_version=PROJECTION,
                                 stage="local-validation", allow_pending_cycle_goal=True)
    return SimpleNamespace(program=program, body=body, definition=definition, projected=projected, binding=binding,
                           loaded=loaded, adapter=adapter, schema=schema)


class _ReadOnlyState:
    """Real session/attempt authorization checks around immutable fixture rows."""

    def __init__(self):
        self.sessions, self.attempts, self.progress = {}, {}, None

    def create_session(self, session, slots):
        self.sessions[session["session_id"]] = deepcopy(session)

    def get_session(self, ident):
        return deepcopy(self.sessions.get(ident))

    def get_attempt(self, auth, ident):
        DynamoStateRepository._check_session(self.get_session(auth.session_id), auth, 1000)
        attempt = self.attempts.get(ident)
        DynamoStateRepository._check_attempt(attempt, auth)
        return deepcopy(attempt)

    def get_progress(self, auth):
        DynamoStateRepository._check_session(self.get_session(auth.session_id), auth, 1000)
        return deepcopy(self.progress)


class _CommittedStorage:
    def __init__(self, snapshot, binding):
        self.snapshot, self.binding, self.reads = snapshot, deepcopy(binding), 0

    def read_final(self, reference, binding, publication):
        assert binding == self.binding
        self.reads += 1
        return self.snapshot

    def save_input(self, *args, **kwargs):
        pytest.fail("A committed input retry attempted to store a new input.")

    def sign_chart(self, *args, **kwargs):
        pytest.fail("A result read renewed its frozen chart URL.")


def _read_world(case, core, verified):
    body = {"condition": deepcopy(case.definition["condition"]),
            **deepcopy(case.projected.payload["response_context"])}
    document = case.projected.payload["document_context"]
    final = finalize_legacy_response(body, deepcopy(core), document_selection=DocumentSelection(
        document["document_source"], deepcopy(document["document"])))
    evaluation = evaluate(final, case.definition, verified)
    state = _ReadOnlyState()
    auth = AuthManager(state, "local-contract-only", {"v1": b"L" * 32}, "v1", clock=lambda: 1000)
    session, token = auth.login("test@test.com", "2222", Catalog().slot_keys)
    _, other_token = auth.login("test@test.com", "2222", Catalog().slot_keys)
    ident = case.binding["attempt_id"]
    program_id = case.program[0]
    target = case.definition["condition"]["target"]
    pending = evaluation["goal"].get("status") == "pending_policy"
    applied = evaluation["program_completed"]
    state.attempts[ident] = {
        **case.binding, "principal": session["principal"], "bound_session_id": session["session_id"],
        "program_id": program_id, "target": target, "profile_name": "tester", "state": "evaluated",
        "definition_json": json.dumps(case.definition), "evaluation": evaluation, "active_counted": False,
        "progress_application": {"applied": applied, "applied_epoch": case.binding["epoch"] if applied else None,
                                 "reason": "GOAL_POLICY_UNRESOLVED" if pending else
                                 "APPLIED" if applied else "REQUIREMENTS_NOT_MET"},
    }
    state.progress = {"epoch": case.binding["epoch"], "revision": 5,
                      "slots": {key: {"completed": key == f"{program_id}:{target}" and applied,
                                      "open_attempts": 0} for key in Catalog().slot_keys}}
    job = {**case.binding, "state": "done", "final_ref": {"test": "immutable"},
           "chart_publication": {"test": "precommitted"}}
    jobs = SimpleNamespace(get_job=lambda ident: deepcopy(job))
    snapshot = json.dumps(final).encode("utf-8")
    storage = _CommittedStorage(snapshot, case.binding)
    calculation = CalculationService(state, jobs, storage, {PROJECTION: case.schema},
                                     payload_limit=1_000_000, clock=lambda: 1000)
    service = JourneyService(state, auth, Catalog(), calculation)
    return SimpleNamespace(service=service, state=state, storage=storage, token=token, other_token=other_token,
                           final=final, evaluation=evaluation)


def _request(world, case, operation="calculation", *, alias=False, retry=False, other=False, changed=False):
    ident = case.binding["attempt_id"]
    path = "/cpr-analysis" if alias else f"/mock/v1/attempts/{ident}" + ("/" + operation if operation else "")
    if retry:
        data = case.body["cpr_b64_data"]
        if changed:
            data = data[:-1] + bytes((data[-1] ^ 1,))
        event = multipart_event({"rawHexBPfile": data, "aedHexBPfile": case.body["aed_b64_data"],
                                 "condition": json.dumps(case.body["condition"])})
    else:
        event = {"headers": {}}
    event.update(httpMethod="POST" if retry else "GET", path=path)
    event["headers"]["Authorization"] = "Bearer " + (world.other_token if other else world.token)
    if alias:
        event["headers"]["X-Attempt-ID"] = ident
    return handle(event, SimpleNamespace(aws_request_id="local-contract-test"), world.service)


@pytest.mark.parametrize("kind,required,observed", [
    ("cycles", 3, 2), ("cycles", 3, 3), ("compressions", 60, 60), ("ventilations", 8, 8),
])
@pytest.mark.parametrize("score", [90, 70, None])
def test_v1_integer_evaluation_shape_and_meaning_are_unchanged(kind, required, observed, score):
    definition = {"condition": {"target": "infant"}, "goal": {"kind": kind, "required": required},
                  "adapter_version": "retained-v1", "profile_version": "retained-profile"}
    verified = VerifiedCalculation({}, kind, observed, "no_chart")
    result = {"cpr_score": {"total_score": {"overall": score}}}
    evaluation = evaluate(result, definition, verified)
    assert set(evaluation["goal"]) == {"kind", "required", "observed", "met"}
    assert type(evaluation["goal"]["observed"]) is int
    assert type(evaluation["goal"]["met"]) is bool
    assert evaluation["program_completed"] is (observed >= required and score == 90)
    assert DynamoJobRepository._evaluation(evaluation, {"definition_json": json.dumps(definition)}) == evaluation


@pytest.mark.parametrize("score,decision,reasons", [
    (90, "pass", ["GOAL_POLICY_UNRESOLVED"]),
    (70, "fail", ["GOAL_POLICY_UNRESOLVED", "SCORE_NOT_PASS"]),
    (None, "fail", ["GOAL_POLICY_UNRESOLVED", "SCORE_NOT_PASS"]),
])
def test_pending_goal_is_not_a_synthetic_score_failure(score, decision, reasons):
    definition = _definition(PROGRAMS[0], "infant")
    verified = VerifiedCalculation({}, "cycles", None, "no_chart", goal_status="pending_policy")
    evaluation = evaluate({"cpr_score": {"total_score": {"overall": score}}}, definition, verified)
    assert evaluation == {
        "goal": {"kind": "cycles", "required": 3, "observed": None, "met": None, "status": "pending_policy"},
        "score": {"decision": decision}, "program_completed": False, "reason_codes": reasons,
    }
    assert DynamoJobRepository._evaluation(evaluation, {"definition_json": json.dumps(definition)}) == evaluation


@pytest.mark.parametrize("program,target", [(program, target) for program in PROGRAMS for target in TARGETS],
                         ids=[f"{program[0]}-{target}" for program in PROGRAMS for target in TARGETS])
def test_all_15_explicit_definitions_preserve_real_core_and_separate_http_completion(program, target):
    case = _real_case(program, target)
    expected = run_calculator(case.body["cpr_b64_data"], case.body["aed_b64_data"],
                              case.body["condition"], [], stage="test")
    raw = case.adapter.calculate(case.loaded, case.binding, lambda: None)
    verified = case.adapter.validate_response(raw, case.projected, case.binding)
    assert typed.canonical_bytes(verified.core_result) == typed.canonical_bytes(expected)
    assert case.adapter.get_chart(verified, case.binding, lambda: None).data
    world = _read_world(case, verified.core_result, verified)
    before_rows, before_progress, before_snapshot = deepcopy(world.state.attempts), deepcopy(world.state.progress), world.storage.snapshot
    response = _request(world, case)
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body.pop("submit_arc") == {"status": "disabled", "ok": False, "error": "arc_contract_pending"}
    assert typed.canonical_bytes(body) == typed.canonical_bytes(world.final)
    assert not {"evaluation", "progress_application", "goal_status", "submit_hstm"}.intersection(body)
    status = _request(world, case, "")
    assert status["statusCode"] == 200
    status_body = json.loads(status["body"])
    assert status_body["evaluation"] == world.evaluation
    assert status_body["calculation_path"] == f'/mock/v1/attempts/{case.binding["attempt_id"]}/calculation'
    passed = bool(_is_pass(world.final, None, None, None, target))
    assert world.evaluation["score"]["decision"] == ("pass" if passed else "fail")
    if program[2] == "cycles":
        assert world.evaluation["goal"]["status"] == "pending_policy"
        assert world.evaluation["goal"]["observed"] is None and world.evaluation["goal"]["met"] is None
        assert world.evaluation["program_completed"] is False
    else:
        assert world.evaluation["goal"]["status"] == "evaluated"
        assert type(world.evaluation["goal"]["observed"]) is int
        assert type(world.evaluation["goal"]["met"]) is bool
    for alias in (False, True):
        assert _request(world, case, retry=True, alias=alias) == response
        conflict = _request(world, case, retry=True, alias=alias, changed=True)
        assert conflict["statusCode"] == 409
        assert json.loads(conflict["body"])["error"]["code"] == "ATTEMPT_INPUT_CONFLICT"
    denied = _request(world, case, other=True)
    assert denied["statusCode"] == 404
    assert _request(world, case) == response
    assert world.state.attempts == before_rows and world.state.progress == before_progress
    assert world.storage.snapshot == before_snapshot


def test_catalog_still_exposes_five_programs_and_three_targets_without_claiming_runtime_configured():
    catalog = Catalog()
    progress = {"epoch": "test-epoch", "revision": 1,
                "slots": {key: {"completed": False, "open_attempts": 0} for key in catalog.slot_keys}}
    view = catalog.programs_view(progress)
    assert len(view["programs"]) == 5 and len(catalog.slot_keys) == 15
    assert view["guideline"] == "ARC2025" and view["profile_name"] == "tester"
    assert all(item["supported_targets"] == ["adult", "child", "infant"] for item in view["programs"])
    # L1 is not the L3 product runtime: a bare catalog still cannot invent an
    # execution definition merely because it advertises the approved programs.
    with pytest.raises(JourneyError) as error:
        catalog.definition("mock-cpr", "adult")
    assert error.value.code == "CALCULATOR_CONTRACT_MISMATCH"

"""V18 unit checks for CourseCalculationBridge. Fakes only; goldens are not rewritten."""

from dataclasses import replace
from pathlib import Path
import inspect

import pytest

from mock_journey.course_calculation import CourseCalculationBridge
from mock_journey.course_contracts import (
    CONDITION_KEYS, CONTRACT_VERSION, EXECUTION_KEYS, POLICY_VERSION, AssignmentBinding,
    CourseView, GateView, InventoryView, PUBLIC_SYMBOLS, StartCommand, definition_digest,
    parse_owned, sealed_bundle, scope_identity, validate_execution_definition,
)
from mock_journey.course_errors import CourseError
from mock_journey.course_submission import CourseCompletionPlan, final_phase_for_evaluation
from mock_journey.models import AuthContext
from mock_journey.typed import digest, parse_json
from tests.test_vcc_contract import (
    BUNDLE_DOC, MAPPING_DOC, VECTORS, assignment_501, baseline_bundle,
)


ROOT = Path(__file__).resolve().parents[1]
EPOCH = "80000000-0000-4000-8000-000000000001"
SESSION = "20000000-0000-4000-8000-000000000001"
REQUEST = "10000000-0000-4000-8000-000000000001"
ATTEMPT = "50000000-0000-4000-8000-000000000001"
INPUT_DIGEST = "a" * 64


def _view(bundle=None):
    owned = sealed_bundle(bundle if bundle is not None else baseline_bundle())
    scope_key = digest(scope_identity(owned.scope))
    learner_key = BUNDLE_DOC["learner_key"]
    assignment = AssignmentBinding(owned.scope, owned.public_ids)
    inventory = InventoryView(learner_key, EPOCH, 1, 1, "ready", None, (assignment,))
    gate = GateView(scope_key, EPOCH, "ready", None, 1, owned.definition_hash)
    return CourseView(scope_key, owned.public_ids, owned, {"items": []}, gate, inventory)


def _auth(view):
    return AuthContext(SESSION, view.bundle.scope.learner.principal, 1, 4102444800)


def _command(view, placement_id=1003):
    return StartCommand(REQUEST, view.public_ids.enrollment_id, view.public_ids.course_id,
                        placement_id, view.bundle.definition_hash)


def _binding_rows(template, *, epoch=EPOCH, user_epoch=None, is_dummy=False, guideline=None,
                  already=False, start_role=None, observed=60, met=True, completed=True,
                  status="evaluated", kind="compressions", required=60, decision="pass",
                  active_counted=True):
    binding = template.binding if hasattr(template, "binding") else template
    role = start_role or binding.start_role
    goal = {"kind": kind, "required": required, "status": status}
    if status == "pending_policy":
        goal.update(observed=None, met=None)
        completed = False
        reason_codes = ["GOAL_POLICY_UNRESOLVED"] + ([] if decision == "pass" else ["SCORE_NOT_PASS"])
    else:
        goal.update(observed=observed, met=met)
        reason_codes = ([] if met else ["GOAL_NOT_MET"]) + ([] if decision == "pass" else ["SCORE_NOT_PASS"])
    evaluation = {
        "goal": goal, "score": {"decision": decision},
        "program_completed": completed, "reason_codes": reason_codes,
    }
    frozen = parse_json(template.existing_template_json) if hasattr(template, "existing_template_json") else {}
    condition = frozen.get("condition") or {"guideline": guideline or "ARC2025"}
    if guideline is not None:
        condition = {**condition, "guideline": guideline}
    attempt = {
        "attempt_id": ATTEMPT, "job_id": "job-1", "epoch": epoch, "revision": 1,
        "state": "evaluated" if already else "processing", "active_counted": active_counted,
        "input_digest": INPUT_DIGEST, "course_binding": binding,
        "definition_json": {**frozen, "condition": condition} if frozen else {"condition": condition},
        "is_dummy": is_dummy, "principal": "fixture-learner@example.test",
    }
    job = {
        "job_id": "job-1", "attempt_id": ATTEMPT, "revision": 2, "state": "done" if already else "running",
        "owner": "worker-1", "fence": 3, "input_digest": INPUT_DIGEST, "epoch": epoch,
    }
    user = {"principal": attempt["principal"], "epoch": user_epoch or epoch, "revision": 4, "is_dummy": is_dummy}
    from mock_journey.course_policy import empty_progress, placement_key
    from mock_journey.typed import json_bytes
    bundle = _view().bundle
    placement = next(p for p in bundle.placements
                     if placement_key(binding.scope_key, p.source_id) == binding.placement_key)
    rows = {
        "is_dummy": is_dummy,
        "bundle": bundle,
        "head": {"revision": 1, "completed_placements": [], "scope_key": binding.scope_key,
                 "epoch": epoch, "definition_hash": bundle.definition_hash,
                 "progress_json": json_bytes(empty_progress()).decode()},
        "item": {"revision": 0, "completed": False, "passed": None, "epoch": epoch,
                 "source_id": placement.source_id, "public_link_id": placement.public_link_id,
                 "placement_key": binding.placement_key, "content_version": binding.content_version},
        "final": {"revision": 1, "phase": "active", "active_attempt_id": ATTEMPT,
                  "passed_attempt_id": None, "epoch": epoch},
    }
    return job, attempt, user, rows, evaluation


class ForbiddenCatalog:
    def get_definition(self, program_id, target):
        raise AssertionError(f"catalog.get_definition({program_id!r}, {target!r})")


class TestV18ExecutionFreeze:
    def test_intermediate_assessment_uses_training_role(self):
        from mock_journey.course_provider import validate_bundle
        from mock_journey.course_settings import fixture_course_settings
        bundle = baseline_bundle()
        placements = list(bundle.placements)
        detail = parse_json(placements[2].detail_json)
        detail["itemType"] = "assessment"
        placements[2] = replace(placements[2], kind="assessment", detail_json=detail)
        changed = sealed_bundle(replace(bundle, placements=tuple(placements)))
        validate_bundle(changed, fixture_course_settings())
        view = _view(changed)
        template = CourseCalculationBridge().prepare(_auth(view), _command(view, 1003), view)
        assert template.binding.start_role == "training"

    def test_contract_version_and_public_protocol_name(self):
        assert CONTRACT_VERSION == "vcc-internal-v1"
        assert "CourseCalculationBridge" in PUBLIC_SYMBOLS
        assert POLICY_VERSION == "vcc-policy-v1"

    def test_source_does_not_look_up_catalog_by_course_id(self):
        source = inspect.getsource(CourseCalculationBridge.prepare)
        assert "get_definition" not in source
        text = (ROOT / "mock_journey" / "course_calculation.py").read_text()
        assert "get_definition" not in text
        assert "ExecutionCatalog" not in text

    def test_seven_key_freeze_matches_mapping_and_condition_six_keys(self):
        view = _view()
        template = CourseCalculationBridge().prepare(_auth(view), _command(view, 1003), view)
        frozen = parse_json(template.existing_template_json)
        mapped = MAPPING_DOC["mappings"]["src-place-1003"]["execution"]
        validate_execution_definition(mapped)
        assert tuple(sorted(key for key in frozen if key != "mapping_version")) == tuple(sorted(EXECUTION_KEYS))
        for key in EXECUTION_KEYS:
            assert frozen[key] == mapped[key]
        assert tuple(frozen["condition"]) == CONDITION_KEYS
        assert frozen["mapping_version"] == view.bundle.mapping_version == MAPPING_DOC["mapping_version"]
        assert frozen["condition"]["guideline"] == "ARC2025"
        assert template.binding.policy_version == POLICY_VERSION
        assert template.binding.start_role == "training"
        assert template.binding.scope_key == assignment_501()["scope_key"]
        assert template.binding.placement_key == digest([template.binding.scope_key, "src-place-1003"])
        assert template.binding.definition_hash == VECTORS["baseline_digest"]
        assert template.binding.epoch == EPOCH
        blob = template.existing_template_json.decode()
        for secret in ("resume", "accessToken", "signedUrl", "Authorization"):
            assert secret not in blob

    def test_assessment_role_and_no_catalog_side_effect(self):
        catalog = ForbiddenCatalog()
        view = _view()
        template = CourseCalculationBridge().prepare(_auth(view), _command(view, 1005), view)
        with pytest.raises(AssertionError):
            catalog.get_definition("src-course-101", "adult")
        assert template.binding.start_role == "final_assessment"
        frozen = parse_json(template.existing_template_json)
        assert frozen["goal"] == MAPPING_DOC["mappings"]["src-place-1005"]["execution"]["goal"]

    def test_missing_and_unsupported_execution(self):
        view = _view()
        with pytest.raises(CourseError) as missing:
            CourseCalculationBridge().prepare(_auth(view), _command(view, 1001), view)
        assert missing.value.code == "EXECUTION_DEFINITION_MISSING"
        items = list(view.bundle.placements)
        broken = replace(items[2], execution_status="unsupported", execution_json=None)
        bundle = replace(view.bundle, placements=tuple(items[:2] + [broken] + items[3:]))
        bundle = sealed_bundle(bundle)
        bad = _view(bundle)
        with pytest.raises(CourseError) as unsupported:
            CourseCalculationBridge().prepare(_auth(bad), _command(bad, 1003), bad)
        assert unsupported.value.code == "EXECUTION_DEFINITION_UNSUPPORTED"

    def test_five_key_ready_payload_is_unsupported(self):
        view = _view()
        item = view.bundle.placements[2]
        five = {key: parse_owned(item.execution_json)[key] for key in EXECUTION_KEYS[:5]}
        with pytest.raises(CourseError):
            replace(item, execution_json=five)

    def test_only_sixty_observed_fifty_nine_is_not_passed(self):
        view = _view()
        template = CourseCalculationBridge().prepare(_auth(view), _command(view, 1005), view)
        evaluation = {
            "goal": {"kind": "compressions", "required": 60, "observed": 59, "met": False, "status": "evaluated"},
            "score": {"decision": "pass"},
            "program_completed": False,
            "reason_codes": ["GOAL_NOT_MET"],
        }
        assert final_phase_for_evaluation(evaluation, start_role="final_assessment") == "free"
        assert evaluation["score"]["decision"] == "pass"
        job, attempt, user, rows, _ = _binding_rows(
            template, observed=59, met=False, completed=False, kind="compressions",
            required=60, start_role="final_assessment",
        )
        attempt["course_binding"] = template.binding
        plan = CourseCompletionPlan().build(job, attempt, user, rows, evaluation)
        receipt = parse_json(plan.result_receipt_json)
        assert receipt["final_phase"] == "free"
        assert receipt["evaluation"]["program_completed"] is False
        actions = parse_json(plan.actions_json)
        final = next(item for item in actions["writes"] if item["kind"] == "COURSE_FINAL")
        assert final["set"]["phase"] == "free"
        assert final["set"]["passed_attempt_id"] is None

    def test_pending_policy_is_not_fail(self):
        view = _view()
        template = CourseCalculationBridge().prepare(_auth(view), _command(view, 1005), view)
        evaluation = {
            "goal": {"kind": "cycles", "required": 3, "observed": None, "met": None, "status": "pending_policy"},
            "score": {"decision": "fail"},
            "program_completed": False,
            "reason_codes": ["GOAL_POLICY_UNRESOLVED", "SCORE_NOT_PASS"],
        }
        assert final_phase_for_evaluation(evaluation, start_role="final_assessment") == "policy_pending"
        job, attempt, user, rows, _ = _binding_rows(template, status="pending_policy", kind="cycles", required=3, decision="fail")
        plan = CourseCompletionPlan().build(job, attempt, user, rows, evaluation)
        receipt = parse_json(plan.result_receipt_json)
        assert receipt["final_phase"] == "policy_pending"
        assert receipt["evaluation"]["program_completed"] is False
        assert receipt["progress_application"]["reason"] == "GOAL_POLICY_UNRESOLVED"
        final = next(item for item in parse_json(plan.actions_json)["writes"] if item["kind"] == "COURSE_FINAL")
        assert final["set"]["phase"] == "policy_pending"
        assert final["set"]["phase"] != "free"

    def test_definition_hash_mismatch_is_changed(self):
        view = _view()
        command = StartCommand(REQUEST, 501, 101, 1003, "b" * 64)
        with pytest.raises(CourseError) as changed:
            CourseCalculationBridge().prepare(_auth(view), command, view)
        assert changed.value.code == "DEFINITION_CHANGED"
        assert definition_digest(view.bundle) == view.bundle.definition_hash

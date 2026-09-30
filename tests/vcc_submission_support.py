"""Completion plan templates, evaluations and stored rows shared by course submission tests.

Moved unchanged from tests/test_vcc_submission.py so other test modules no longer
import a test module; test_vcc_submission re-exports the same objects.
"""

from mock_journey.course_calculation import CourseCalculationBridge
from mock_journey.course_contracts import (
    AssignmentBinding, CourseView, GateView, InventoryView, StartCommand, sealed_bundle, scope_identity,
)
from mock_journey.models import AuthContext
from mock_journey.course_policy import empty_progress, placement_key
from mock_journey.typed import digest, parse_json, json_bytes
from tests.vcc_contract_support import BUNDLE_DOC, baseline_bundle
from tests.vcc_support import EPOCH


LATER = "81000000-0000-4000-8000-000000000001"
SESSION = "20000000-0000-4000-8000-000000000001"
REQUEST = "10000000-0000-4000-8000-000000000001"
ATTEMPT = "50000000-0000-4000-8000-000000000001"
INPUT_DIGEST = "a" * 64


def _view():
    owned = sealed_bundle(baseline_bundle())
    scope_key = digest(scope_identity(owned.scope))
    assignment = AssignmentBinding(owned.scope, owned.public_ids)
    inventory = InventoryView(BUNDLE_DOC["learner_key"], EPOCH, 1, 1, "ready", None, (assignment,))
    gate = GateView(scope_key, EPOCH, "ready", None, 1, owned.definition_hash)
    return CourseView(scope_key, owned.public_ids, owned, {"items": []}, gate, inventory)


def _template(placement_id=1003):
    view = _view()
    auth = AuthContext(SESSION, view.bundle.scope.learner.principal, 1, 4102444800)
    command = StartCommand(REQUEST, 501, 101, placement_id, view.bundle.definition_hash)
    return CourseCalculationBridge().prepare(auth, command, view)


def _evaluation(*, observed=60, met=True, completed=True, status="evaluated", kind="compressions",
                required=60, decision="pass"):
    goal = {"kind": kind, "required": required, "status": status}
    if status == "pending_policy":
        goal.update(observed=None, met=None)
        completed = False
        reasons = ["GOAL_POLICY_UNRESOLVED"] + ([] if decision == "pass" else ["SCORE_NOT_PASS"])
    else:
        goal.update(observed=observed, met=met)
        reasons = ([] if met else ["GOAL_NOT_MET"]) + ([] if decision == "pass" else ["SCORE_NOT_PASS"])
    return {
        "goal": goal, "score": {"decision": decision},
        "program_completed": completed, "reason_codes": reasons,
    }


def _rows(template, *, is_dummy=False, epoch=EPOCH, user_epoch=None, already=False,
          guideline="ARC2025", active_counted=True, submit_arc=None):
    frozen = parse_json(template.existing_template_json)
    condition = dict(frozen["condition"])
    condition["guideline"] = guideline
    attempt = {
        "attempt_id": ATTEMPT, "job_id": "job-1", "epoch": epoch, "revision": 1,
        "state": "evaluated" if already else "processing", "active_counted": active_counted,
        "input_digest": INPUT_DIGEST, "course_binding": template.binding,
        "definition_json": {**frozen, "condition": condition},
        "is_dummy": is_dummy, "principal": "fixture-learner@example.test",
        "submit_arc": submit_arc,
    }
    job = {
        "job_id": "job-1", "attempt_id": ATTEMPT, "revision": 2,
        "state": "done" if already else "running", "owner": "worker-1", "fence": 3,
        "input_digest": INPUT_DIGEST, "epoch": epoch,
    }
    user = {
        "principal": attempt["principal"], "epoch": user_epoch or epoch, "revision": 4,
        "is_dummy": is_dummy,
    }
    bundle = _view().bundle
    selected = next(p for p in bundle.placements
                    if placement_key(template.binding.scope_key, p.source_id) == template.binding.placement_key)
    attempt["content_identity_hash"] = digest(parse_json(selected.content_identity_json))
    course_rows = {
        "is_dummy": is_dummy, "bundle": bundle,
        "head": {"revision": 1, "completed_placements": [], "epoch": epoch,
                 "scope_key": template.binding.scope_key, "definition_hash": bundle.definition_hash,
                 "progress_json": json_bytes(empty_progress()).decode("utf-8")},
        "item": {"revision": 0, "completed": False, "passed": None, "epoch": epoch,
                 "placement_key": template.binding.placement_key, "source_id": selected.source_id,
                 "public_link_id": selected.public_link_id, "content_version": selected.content_version},
        "final": {"revision": 1, "epoch": epoch, "phase": "active", "active_attempt_id": ATTEMPT,
                  "passed_attempt_id": None},
    }
    return job, attempt, user, course_rows

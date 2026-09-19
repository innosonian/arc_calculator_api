"""V20/V21 unit checks for completion plan and disabled ARC gateway."""

from copy import deepcopy
from dataclasses import replace

import pytest

from mock_journey.course_calculation import CourseCalculationBridge
from mock_journey.course_contracts import (
    CONTRACT_VERSION, EXCLUSION_REASON_ORDER, RECEIPT_FORBIDDEN_KEYS, AssignmentBinding,
    CourseView, GateView, InventoryView, StartCommand, sealed_bundle, scope_identity,
)
from mock_journey.course_errors import CourseError
from mock_journey.course_submission import (
    HOOK_REQUESTS, SUBMISSION_POLICY_VERSION, ArcReceipt, CourseCompletionPlan,
    DisabledArcGateway, classify_launch_status, classify_submission, collect_exclusion_reasons,
    submission_intent_id,
)
from mock_journey.models import AuthContext
from mock_journey.course_policy import CoursePolicy, empty_progress, placement_key
from mock_journey.typed import digest, parse_json, json_bytes
from tests.test_vcc_contract import BUNDLE_DOC, baseline_bundle


EPOCH = "80000000-0000-4000-8000-000000000001"
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


def _kinds(plan):
    return [item["kind"] for item in parse_json(plan.actions_json)["writes"]]


class TestV20ExclusionAndEpoch:
    def test_dummy_non_arc_and_reset_are_excluded_ok_false(self):
        template = _template()
        complete = _evaluation()
        incomplete = _evaluation(observed=10, met=False, completed=False)
        plan = CourseCompletionPlan()
        dummy_job, dummy_attempt, dummy_user, dummy_rows = _rows(template, is_dummy=True)
        dummy = plan.build(dummy_job, dummy_attempt, dummy_user, dummy_rows, complete)
        dummy_submit = parse_json(dummy.result_receipt_json)["submit_arc"]
        assert dummy_submit == {
            "status": "excluded", "ok": False, "error": None, "exclusion_reasons": ["dummy"],
        }
        non_job, non_attempt, non_user, non_rows = _rows(template, guideline="AHA2020")
        non_arc = plan.build(non_job, non_attempt, non_user, non_rows, incomplete)
        assert parse_json(non_arc.result_receipt_json)["submit_arc"]["exclusion_reasons"] == ["non_arc_guideline"]
        assert parse_json(non_arc.result_receipt_json)["submit_arc"]["ok"] is False
        reset_job, reset_attempt, reset_user, reset_rows = _rows(template, user_epoch=LATER)
        reset = plan.build(reset_job, reset_attempt, reset_user, reset_rows, complete)
        assert parse_json(reset.result_receipt_json)["submit_arc"]["exclusion_reasons"] == [
            "progress_reset_before_result",
        ]
        assert parse_json(reset.result_receipt_json)["progress_application"]["reason"] == "PROGRESS_RESET"
        assert "COURSE_HEAD" not in _kinds(reset)
        assert "COURSE_ITEM" not in _kinds(reset)
        assert "COURSE_FINAL" not in _kinds(reset)
        assert not any(key[0].startswith("COURSE#") for key in reset.affected_keys)

    def test_display_order_stores_all_reasons(self):
        reasons = collect_exclusion_reasons(
            is_dummy=True, guideline="ERC2020", attempt_epoch=EPOCH,
            current_epoch=LATER, already_finalized=False,
        )
        assert reasons == EXCLUSION_REASON_ORDER
        classified = classify_launch_status(reasons)
        assert classified["status"] == "excluded"
        assert classified["exclusion_reasons"] == list(EXCLUSION_REASON_ORDER)
        assert classified["ok"] is False

    def test_already_finalized_is_not_retroactively_excluded(self):
        template = _template()
        stored = {
            "status": "disabled", "ok": False, "error": "arc_contract_pending", "exclusion_reasons": [],
        }
        job, attempt, user, rows = _rows(
            template, already=True, user_epoch=LATER, submit_arc=stored,
        )
        plan = CourseCompletionPlan().build(job, attempt, user, rows, _evaluation())
        submit = parse_json(plan.result_receipt_json)["submit_arc"]
        assert submit["status"] == "disabled"
        assert submit["exclusion_reasons"] == []
        assert "progress_reset_before_result" not in submit["exclusion_reasons"]

    def test_complete_and_incomplete_are_both_eligible_when_not_excluded(self):
        template = _template()
        plan = CourseCompletionPlan()
        complete = plan.build(*_rows(template), _evaluation())
        incomplete = plan.build(*_rows(template), _evaluation(observed=1, met=False, completed=False))
        for built in (complete, incomplete):
            submit = parse_json(built.result_receipt_json)["submit_arc"]
            assert submit == {
                "status": "disabled", "ok": False, "error": "arc_contract_pending", "exclusion_reasons": [],
            }
        assert parse_json(complete.result_receipt_json)["evaluation"]["program_completed"] is True
        assert parse_json(incomplete.result_receipt_json)["evaluation"]["program_completed"] is False


class TestV21DisabledGatewayAndIntent:
    def test_disabled_versus_excluded_and_stable_intent(self):
        template = _template()
        plan = CourseCompletionPlan()
        first = plan.build(*_rows(template), _evaluation())
        second = plan.build(*_rows(template), _evaluation())
        actions = parse_json(first.actions_json)
        assert actions["submit_queue"] == []
        assert actions["two_phase"] is False
        assert actions["intent"]["status"] == "disabled"
        expected = submission_intent_id(
            ATTEMPT, INPUT_DIGEST, parse_json(first.result_receipt_json)["result_digest"],
        )
        assert actions["intent"]["intent_id"] == expected
        assert parse_json(second.actions_json)["intent"]["intent_id"] == expected
        assert SUBMISSION_POLICY_VERSION == "vcc-submission-policy-v1"
        assert CONTRACT_VERSION == "vcc-internal-v1"
        dummy = plan.build(*_rows(template, is_dummy=True), _evaluation())
        assert parse_json(dummy.actions_json)["intent"]["status"] == "excluded"
        assert parse_json(dummy.actions_json)["intent"]["error"] is None

    def test_legacy_branch_has_no_course_control_writes(self):
        template = _template()
        job, attempt, user, rows = _rows(template)
        attempt = deepcopy(attempt)
        attempt.pop("course_binding")
        plan = CourseCompletionPlan().build(job, attempt, user, rows, _evaluation())
        actions = parse_json(plan.actions_json)
        assert actions["branch"] == "legacy"
        assert "COURSE_HEAD" not in _kinds(plan)
        assert "SUBMISSION" not in _kinds(plan)
        assert not any(key[0].startswith("COURSE#") or key[0].startswith("SUBMISSION#") for key in plan.affected_keys)

    def test_no_resume_token_or_queue(self):
        template = _template()
        plan = CourseCompletionPlan().build(*_rows(template), _evaluation())
        receipt = parse_json(plan.result_receipt_json)
        actions = parse_json(plan.actions_json)
        for blob in (receipt, actions):
            text = str(blob)
            for key in RECEIPT_FORBIDDEN_KEYS:
                assert key not in text
            assert "resume_credential" not in text
        assert actions["submit_queue"] == []
        assert "OUTBOX" not in _kinds(plan)
        current = CourseCompletionPlan().build(*_rows(template, active_counted=True), _evaluation())
        assert len(parse_json(current.actions_json)["writes"]) <= 20

    def test_disabled_gateway_is_zero_network(self, monkeypatch):
        def forbid(*_args, **_kwargs):
            raise AssertionError("network")

        monkeypatch.setattr("socket.socket", forbid)
        receipt = DisabledArcGateway().submit({"attempt_id": ATTEMPT, "payload": "must-not-send"})
        assert type(receipt) is ArcReceipt
        assert receipt.status == "disabled"
        assert receipt.receipt_id is None
        assert receipt.verified_payload_json is None
        with pytest.raises(CourseError):
            ArcReceipt("succeeded", "r1", None)

    def test_classify_helper_bytes_and_hooks(self):
        template = _template()
        body = classify_submission(
            template.binding, _evaluation(), is_dummy=False, current_epoch=EPOCH,
            attempt_epoch=EPOCH, guideline="ARC2025",
        )
        assert parse_json(body)["status"] == "disabled"
        assert "close_course_terminal" in HOOK_REQUESTS
        assert "reopen_course_recovery" in HOOK_REQUESTS
        assert "course_binding" in HOOK_REQUESTS
        assert "CourseCompletionPlan" in HOOK_REQUESTS
        assert "mark_calculation_saved" in HOOK_REQUESTS
        assert "two_phase" in HOOK_REQUESTS or "two-phase" in HOOK_REQUESTS.lower() or "Never two-phase" in HOOK_REQUESTS or "never Mock-slot" in HOOK_REQUESTS


class TestAtomicCourseProgress:
    def test_training_updates_public_progress_and_survives_next_aggregation(self):
        job, attempt, user, rows = _rows(_template())
        plan = CourseCompletionPlan().build(job, attempt, user, rows, _evaluation())
        actions = parse_json(plan.actions_json)
        head = next(write["set"] for write in actions["writes"] if write["kind"] == "COURSE_HEAD")
        progress = parse_json(head["progress_json"].encode())
        key = attempt["course_binding"].placement_key
        assert progress["items"][key]["completed"] is True
        assert head["completed_placements"] == progress["completed_placements"] == [key]
        assert head["course_complete"] is False
        # A later content report aggregates the saved JSON, not a separate copy.
        repeated = parse_json(CoursePolicy.aggregate_progress(rows["bundle"], json_bytes(progress)))
        assert repeated["items"][key]["completed"] is True
        assert actions["conditions"]["head_definition_hash"] == rows["head"]["definition_hash"]
        assert actions["conditions"]["final_active_attempt_id"] == ATTEMPT

    def test_final_success_completes_course_only_with_all_prerequisites(self):
        job, attempt, user, rows = _rows(_template(1005))
        bundle = rows["bundle"]
        progress = empty_progress()
        for p in bundle.placements[:-1]:
            key = placement_key(attempt["course_binding"].scope_key, p.source_id)
            progress["completed_placements"].append(key)
            progress["items"][key] = {
                "source_id": p.source_id, "public_link_id": p.public_link_id, "kind": p.kind,
                "content_version": p.content_version, "completed": True, "passed": None,
            }
        rows["head"]["progress_json"] = json_bytes(progress).decode()
        plan = CourseCompletionPlan().build(job, attempt, user, rows, _evaluation())
        writes = parse_json(plan.actions_json)["writes"]
        head = next(w["set"] for w in writes if w["kind"] == "COURSE_HEAD")
        final = next(w["set"] for w in writes if w["kind"] == "COURSE_FINAL")
        assert head["course_complete"] is True
        assert parse_json(head["progress_json"].encode())["course_status"] == "FINISHED"
        assert final["passed_placement_key"] == attempt["course_binding"].placement_key
        assert final["passed_definition_hash"] == attempt["course_binding"].definition_hash

    @pytest.mark.parametrize("same_placement", [False, True])
    def test_old_final_pass_is_retained_without_completing_replacement(self, same_placement):
        job, attempt, user, rows = _rows(_template(1005))
        original = rows["bundle"]
        last = original.placements[-1]
        replacement = replace(last, content_version="assessment-v2", **({} if same_placement else {
            "source_id": "replacement-assessment", "public_link_id": 2005,
        }))
        rows["bundle"] = sealed_bundle(replace(original, placements=original.placements[:-1] + (replacement,)))
        rows["head"]["definition_hash"] = rows["bundle"].definition_hash
        plan = CourseCompletionPlan().build(job, attempt, user, rows, _evaluation())
        writes = parse_json(plan.actions_json)["writes"]
        head = next(w["set"] for w in writes if w["kind"] == "COURSE_HEAD")
        final = next(w["set"] for w in writes if w["kind"] == "COURSE_FINAL")
        item = next(w["set"] for w in writes if w["kind"] == "COURSE_ITEM")
        replacement_key = placement_key(attempt["course_binding"].scope_key, replacement.source_id)
        public = parse_json(head["progress_json"].encode())
        assert item["completed"] is True
        assert final["phase"] == "passed" and final["passed_attempt_id"] == ATTEMPT
        assert public["items"][replacement_key]["passed"] is not True
        assert public["items"][replacement_key]["completed"] is False
        assert public["course_complete"] is False
        assert head["gate_state"] == "reconciliation_required"
        assert parse_json(plan.result_receipt_json)["progress_application"]["reason"] == "PROGRESS_RECONCILIATION_REQUIRED"

    def test_new_final_owner_cannot_be_released_by_old_result(self):
        job, attempt, user, rows = _rows(_template(1005))
        rows["final"]["active_attempt_id"] = "51000000-0000-4000-8000-000000000001"
        with pytest.raises(CourseError) as error:
            CourseCompletionPlan().build(job, attempt, user, rows, _evaluation(completed=False, met=False))
        assert error.value.code == "INVALID_STATE"

    @pytest.mark.parametrize("change_final,legacy", [(True, False), (False, False), (True, True)])
    def test_completion_uses_started_content_identity(self, change_final, legacy):
        job, attempt, user, rows = _rows(_template(1005))
        original = rows["bundle"]
        placements = list(original.placements)
        index = -1 if change_final else 0
        identity = parse_json(placements[index].content_identity_json)
        if change_final:
            identity["training_program_id"] = "replacement-program"
        else:
            identity["asset_ids"] = ["replacement-video"]
        placements[index] = replace(placements[index], content_identity_json=identity)
        rows["bundle"] = sealed_bundle(replace(original, placements=tuple(placements)))
        rows["head"]["definition_hash"] = rows["bundle"].definition_hash
        if legacy:
            attempt.pop("content_identity_hash")
        plan = CourseCompletionPlan().build(job, attempt, user, rows, _evaluation())
        writes = parse_json(plan.actions_json)["writes"]
        head = next(w["set"] for w in writes if w["kind"] == "COURSE_HEAD")
        final = next(w["set"] for w in writes if w["kind"] == "COURSE_FINAL")
        receipt = parse_json(plan.result_receipt_json)
        assert final["phase"] == "passed"
        assert receipt["progress_application"]["applied"] is (not change_final)
        if change_final:
            assert head["assessment_reconciliation_required"] is True
            assert receipt["progress_application"]["reason"] == "PROGRESS_RECONCILIATION_REQUIRED"

    def test_missing_verified_bundle_cannot_partially_finalize_course(self):
        job, attempt, user, rows = _rows(_template())
        rows.pop("bundle")
        with pytest.raises(CourseError) as error:
            CourseCompletionPlan().build(job, attempt, user, rows, _evaluation())
        assert error.value.code == "TEMPORARILY_UNAVAILABLE"

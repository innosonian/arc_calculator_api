"""V09/V11/V13/V15/V16 course policy unit tests. No DB, HTTP, or sleep."""

from copy import deepcopy
from pathlib import Path

import pytest

from mock_journey.course_contracts import (
    CONTRACT_VERSION, POLICY_VERSION, AssignmentBinding, ContentReport, CourseBinding, CourseBundle,
    CourseScope, CourseView, GateView, InventoryView, LearnerContext, Placement, PublicIds,
    StartCommand, definition_digest, learner_identity, parse_owned, sealed_bundle, scope_identity,
)
from mock_journey.course_errors import CourseError
from mock_journey.course_policy import (
    CoursePolicy, empty_progress, learner_key, merge_intervals, placement_key, scope_key,
)
from mock_journey.course_settings import fixture_course_settings
from mock_journey.typed import digest, json_bytes, parse_json


ROOT = Path(__file__).resolve().parents[1]
BUNDLE_DOC = parse_json((ROOT / "tests" / "fixtures" / "vcc_contract" / "v1" / "course_bundle.json").read_bytes())
EPOCH = "80000000-0000-4000-8000-000000000001"
REQUEST = "10000000-0000-4000-8000-000000000001"
START = "40000000-0000-4000-8000-000000000001"
REPORT_A = "20000000-0000-4000-8000-000000000001"
REPORT_B = "30000000-0000-4000-8000-000000000001"


def learner_from(doc):
    return LearnerContext(**doc)


def placement_from(doc, **overrides):
    payload = {
        "source_id": doc["source_id"],
        "public_link_id": doc["public_link_id"],
        "public_item_id": doc["public_item_id"],
        "position": doc["position"],
        "kind": doc["kind"],
        "content_version": doc["content_version"],
        "content_identity_json": deepcopy(doc["content_identity"]),
        "detail_json": deepcopy(doc["detail"]),
        "execution_json": deepcopy(doc["execution"]),
        "execution_status": doc["execution_status"],
        "duration_ms": doc["duration_ms"],
    }
    payload.update(overrides)
    return Placement(**payload)


def bundle_from(assignment, *, placements=None):
    learner = learner_from(BUNDLE_DOC["learners"]["real"])
    scope = CourseScope(learner, assignment["scope"][3], assignment["scope"][4])
    public = PublicIds(assignment["course_public_id"], assignment["enrollment_public_id"], assignment["progress_id"])
    items = placements if placements is not None else [placement_from(item) for item in BUNDLE_DOC["placements"]]
    return sealed_bundle(CourseBundle(
        scope=scope, public_ids=public, source_revision=assignment["source_revision"],
        mapping_version=BUNDLE_DOC["mapping_version"], definition_hash=assignment["definition_hash"],
        placements=items, course_json=deepcopy(BUNDLE_DOC["course_json"]),
        source_progress_json=deepcopy(BUNDLE_DOC["source_progress_json"]),
    ))


def assignment_doc(enrollment_public_id):
    return next(item for item in BUNDLE_DOC["assignments"] if item["enrollment_public_id"] == enrollment_public_id)


def policy():
    return CoursePolicy(fixture_course_settings())


def view_for(bundle, *, inventory_state="ready", gate_state="ready", progress=None, extra_assignments=()):
    assignment = AssignmentBinding(bundle.scope, bundle.public_ids)
    reason = None
    if inventory_state == "waiting":
        reason = "arc_progress_unavailable"
    elif inventory_state == "reconciliation_required":
        reason = "progress_reconciliation_required"
    inventory = InventoryView(
        learner_key(bundle.scope.learner), EPOCH, 1, 1, inventory_state, reason,
        (assignment,) + tuple(extra_assignments),
    )
    gate_reason = None
    if gate_state == "waiting":
        gate_reason = "arc_progress_unavailable"
    elif gate_state == "reconciliation_required":
        gate_reason = "progress_reconciliation_required"
    gate = GateView(
        scope_key(bundle.scope), EPOCH, gate_state, gate_reason, 1,
        bundle.definition_hash if gate_state == "ready" else None,
    )
    payload = empty_progress() if progress is None else progress
    return CourseView(scope_key(bundle.scope), bundle.public_ids, bundle, json_bytes(payload), gate, inventory)


def command(bundle, placement_id, request_id=REQUEST):
    return StartCommand(request_id, bundle.public_ids.enrollment_id, bundle.public_ids.course_id, placement_id, bundle.definition_hash)


def item_key(bundle, public_link_id):
    item = next(row for row in bundle.placements if row.public_link_id == public_link_id)
    return placement_key(scope_key(bundle.scope), item.source_id)


def completed_progress(bundle, *link_ids, final=None, evaluation=None):
    payload = empty_progress()
    keys = [item_key(bundle, link) for link in link_ids]
    payload["completed_placements"] = keys
    payload["items"] = {
        item_key(bundle, item.public_link_id): {
            "source_id": item.source_id,
            "public_link_id": item.public_link_id,
            "kind": item.kind,
            "content_version": item.content_version,
            "completed": item.public_link_id in link_ids,
            "passed": True if item.kind == "assessment" and item.public_link_id in link_ids else (False if item.kind == "training" and item.public_link_id in link_ids else None),
        }
        for item in bundle.placements
    }
    if final is not None:
        payload["final"].update(final)
    if evaluation is not None:
        payload["evaluation"] = evaluation
    return payload


class TestContractAndKeys:
    def test_versions_and_w0_digests(self):
        assert CONTRACT_VERSION == "vcc-internal-v1"
        assert POLICY_VERSION == "vcc-policy-v1"
        bundle = bundle_from(assignment_doc(501))
        assert learner_key(bundle.scope.learner) == BUNDLE_DOC["learner_key"]
        assert digest(learner_identity(bundle.scope.learner)) == BUNDLE_DOC["learner_key"]
        assert scope_key(bundle.scope) == assignment_doc(501)["scope_key"]
        assert digest(scope_identity(bundle.scope)) == assignment_doc(501)["scope_key"]
        assert definition_digest(bundle) == assignment_doc(501)["definition_hash"]
        place = placement_key(scope_key(bundle.scope), "src-place-1003")
        assert place != placement_key(scope_key(bundle.scope), "src-place-1004")
        other = bundle_from(assignment_doc(502))
        assert scope_key(other.scope) != scope_key(bundle.scope)
        assert placement_key(scope_key(other.scope), "src-place-1003") != place


class TestV09Isolation:
    def test_enrollment_501_placement_1003_does_not_complete_1004_or_502(self):
        first = bundle_from(assignment_doc(501))
        second = bundle_from(assignment_doc(502))
        progress = completed_progress(first, 1003)
        aggregated = parse_json(policy().aggregate_progress(first, json_bytes(progress)))
        key_1003 = item_key(first, 1003)
        key_1004 = item_key(first, 1004)
        assert aggregated["items"][key_1003]["completed"] is True
        assert aggregated["items"][key_1004]["completed"] is False
        assert aggregated["course_complete"] is False
        other = parse_json(policy().aggregate_progress(second, json_bytes(empty_progress())))
        assert other["completed_placements"] == []
        assert item_key(second, 1003) not in aggregated["completed_placements"]
        assert item_key(second, 1003) != key_1003

    def test_can_start_1004_after_1003_complete(self):
        bundle = bundle_from(assignment_doc(501))
        current = view_for(bundle, progress=completed_progress(bundle, 1003))
        policy().can_start(current, command(bundle, 1004), "training")
        with pytest.raises(CourseError) as raised:
            policy().can_start(current, command(bundle, 1003), "training")
        assert raised.value.code == "ITEM_ALREADY_COMPLETED"
        assert raised.value.status == 409


class TestV11FinalAndOnly:
    def test_prerequisites_block_final(self):
        bundle = bundle_from(assignment_doc(501))
        current = view_for(bundle)
        with pytest.raises(CourseError) as raised:
            policy().can_start(current, command(bundle, 1005), "final_assessment")
        assert raised.value.code == "PREREQUISITES_NOT_COMPLETED"

    def test_all_prior_complete_allows_final_when_free(self):
        bundle = bundle_from(assignment_doc(501))
        progress = completed_progress(bundle, 1001, 1002, 1003, 1004, final={"phase": "free"})
        policy().can_start(view_for(bundle, progress=progress), command(bundle, 1005), "final_assessment")

    def test_active_and_recovery_and_passed_and_pending_policy(self):
        bundle = bundle_from(assignment_doc(501))
        prior = completed_progress(bundle, 1001, 1002, 1003, 1004)
        cases = [
            ({"phase": "active"}, "FINAL_ASSESSMENT_ACTIVE"),
            ({"phase": "recovery_required"}, "FINAL_ASSESSMENT_RECOVERY_REQUIRED"),
            ({"phase": "policy_pending"}, "COMPLETION_POLICY_PENDING"),
            ({"phase": "passed"}, "ASSESSMENT_ALREADY_PASSED"),
        ]
        for final, code in cases:
            progress = deepcopy(prior)
            progress["final"].update(final)
            with pytest.raises(CourseError) as raised:
                policy().can_start(view_for(bundle, progress=progress), command(bundle, 1005), "final_assessment")
            assert raised.value.code == code
        pending = deepcopy(prior)
        pending["evaluation"] = {
            "goal": {"kind": "cycles", "required": 3, "observed": None, "met": None, "status": "pending_policy"},
            "score": {"decision": "pass"},
            "program_completed": False,
        }
        with pytest.raises(CourseError) as raised:
            policy().can_start(view_for(bundle, progress=pending), command(bundle, 1005), "final_assessment")
        assert raised.value.code == "COMPLETION_POLICY_PENDING"

    def test_evaluated_false_allows_retry_true_blocks(self):
        bundle = bundle_from(assignment_doc(501))
        prior = completed_progress(bundle, 1001, 1002, 1003, 1004)
        retry = deepcopy(prior)
        retry["evaluation"] = {
            "goal": {"kind": "ventilations", "required": 8, "observed": 4, "met": False, "status": "evaluated"},
            "score": {"decision": "fail"},
            "program_completed": False,
        }
        retry["final"]["phase"] = "free"
        policy().can_start(view_for(bundle, progress=retry), command(bundle, 1005), "final_assessment")
        passed = deepcopy(prior)
        passed["evaluation"] = {
            "goal": {"kind": "ventilations", "required": 8, "observed": 8, "met": True, "status": "evaluated"},
            "score": {"decision": "pass"},
            "program_completed": True,
        }
        with pytest.raises(CourseError) as raised:
            policy().can_start(view_for(bundle, progress=passed), command(bundle, 1005), "final_assessment")
        assert raised.value.code == "ASSESSMENT_ALREADY_PASSED"

    def test_only_60_observed_59_score_pass_is_free(self):
        bundle = bundle_from(assignment_doc(501))
        prior = completed_progress(bundle, 1001, 1002, 1003, 1004)
        only = deepcopy(prior)
        only["evaluation"] = {
            "goal": {"kind": "compressions", "required": 60, "observed": 59, "met": False, "status": "evaluated"},
            "score": {"decision": "pass"},
            "program_completed": False,
        }
        only["final"]["phase"] = "active"
        only["active_counted"] = False
        only["lease_seconds"] = 0
        only["app_wait_seconds"] = 30
        with pytest.raises(CourseError) as raised:
            policy().can_start(view_for(bundle, progress=only), command(bundle, 1005), "final_assessment")
        assert raised.value.code == "FINAL_ASSESSMENT_ACTIVE"
        # The evaluated failure permits retry only after finalize releases its
        # own role atomically; timeouts or a stale evaluation cannot release it.
        only["final"]["phase"] = "free"
        policy().can_start(view_for(bundle, progress=only), command(bundle, 1005), "final_assessment")

    def test_disabled_submit_arc_is_not_a_start_gate(self):
        bundle = bundle_from(assignment_doc(501))
        prior = completed_progress(bundle, 1001, 1002, 1003, 1004)
        prior["submit_arc"] = {"status": "disabled", "ok": False, "error": "arc_contract_pending", "exclusionReasons": []}
        policy().can_start(view_for(bundle, progress=prior), command(bundle, 1005), "final_assessment")

    def test_gate_and_hash_and_execution(self):
        bundle = bundle_from(assignment_doc(501))
        waiting = view_for(bundle, inventory_state="waiting")
        with pytest.raises(CourseError) as raised:
            policy().can_start(waiting, command(bundle, 1003), "training")
        assert raised.value.code == "ARC_PROGRESS_UNAVAILABLE"
        gate_wait = view_for(bundle, gate_state="waiting")
        with pytest.raises(CourseError) as raised:
            policy().can_start(gate_wait, command(bundle, 1003), "training")
        assert raised.value.code == "ARC_PROGRESS_UNAVAILABLE"
        changed = StartCommand(REQUEST, bundle.public_ids.enrollment_id, bundle.public_ids.course_id, 1003, "ab" * 32)
        with pytest.raises(CourseError) as raised:
            policy().can_start(view_for(bundle), changed, "training")
        assert raised.value.code == "DEFINITION_CHANGED"


class TestV15VideoUnion:
    def test_full_coverage_without_button(self):
        evidence = {
            "start_id": START, "public_link_id": 1001, "kind": "video",
            "content_version": "video-v1", "duration_ms": 20000, "merged_intervals_ms": [],
            "report_count": 0, "completed": False, "course_status": "IN_PROGRESS",
        }
        report = ContentReport(REPORT_B, START, "video-v1", "video_segments", [[0, 10000], [10000, 20000]])
        receipt = parse_json(policy().evaluate_content(json_bytes(evidence), report))
        assert receipt["isCompleted"] is True
        assert receipt["isPassed"] is None
        assert receipt["application"] == "applied"
        assert receipt["courseStatus"] == "IN_PROGRESS"

    def test_repeats_overlap_gap_and_end_only(self):
        base = {
            "start_id": START, "public_link_id": 1001, "kind": "video",
            "content_version": "video-v1", "duration_ms": 20000, "report_count": 0,
            "completed": False, "course_status": "IN_PROGRESS",
        }
        cases = [
            ([[0, 10000], [0, 10000]], False),
            ([[0, 10000], [5000, 15000]], False),
            ([[0, 10000], [10001, 20000]], False),
            ([[19999, 20000]], False),
            ([[0, 10000], [10000, 20000]], True),
        ]
        for intervals, complete in cases:
            evidence = {**base, "merged_intervals_ms": []}
            report = ContentReport(REPORT_B, START, "video-v1", "video_segments", intervals)
            receipt = parse_json(policy().evaluate_content(json_bytes(evidence), report))
            assert receipt["isCompleted"] is complete
        assert merge_intervals([[0, 10000], [10000, 20000], [0, 10000]]) == [[0, 20000]]
        assert merge_intervals([[0, 10000], [10001, 20000]]) == [[0, 10000], [10001, 20000]]


class TestV16DocumentAndHistorical:
    def test_confirm_first_then_display(self):
        evidence = {
            "start_id": START, "public_link_id": 1002, "kind": "document",
            "content_version": "document-v1", "displayed": [], "pending_confirmations": [],
            "report_count": 0, "completed": False, "course_status": "IN_PROGRESS",
        }
        confirm = ContentReport(REPORT_B, START, "document-v1", "document_confirmed", display_report_id=REPORT_A)
        first = parse_json(policy().evaluate_content(json_bytes(evidence), confirm))
        assert first["application"] == "pending_evidence"
        assert first["isCompleted"] is False
        after = dict(evidence)
        after["pending_confirmations"] = [{
            "report_id": REPORT_B, "display_report_id": REPORT_A, "content_version": "document-v1",
        }]
        display = ContentReport(REPORT_A, START, "document-v1", "document_displayed")
        second = parse_json(policy().evaluate_content(json_bytes(after), display))
        assert second["isCompleted"] is True
        assert second["application"] == "applied"

    def test_display_then_confirm_and_foreign_display_rejected(self):
        evidence = {
            "start_id": START, "public_link_id": 1002, "kind": "document",
            "content_version": "document-v1",
            "displayed": [{"report_id": REPORT_A, "content_version": "document-v1"}],
            "pending_confirmations": [], "report_count": 1, "completed": False, "course_status": "IN_PROGRESS",
        }
        confirm = ContentReport(REPORT_B, START, "document-v1", "document_confirmed", display_report_id=REPORT_A)
        receipt = parse_json(policy().evaluate_content(json_bytes(evidence), confirm))
        assert receipt["isCompleted"] is True
        foreign = ContentReport(REPORT_B, START, "document-v1", "document_confirmed", display_report_id="21000000-0000-4000-8000-000000000099")
        pending = parse_json(policy().evaluate_content(json_bytes(evidence), foreign))
        assert pending["application"] == "pending_evidence"
        assert pending["isCompleted"] is False

    def test_historical_only_nulls(self):
        evidence = {
            "start_id": START, "public_link_id": 1001, "kind": "video",
            "content_version": "video-v1", "duration_ms": 20000, "historical_only": True,
            "merged_intervals_ms": [[0, 20000]], "completed": True, "course_status": "FINISHED",
        }
        report = ContentReport(REPORT_B, START, "video-v1", "video_segments", [[0, 20000]])
        receipt = parse_json(policy().evaluate_content(json_bytes(evidence), report))
        assert receipt["application"] == "historical_only"
        assert receipt["isCompleted"] is None
        assert receipt["isPassed"] is None
        assert receipt["courseStatus"] is None

    def test_content_version_mismatch(self):
        evidence = {
            "start_id": START, "public_link_id": 1001, "kind": "video",
            "content_version": "video-v1", "duration_ms": 20000, "merged_intervals_ms": [],
            "report_count": 0, "completed": False, "course_status": "IN_PROGRESS",
        }
        report = ContentReport(REPORT_B, START, "video-v2", "video_segments", [[0, 20000]])
        with pytest.raises(CourseError) as raised:
            policy().evaluate_content(json_bytes(evidence), report)
        assert raised.value.code == "CONTENT_VERSION_MISMATCH"


class TestV13SnapshotVersusNewVersion:
    def test_existing_completion_kept_and_pass_not_copied(self):
        bundle = bundle_from(assignment_doc(501))
        progress = completed_progress(bundle, 1001, 1002, 1003, 1004)
        progress["items"][item_key(bundle, 1001)]["content_version"] = "video-v1"
        progress["final"]["phase"] = "passed"
        progress["passed_final"] = True
        progress["evaluation"] = {
            "goal": {"kind": "ventilations", "required": 8, "observed": 8, "met": True, "status": "evaluated"},
            "score": {"decision": "pass"},
            "program_completed": True,
        }
        placements = []
        for item in BUNDLE_DOC["placements"]:
            if item["public_link_id"] == 1005:
                changed = deepcopy(item)
                changed["content_version"] = "assessment-v2"
                placements.append(placement_from(changed))
            else:
                placements.append(placement_from(item))
        revised = bundle_from(assignment_doc(501), placements=placements)
        assert definition_digest(revised) != definition_digest(bundle)
        aggregated = parse_json(policy().aggregate_progress(revised, json_bytes(progress)))
        assert aggregated["items"][item_key(revised, 1001)]["completed"] is True
        assert aggregated["items"][item_key(revised, 1001)]["passed"] is None
        assert aggregated["items"][item_key(revised, 1005)]["completed"] is False
        assert aggregated["items"][item_key(revised, 1005)]["passed"] is None
        assert aggregated["final"]["phase"] == "passed"
        assert aggregated["course_status"] != "CERTIFIED"
        assert aggregated["course_complete"] is False
        with pytest.raises(CourseError) as raised:
            policy().can_start(view_for(revised, progress=progress), command(revised, 1005), "final_assessment")
        assert raised.value.code == "ASSESSMENT_ALREADY_PASSED"
        # Same enrollment FINAL pass still blocks even if the last placement version changed.
        matching = view_for(bundle, progress=progress)
        with pytest.raises(CourseError) as raised:
            policy().can_start(matching, command(bundle, 1005), "final_assessment")
        assert raised.value.code == "ASSESSMENT_ALREADY_PASSED"

    def test_pending_reconciliation_does_not_erase_completion(self):
        evidence = {
            "start_id": START, "public_link_id": 1001, "kind": "video",
            "content_version": "video-v1", "current_content_version": "video-v2",
            "duration_ms": 20000, "merged_intervals_ms": [[0, 20000]],
            "report_count": 1, "completed": True, "course_status": "IN_PROGRESS",
        }
        report = ContentReport(REPORT_B, START, "video-v1", "video_segments", [[0, 10000]])
        receipt = parse_json(policy().evaluate_content(json_bytes(evidence), report))
        assert receipt["application"] == "pending_reconciliation"
        assert receipt["isCompleted"] is True


class TestAggregateAndClassify:
    def test_old_pass_cannot_complete_replacement_with_reused_public_link(self):
        from dataclasses import replace
        bundle = bundle_from(assignment_doc(501))
        progress = completed_progress(bundle, 1001, 1002, 1003, 1004, 1005, final={"phase": "passed"})
        replacement = replace(bundle.placements[-1], source_id="new-source-assessment")
        revised = sealed_bundle(replace(bundle, placements=bundle.placements[:-1] + (replacement,)))
        aggregated = parse_json(policy().aggregate_progress(revised, json_bytes(progress)))
        current = aggregated["items"][item_key(revised, 1005)]
        assert current["completed"] is False
        assert current["passed"] is None
        assert aggregated["final"]["phase"] == "passed"
        assert aggregated["course_complete"] is False
        with pytest.raises(CourseError) as raised:
            policy().can_start(view_for(revised, progress=aggregated), command(revised, 1005), "final_assessment")
        assert raised.value.code == "ASSESSMENT_ALREADY_PASSED"

    def test_finished_requires_all_items_and_final_pass(self):
        bundle = bundle_from(assignment_doc(501))
        partial = completed_progress(bundle, 1001, 1002, 1003, 1004, 1005, final={"phase": "passed"}, evaluation={
            "goal": {"kind": "ventilations", "required": 8, "observed": 8, "met": True, "status": "evaluated"},
            "score": {"decision": "pass"},
            "program_completed": True,
        })
        done = parse_json(policy().aggregate_progress(bundle, json_bytes(partial)))
        assert done["course_status"] == "FINISHED"
        assert done["course_complete"] is True
        missing = completed_progress(bundle, 1005, final={"phase": "passed"}, evaluation={
            "goal": {"kind": "ventilations", "required": 8, "observed": 8, "met": True, "status": "evaluated"},
            "score": {"decision": "pass"},
            "program_completed": True,
        })
        missing["items"][item_key(bundle, 1001)]["completed"] = False
        missing["completed_placements"] = [item_key(bundle, 1005)]
        mixed = parse_json(policy().aggregate_progress(bundle, json_bytes(missing)))
        assert mixed["course_complete"] is False
        assert mixed["course_status"] == "IN_PROGRESS"
        assert mixed["items"][item_key(bundle, 1001)]["completed"] is False
        assert mixed["items"][item_key(bundle, 1001)]["passed"] is None

    def test_classify_submission_matches_d90_d92(self):
        bundle = bundle_from(assignment_doc(501))
        binding = CourseBinding(
            scope_key(bundle.scope), item_key(bundle, 1005), "final_assessment",
            bundle.definition_hash, "assessment-v1", EPOCH, POLICY_VERSION,
        )
        result = {"condition": {"guideline": "ARC2025"}, "program_completed": True}
        disabled = parse_json(policy().classify_submission(binding, json_bytes(result), is_dummy=False, current_epoch=EPOCH))
        assert disabled == {"status": "disabled", "ok": False, "error": "arc_contract_pending", "exclusionReasons": []}
        dummy = parse_json(policy().classify_submission(binding, json_bytes(result), is_dummy=True, current_epoch=EPOCH))
        assert dummy["status"] == "excluded" and dummy["ok"] is False and dummy["error"] is None
        assert dummy["exclusionReasons"] == ["dummy"]
        non_arc = parse_json(policy().classify_submission(
            binding, json_bytes({"condition": {"guideline": "AHA2020"}}), is_dummy=False, current_epoch=EPOCH,
        ))
        assert non_arc["exclusionReasons"] == ["non_arc_guideline"]
        reset = parse_json(policy().classify_submission(
            binding, json_bytes(result), is_dummy=False, current_epoch="90000000-0000-4000-8000-000000000001",
        ))
        assert reset["exclusionReasons"] == ["progress_reset_before_result"]
        stacked = parse_json(policy().classify_submission(
            binding, json_bytes({"condition": {"guideline": "ERC2020"}}), is_dummy=True,
            current_epoch="90000000-0000-4000-8000-000000000001",
        ))
        assert stacked["exclusionReasons"] == ["dummy", "non_arc_guideline", "progress_reset_before_result"]
        assert stacked["ok"] is False

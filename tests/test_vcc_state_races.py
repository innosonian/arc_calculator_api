"""Deterministic commit schedules shared with the real DynamoDB acceptance tests."""

from dataclasses import replace
import uuid

import pytest

from mock_journey.course_contracts import ContentReport, sealed_bundle
from mock_journey.course_errors import CourseError
from mock_journey.course_policy import learner_key
from mock_journey.storage_keys import course_head_key, course_report_key, course_start_key
from mock_journey.typed import parse_json
from tests.vcc_state_races_support import (  # noqa: F401 (re-export)
    _new_inventory, _once_before_transaction, _report, _reset, _start, assert_document_evidence_capacity_is_atomic,
    assert_first_final_initializes_item, assert_identity_change_does_not_reuse_unfinished_content,
    assert_new_definition_wins_report, assert_new_refresh_generation_survives_report,
    assert_parent_generation_wins_refresh, assert_public_id_remap_cannot_retarget_prepared_start,
    assert_reassigned_public_ids_do_not_authorize_old_scope, assert_refresh_replacement_preserves_pass_lock,
    assert_reset_wins_report, assert_revocation_wins_start, provision_race,
)
from tests.vcc_state_support import EPOCH, REPORT_A, binding_of, repository


@pytest.fixture
def race_context():
    repo, store, _ = repository(uuids=lambda: str(uuid.uuid4()))
    return provision_race(repo, store)


def test_reassigned_public_ids_do_not_authorize_old_scope(race_context):
    assert_reassigned_public_ids_do_not_authorize_old_scope(race_context)


@pytest.mark.parametrize("kind", ["content", "attempt"])
def test_public_id_remap_cannot_retarget_prepared_start(race_context, kind):
    assert_public_id_remap_cannot_retarget_prepared_start(race_context, kind=kind)


def test_document_evidence_capacity_is_atomic(race_context):
    assert_document_evidence_capacity_is_atomic(race_context)


def test_new_refresh_generation_is_not_reverted_by_report(race_context):
    assert_new_refresh_generation_survives_report(race_context)


def test_first_final_initializes_item(race_context):
    assert_first_final_initializes_item(race_context)


@pytest.mark.parametrize("source", [
    {}, {"synthetic": False, "progress": None}, {"progress": False},
    {"progress": {"completed_placements": []}},
    {"progress": {"completed_placements": ["unknown-placement"]}},
])
def test_unknown_source_progress_keeps_reconciliation_gate(race_context, source):
    repo, store, auth, bundle, view, _ = race_context
    updated = sealed_bundle(replace(bundle, source_progress_json=source))
    ticket = _new_inventory(repo, auth, updated)
    refresh = repo.begin_refresh(auth, binding_of(updated), ticket)
    gate = repo.apply_refresh(auth, refresh, updated)
    assert gate.state == "reconciliation_required"
    assert gate.reason == "progress_reconciliation_required"
    assert store.get_item(course_head_key(view.scope_key, EPOCH))["completed_placements"] == []


def test_pre_resolve_generation_uses_stored_binding(race_context):
    repo, _, auth, bundle, _, _ = race_context
    ticket = repo.begin_inventory_for_session(auth)
    assert ticket.learner_key == learner_key(bundle.scope.learner)
    assert ticket.generation == 2
    failed = repo.apply_inventory(auth, ticket, CourseError("CONTRACT_PENDING"))
    assert failed.state == "waiting"
    assert failed.reason == "contract_pending"


@pytest.mark.parametrize("replace_assessment", [False, True])
def test_refresh_after_pass_reconciles_only_replaced_assessment(race_context, replace_assessment):
    assert_refresh_replacement_preserves_pass_lock(race_context, replace_assessment=replace_assessment)


def test_unfinished_content_identity_change_does_not_inherit_old_report(race_context):
    assert_identity_change_does_not_reuse_unfinished_content(race_context)


def test_assessment_identity_change_does_not_inherit_pass(race_context):
    assert_refresh_replacement_preserves_pass_lock(race_context, replace_assessment=True, identity_only=True)


def test_json_key_order_is_not_an_assessment_replacement(race_context):
    repo, store, auth, bundle, view, _ = race_context
    assert_refresh_replacement_preserves_pass_lock(race_context, replace_assessment=False)
    current = repo.load_view(auth, bundle.public_ids).bundle
    last = current.placements[-1]
    identity = dict(reversed(list(parse_json(last.content_identity_json).items())))
    execution = dict(reversed(list(parse_json(last.execution_json).items())))
    changed = sealed_bundle(replace(current, placements=current.placements[:-1] + (
        replace(last, content_identity_json=identity, execution_json=execution),
    )))
    assert changed.definition_hash == current.definition_hash
    ticket = _new_inventory(repo, auth, changed)
    refresh = repo.begin_refresh(auth, binding_of(changed), ticket)
    assert repo.apply_refresh(auth, refresh, changed).state == "ready"
    assert store.get_item(course_head_key(view.scope_key, EPOCH))["course_complete"] is True


def test_revocation_wins_start(race_context):
    assert_revocation_wins_start(race_context)


@pytest.mark.parametrize("failure", [False, True])
def test_parent_generation_wins_refresh(race_context, failure):
    assert_parent_generation_wins_refresh(race_context, failure=failure)


def test_reset_wins_report(race_context):
    assert_reset_wins_report(race_context)


def test_definition_wins_report(race_context):
    assert_new_definition_wins_report(race_context)


def test_committed_report_replays_unchanged_after_reset(race_context):
    _, store, auth, _, view, _ = race_context
    started = _start(race_context)
    before = _report(race_context, started.start_id)
    _reset(store, auth)
    after = _report(race_context, started.start_id)
    assert after.response_json == before.response_json
    assert parse_json(after.response_json)["application"] == "applied"


@pytest.mark.parametrize("change,expected", [
    ({"content_version": "different"}, "CONTENT_VERSION_MISMATCH"),
    ({"event_type": "document_displayed", "intervals_ms": ()}, "INVALID_REQUEST"),
    ({"intervals_ms": ((0, 20001),)}, "INVALID_REQUEST"),
    ({"report_count": 4096}, "PROGRESS_CAPACITY_EXCEEDED"),
])
def test_historical_reports_still_validate_and_enforce_capacity(race_context, change, expected):
    repo, store, auth, _, view, _ = race_context
    started = _start(race_context)
    _reset(store, auth)
    if "report_count" in change:
        start = store.get_item(course_start_key(view.scope_key, EPOCH, started.start_id))
        start["report_count"] = change["report_count"]
        store.seed(start)
        change = {}
    report = replace(ContentReport(REPORT_A, started.start_id, "video-v1", "video_segments", ((0, 20000),)), **change)
    with pytest.raises(CourseError) as raised:
        repo.report(auth, course_id=101, enrollment_id=501, placement_id=1001, report=report)
    assert raised.value.code == expected
    assert store.get_item(course_report_key(view.scope_key, EPOCH, started.start_id, REPORT_A)) is None

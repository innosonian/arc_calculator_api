"""The audited race schedules run through real DynamoDB transaction conditions."""

import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier, local

import pytest

from mock_journey.course_policy import CoursePolicy
from mock_journey.course_contracts import AttemptTemplate, CourseBinding, POLICY_VERSION, StartCommand
from mock_journey.course_errors import CourseError
from mock_journey.course_policy import placement_key
from mock_journey.course_settings import fixture_course_settings
from mock_journey.course_state import DynamoCourseRepository, InMemoryBlobStore, _final_key, _head_key, _session_key, _user_key
from mock_journey.state import DynamoCourseStore, DynamoStateRepository, _encode
from tests.test_vcc_state_races import (
    assert_new_definition_wins_report, assert_parent_generation_wins_refresh,
    assert_reset_wins_report, assert_revocation_wins_start, assert_first_final_initializes_item, provision_race,
    assert_refresh_replacement_preserves_pass_lock,
    assert_new_refresh_generation_survives_report,
    assert_reassigned_public_ids_do_not_authorize_old_scope,
    assert_identity_change_does_not_reuse_unfinished_content,
    assert_public_id_remap_cannot_retarget_prepared_start,
    assert_document_evidence_capacity_is_atomic,
)
from tests.test_vcc_state import EPOCH
from tests.test_vcc_policy import completed_progress
from mock_journey.typed import json_bytes


@pytest.fixture
def race_context(dynamodb_client, dynamodb_table):
    class SeedableCourseStore(DynamoCourseStore):
        def seed(self, item):
            dynamodb_client.put_item(TableName=dynamodb_table, Item=_encode(item))

    store = SeedableCourseStore(DynamoStateRepository(
        dynamodb_client, dynamodb_table, clock=lambda: 1_000_000,
    ))
    settings = fixture_course_settings()
    repo = DynamoCourseRepository(
        store, settings, CoursePolicy(settings), InMemoryBlobStore(),
        clock=lambda: 1_000_000, uuid_factory=lambda: str(uuid.uuid4()),
    )
    return provision_race(repo, store)


def test_revocation_commit_before_start(race_context):
    assert_revocation_wins_start(race_context)


def test_reassigned_public_ids_do_not_authorize_old_scope(race_context):
    assert_reassigned_public_ids_do_not_authorize_old_scope(race_context)


@pytest.mark.parametrize("kind", ["content", "attempt"])
def test_public_id_remap_cannot_retarget_prepared_start(race_context, kind):
    assert_public_id_remap_cannot_retarget_prepared_start(race_context, kind=kind)


def test_document_evidence_capacity_is_atomic(race_context):
    assert_document_evidence_capacity_is_atomic(race_context)


@pytest.mark.parametrize("failure", [False, True])
def test_inventory_commit_before_old_refresh(race_context, failure):
    assert_parent_generation_wins_refresh(race_context, failure=failure)


def test_reset_commit_before_report(race_context):
    assert_reset_wins_report(race_context)


def test_definition_commit_before_report(race_context):
    assert_new_definition_wins_report(race_context)


def test_new_refresh_generation_is_not_reverted_by_report(race_context):
    assert_new_refresh_generation_survives_report(race_context)


def test_first_final_creates_item_and_role_atomically(race_context):
    assert_first_final_initializes_item(race_context)


def test_two_sessions_reach_final_start_transaction_together(race_context):
    repo, store, auth, bundle, view, _ = race_context
    other_id = str(uuid.uuid4())
    session = store.get_item(_session_key(auth.session_id))
    store.seed({**session, **_session_key(other_id), "session_id": other_id})
    other_auth = replace(auth, session_id=other_id)
    head = store.get_item(_head_key(view.scope_key, EPOCH))
    progress = completed_progress(bundle, 1001, 1002, 1003, 1004)
    store.seed({**head, "progress_json": json_bytes(progress).decode(),
                "completed_placements": progress["completed_placements"]})
    placement = bundle.placements[-1]
    commands = [StartCommand(str(uuid.uuid4()), 501, 101, 1005, bundle.definition_hash) for _ in range(2)]
    templates = [AttemptTemplate({"attempt_id": str(uuid.uuid4()), "definition_json": placement.execution_json.decode()}, CourseBinding(
        view.scope_key, placement_key(view.scope_key, placement.source_id), "final_assessment",
        bundle.definition_hash, placement.content_version, EPOCH, POLICY_VERSION,
    )) for _ in range(2)]
    snapshots = [repo.load_start_view(owner, command)
                 for owner, command in zip((auth, other_auth), commands)]
    original, barrier, per_thread = store.transact, Barrier(2), local()

    def transact(actions):
        if not getattr(per_thread, "started", False):
            per_thread.started = True
            barrier.wait(timeout=10)
        return original(actions)

    def begin(index):
        try:
            receipt = repo.start((auth, other_auth)[index], commands[index], kind="attempt",
                                 view=snapshots[index], template=templates[index])
            return "created", receipt.attempt_id
        except CourseError as error:
            return error.code, None

    store.transact = transact
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(begin, (0, 1)))
    finally:
        store.transact = original
    assert sorted(row[0] for row in results) == ["FINAL_ASSESSMENT_ACTIVE", "created"]
    winner = next(row[1] for row in results if row[0] == "created")
    final = store.get_item(_final_key(view.scope_key, EPOCH))
    assert final["phase"] == "active" and final["active_attempt_id"] == winner
    receipts = [repo.find_created(owner, command, kind="attempt")
                for owner, command in zip((auth, other_auth), commands)]
    assert sum(receipt is not None for receipt in receipts) == 1


@pytest.mark.parametrize("replace_assessment", [False, True])
def test_refresh_after_pass_preserves_scope_of_evidence(race_context, replace_assessment):
    assert_refresh_replacement_preserves_pass_lock(race_context, replace_assessment=replace_assessment)


def test_assessment_identity_change_does_not_inherit_pass(race_context):
    assert_refresh_replacement_preserves_pass_lock(race_context, replace_assessment=True, identity_only=True)


def test_unfinished_content_identity_change_does_not_inherit_old_report(race_context):
    assert_identity_change_does_not_reuse_unfinished_content(race_context)


def test_cancel_retry_does_not_release_new_final(race_context):
    repo, store, auth, _, view, _ = race_context
    first = assert_first_final_initializes_item(race_context)
    state = store._state
    original = state._write
    fired = False
    next_attempt = []

    def write(actions):
        nonlocal fired
        if not fired:
            fired = True
            # The competing request cancels A and starts B before the original
            # A cancellation commits. Its retry must be an idempotent no-op.
            state.cancel_attempt(auth, first.attempt_id, "other_request")
            next_attempt.append(assert_first_final_initializes_item(race_context).attempt_id)
        return original(actions)

    state._write = write
    state.cancel_attempt(auth, first.attempt_id, "original_request")
    assert state.get_attempt(auth, first.attempt_id)["state"] == "cancelled"
    final = store.get_item(_final_key(view.scope_key, EPOCH))
    assert final["phase"] == "active"
    assert final["active_attempt_id"] == next_attempt[0]


def test_reset_wins_cancel_without_changing_old_course_rows(race_context):
    _, store, auth, _, view, _ = race_context
    first = assert_first_final_initializes_item(race_context)
    state = store._state
    original = state._write
    fired = False
    old_head = store.get_item(_head_key(view.scope_key, EPOCH))
    old_final = store.get_item(_final_key(view.scope_key, EPOCH))
    new_epoch = str(uuid.uuid4())

    def write(actions):
        nonlocal fired
        if not fired:
            fired = True
            user = store.get_item(_user_key(auth.principal))
            store.seed({**user, "epoch": new_epoch, "revision": user["revision"] + 1})
        return original(actions)

    state._write = write
    state.cancel_attempt(auth, first.attempt_id, "test_reset")
    assert state.get_attempt(auth, first.attempt_id)["state"] == "cancelled"
    assert store.get_item(_user_key(auth.principal))["epoch"] == new_epoch
    assert store.get_item(_head_key(view.scope_key, EPOCH)) == old_head
    assert store.get_item(_final_key(view.scope_key, EPOCH)) == old_final

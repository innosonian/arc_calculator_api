"""Deterministic commit schedules shared with the real DynamoDB acceptance tests."""

from dataclasses import replace
import uuid

import pytest

from mock_journey.course_contracts import (
    POLICY_VERSION, AssignmentBinding, AttemptTemplate, ContentReport, CourseBinding, InventoryTicket, StartCommand, sealed_bundle,
)
from mock_journey.course_errors import CourseError
from mock_journey.course_calculation import CourseCalculationBridge
from mock_journey.course_provider import validate_bundle
from mock_journey.course_policy import learner_key, placement_key
from mock_journey.course_state import _final_key, _head_key, _item_key, _report_key, _start_key, _user_key
from mock_journey.typed import json_bytes, parse_json
from tests.test_vcc_policy import completed_progress
from tests.test_vcc_state import (
    EPOCH, REPORT_A, REQUEST, binding_of, make_bundle, provision, repository, seed_auth,
)


@pytest.fixture
def race_context():
    repo, store, _ = repository(uuids=lambda: str(uuid.uuid4()))
    return provision_race(repo, store)


def provision_race(repo, store):
    auth = seed_auth(store)
    bundle = make_bundle()
    view = provision(repo, auth, bundle)
    command = StartCommand(REQUEST, 501, 101, 1001, bundle.definition_hash)
    return repo, store, auth, bundle, view, command


def _once_before_transaction(store, operation):
    original = store.transact
    fired = False

    def transact(actions):
        nonlocal fired
        if not fired:
            fired = True
            operation()
        return original(actions)

    store.transact = transact


def _new_inventory(repo, auth, bundle, assignments=None):
    ticket = repo.begin_inventory(auth, bundle.scope.learner)
    repo.apply_inventory(auth, ticket, (binding_of(bundle),) if assignments is None else assignments)
    return ticket


def _start(context):
    repo, _, auth, _, view, command = context
    return repo.start(auth, command, kind="content", view=view, template=None)


def _report(context, start_id):
    repo, _, auth, _, _, _ = context
    return repo.report(auth, course_id=101, enrollment_id=501, placement_id=1001,
                       report=ContentReport(REPORT_A, start_id, "video-v1", "video_segments", ((0, 20000),)))


def _reset(store, auth):
    user = store.get_item(_user_key(auth.principal))
    user["epoch"] = str(uuid.uuid4())
    user["revision"] += 1
    store.seed(user)


def assert_revocation_wins_start(context):
    repo, store, auth, bundle, view, command = context
    original = repo.policy.can_start
    fired = False

    def revoke_after_policy(*args):
        nonlocal fired
        original(*args)
        if not fired:
            fired = True
            _new_inventory(repo, auth, bundle, ())

    repo.policy.can_start = revoke_after_policy
    with pytest.raises(CourseError) as raised:
        _start(context)
    assert raised.value.code == "NOT_FOUND"
    assert repo.find_created(auth, command, kind="content") is None
    assert repo.load_inventory_for_session(auth).assignments == ()
    assert store.get_item(_head_key(view.scope_key, EPOCH))["revision"] == view.gate.revision


def assert_reassigned_public_ids_do_not_authorize_old_scope(context):
    repo, store, auth, bundle, view, command = context
    replacement = AssignmentBinding(
        replace(bundle.scope, enrollment_id="another-source-enrollment"), bundle.public_ids,
    )
    _new_inventory(repo, auth, bundle, (replacement,))
    with pytest.raises(CourseError) as raised:
        _start(context)
    assert raised.value.code == "NOT_FOUND"
    assert repo.find_created(auth, command, kind="content") is None
    assert store.get_item(_head_key(view.scope_key, EPOCH))["revision"] == view.gate.revision


def test_reassigned_public_ids_do_not_authorize_old_scope(race_context):
    assert_reassigned_public_ids_do_not_authorize_old_scope(race_context)


def assert_public_id_remap_cannot_retarget_prepared_start(context, *, kind):
    repo, _, auth, bundle, _, _ = context
    indices = (0, 2) if kind == "content" else (2, 3)
    original = bundle.placements[indices[0]]
    command = StartCommand(str(uuid.uuid4()), 501, 101, original.public_link_id, bundle.definition_hash)
    view = repo.load_start_view(auth, command)
    template = None if kind == "content" else CourseCalculationBridge().prepare(auth, command, view)
    placements = list(bundle.placements)
    for index, other in zip(indices, reversed(indices)):
        detail = parse_json(placements[index].detail_json)
        detail["courseItemLinkId"] = bundle.placements[other].public_link_id
        placements[index] = replace(placements[index], public_link_id=detail["courseItemLinkId"], detail_json=detail)
    changed = sealed_bundle(replace(bundle, placements=tuple(placements)))
    validate_bundle(changed, repo.settings)
    assert changed.definition_hash == bundle.definition_hash
    ticket = _new_inventory(repo, auth, changed)
    refresh = repo.begin_refresh(auth, binding_of(changed), ticket)
    assert repo.apply_refresh(auth, refresh, changed).state == "ready"
    with pytest.raises(CourseError) as raised:
        repo.start(auth, command, kind=kind, view=view, template=template)
    assert raised.value.code == "DEFINITION_CHANGED"
    assert repo.find_created(auth, command, kind=kind) is None


@pytest.mark.parametrize("kind", ["content", "attempt"])
def test_public_id_remap_cannot_retarget_prepared_start(race_context, kind):
    assert_public_id_remap_cannot_retarget_prepared_start(race_context, kind=kind)


def assert_document_evidence_capacity_is_atomic(context):
    repo, store, auth, bundle, view, _ = context
    version = "v" * 8000
    placements = list(bundle.placements)
    placements[1] = replace(placements[1], content_version=version)
    changed = sealed_bundle(replace(bundle, placements=tuple(placements)))
    validate_bundle(changed, repo.settings)
    ticket = _new_inventory(repo, auth, changed)
    refresh = repo.begin_refresh(auth, binding_of(changed), ticket)
    repo.apply_refresh(auth, refresh, changed)
    command = StartCommand(str(uuid.uuid4()), 501, 101, 1002, changed.definition_hash)
    started = repo.start(auth, command, kind="content", view=repo.load_start_view(auth, command), template=None)
    control_keys = [_start_key(view.scope_key, EPOCH, started.start_id), _head_key(view.scope_key, EPOCH),
                    _item_key(view.scope_key, EPOCH, placement_key(view.scope_key, placements[1].source_id))]
    accepted = []
    for _ in range(60):
        report = ContentReport(str(uuid.uuid4()), started.start_id, version, "document_confirmed",
                               display_report_id=str(uuid.uuid4()))
        before = [store.get_item(key) for key in control_keys]
        try:
            receipt = repo.report(auth, course_id=101, enrollment_id=501, placement_id=1002, report=report)
        except CourseError as error:
            assert error.code == "PROGRESS_CAPACITY_EXCEEDED"
            assert [store.get_item(key) for key in control_keys] == before
            assert store.get_item(_report_key(view.scope_key, EPOCH, started.start_id, report.report_id)) is None
            assert accepted
            old_report, old_receipt = accepted[0]
            assert repo.report(auth, course_id=101, enrollment_id=501, placement_id=1002,
                               report=old_report).response_json == old_receipt.response_json
            return
        accepted.append((report, receipt))
    pytest.fail("Document evidence exceeded the control row budget without a capacity error")


def test_document_evidence_capacity_is_atomic(race_context):
    assert_document_evidence_capacity_is_atomic(race_context)


def assert_parent_generation_wins_refresh(context, *, failure):
    repo, store, auth, bundle, view, _ = context
    ticket = InventoryTicket(learner_key(bundle.scope.learner), EPOCH, 1)
    refresh = repo.begin_refresh(auth, binding_of(bundle), ticket)
    before = store.get_item(_head_key(view.scope_key, EPOCH))
    _once_before_transaction(store, lambda: _new_inventory(repo, auth, bundle))
    response = repo.apply_refresh(auth, refresh, CourseError("ARC_PROGRESS_UNAVAILABLE") if failure else bundle)
    assert response.state == "ready"
    assert repo.load_inventory_for_session(auth).generation == 2
    assert store.get_item(_head_key(view.scope_key, EPOCH)) == before


def assert_new_refresh_generation_survives_report(context):
    repo, store, auth, bundle, view, _ = context
    started = _start(context)
    inventory = InventoryTicket(learner_key(bundle.scope.learner), EPOCH, 1)
    tickets = []
    _once_before_transaction(store, lambda: tickets.append(
        repo.begin_refresh(auth, binding_of(bundle), inventory)))
    receipt = parse_json(_report(context, started.start_id).response_json)
    assert receipt["isCompleted"] is True
    ticket = tickets[0]
    head = store.get_item(_head_key(view.scope_key, EPOCH))
    assert head["refresh_generation"] == ticket.generation
    assert head["revision"] > ticket.head_revision
    gate = repo.apply_refresh(auth, ticket, CourseError("ARC_PROGRESS_UNAVAILABLE"))
    assert gate.state == "waiting"
    assert gate.reason == "arc_progress_unavailable"


def test_new_refresh_generation_is_not_reverted_by_report(race_context):
    assert_new_refresh_generation_survives_report(race_context)


def assert_reset_wins_report(context):
    repo, store, auth, bundle, view, _ = context
    started = _start(context)
    old_head = store.get_item(_head_key(view.scope_key, EPOCH))
    _once_before_transaction(store, lambda: _reset(store, auth))
    body = parse_json(_report(context, started.start_id).response_json)
    assert body["application"] == "historical_only"
    assert body["isCompleted"] is body["isPassed"] is body["courseStatus"] is None
    assert store.get_item(_head_key(view.scope_key, EPOCH)) == old_head
    stored = store.get_item(_report_key(view.scope_key, EPOCH, started.start_id, REPORT_A))
    assert stored["historical_only"] is True


def assert_new_definition_wins_report(context):
    repo, store, auth, bundle, view, _ = context
    started = _start(context)
    new_bundle = sealed_bundle(replace(bundle, placements=(
        replace(bundle.placements[0], content_version="video-v2"),
    ) + bundle.placements[1:]))
    original = repo.policy.evaluate_content
    fired = False

    def update_after_evaluation(*args):
        nonlocal fired
        result = original(*args)
        if not fired:
            fired = True
            ticket = _new_inventory(repo, auth, new_bundle)
            refresh = repo.begin_refresh(auth, binding_of(new_bundle), ticket)
            assert repo.apply_refresh(auth, refresh, new_bundle).state == "ready"
        return result

    repo.policy.evaluate_content = update_after_evaluation
    body = parse_json(_report(context, started.start_id).response_json)
    assert body["application"] == "pending_reconciliation"
    assert body["isCompleted"] is False
    head = store.get_item(_head_key(view.scope_key, EPOCH))
    assert head["definition_hash"] == new_bundle.definition_hash
    assert head["completed_placements"] == []
    assert not any(item["completed"] for item in parse_json(head["progress_json"])["items"].values())


def assert_first_final_initializes_item(context):
    repo, store, auth, bundle, view, _ = context
    progress = completed_progress(bundle, 1001, 1002, 1003, 1004)
    head = store.get_item(_head_key(view.scope_key, EPOCH))
    head["progress_json"] = json_bytes(progress).decode("utf-8")
    head["completed_placements"] = progress["completed_placements"]
    store.seed(head)
    final = bundle.placements[-1]
    item_key = placement_key(view.scope_key, final.source_id)
    command = StartCommand(str(uuid.uuid4()), 501, 101, final.public_link_id, bundle.definition_hash)
    template = AttemptTemplate({"attempt_id": str(uuid.uuid4()), "definition_json": final.execution_json.decode()}, CourseBinding(
        view.scope_key, item_key, "final_assessment", bundle.definition_hash,
        final.content_version, EPOCH, POLICY_VERSION,
    ))
    receipt = repo.start(auth, command, kind="attempt", view=repo.load_start_view(auth, command), template=template)
    item = store.get_item(_item_key(view.scope_key, EPOCH, item_key))
    assert item["completed"] is False
    assert item["passed"] is None
    role = store.get_item(_final_key(view.scope_key, EPOCH))
    assert role["phase"] == "active"
    assert role["active_attempt_id"] == receipt.attempt_id
    return receipt


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
    assert store.get_item(_head_key(view.scope_key, EPOCH))["completed_placements"] == []


def test_pre_resolve_generation_uses_stored_binding(race_context):
    repo, _, auth, bundle, _, _ = race_context
    ticket = repo.begin_inventory_for_session(auth)
    assert ticket.learner_key == learner_key(bundle.scope.learner)
    assert ticket.generation == 2
    failed = repo.apply_inventory(auth, ticket, CourseError("CONTRACT_PENDING"))
    assert failed.state == "waiting"
    assert failed.reason == "contract_pending"


def assert_refresh_replacement_preserves_pass_lock(context, *, replace_assessment, identity_only=False):
    repo, store, auth, bundle, view, _ = context
    progress = completed_progress(bundle, 1001, 1002, 1003, 1004, 1005, final={"phase": "passed"})
    head = store.get_item(_head_key(view.scope_key, EPOCH))
    head.update(progress_json=json_bytes(progress).decode("utf-8"),
                completed_placements=progress["completed_placements"], course_complete=True)
    store.seed(head)
    final = store.get_item(_final_key(view.scope_key, EPOCH))
    final.update(phase="passed", passed_attempt_id=str(uuid.uuid4()))
    store.seed(final)
    if replace_assessment:
        last = bundle.placements[-1]
        if identity_only:
            identity = parse_json(last.content_identity_json)
            identity["training_program_id"] = "replacement-program"
            changed_last = replace(last, content_identity_json=identity)
        else:
            execution = parse_json(last.execution_json)
            execution["goal"]["required"] += 1
            changed_last = replace(last, execution_json=execution)
        changed = sealed_bundle(replace(bundle, placements=bundle.placements[:-1] + (
            changed_last,
        )))
    else:
        changed = sealed_bundle(replace(bundle, placements=(
            replace(bundle.placements[0], content_version="video-v2"),
        ) + bundle.placements[1:]))
    for _ in range(2):
        ticket = _new_inventory(repo, auth, changed)
        refresh = repo.begin_refresh(auth, binding_of(changed), ticket)
        gate = repo.apply_refresh(auth, refresh, changed)
        assert gate.state == ("reconciliation_required" if replace_assessment else "ready")
    projected = parse_json(store.get_item(_head_key(view.scope_key, EPOCH))["progress_json"])
    key = placement_key(view.scope_key, changed.placements[-1].source_id)
    assert projected["items"][key]["completed"] is (not replace_assessment)
    assert projected["items"][key]["passed"] is (None if replace_assessment else True)
    assert store.get_item(_final_key(view.scope_key, EPOCH)) == final


@pytest.mark.parametrize("replace_assessment", [False, True])
def test_refresh_after_pass_reconciles_only_replaced_assessment(race_context, replace_assessment):
    assert_refresh_replacement_preserves_pass_lock(race_context, replace_assessment=replace_assessment)


def assert_identity_change_does_not_reuse_unfinished_content(context):
    repo, store, auth, bundle, view, _ = context
    started = _start(context)
    identity = parse_json(bundle.placements[0].content_identity_json)
    identity["asset_ids"] = ["replacement-video"]
    changed = sealed_bundle(replace(bundle, placements=(
        replace(bundle.placements[0], content_identity_json=identity),
    ) + bundle.placements[1:]))
    ticket = _new_inventory(repo, auth, changed)
    refresh = repo.begin_refresh(auth, binding_of(changed), ticket)
    assert repo.apply_refresh(auth, refresh, changed).state == "ready"
    receipt = parse_json(_report(context, started.start_id).response_json)
    assert receipt["application"] == "pending_reconciliation"
    assert receipt["isCompleted"] is False
    assert store.get_item(_head_key(view.scope_key, EPOCH))["completed_placements"] == []


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
    assert store.get_item(_head_key(view.scope_key, EPOCH))["course_complete"] is True


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
        start = store.get_item(_start_key(view.scope_key, EPOCH, started.start_id))
        start["report_count"] = change["report_count"]
        store.seed(start)
        change = {}
    report = replace(ContentReport(REPORT_A, started.start_id, "video-v1", "video_segments", ((0, 20000),)), **change)
    with pytest.raises(CourseError) as raised:
        repo.report(auth, course_id=101, enrollment_id=501, placement_id=1001, report=report)
    assert raised.value.code == expected
    assert store.get_item(_report_key(view.scope_key, EPOCH, started.start_id, REPORT_A)) is None

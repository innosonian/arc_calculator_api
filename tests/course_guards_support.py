"""Q16 v2 commit operations and session-expiry probes shared with DynamoDB Local.

Moved unchanged from tests/test_course_guards_settings.py so the DynamoDB Local
suite no longer imports a test module; that module re-exports the same objects.
"""

from copy import deepcopy
import uuid

import pytest

from mock_journey.course_contracts import (
    POLICY_VERSION, AttemptTemplate, ContentReport, CourseBinding, StartCommand,
)
from mock_journey.course_errors import CourseError
from mock_journey.course_policy import placement_key
from mock_journey.storage_keys import course_head_key, session_key
from mock_journey.typed import json_bytes
from tests.vcc_policy_support import completed_progress
from tests.vcc_state_support import (
    EPOCH, REPORT_A, REQUEST, binding_of, make_bundle, provision, repository, seed_auth,
)


NOW = 1_000_000
EXPIRES_AT = 2_000_000


class Context:
    def __init__(self, repo, store, auth, bundle, snapshot):
        self.repo, self.store, self.auth, self.bundle, self.snapshot = repo, store, auth, bundle, snapshot


def memory_context(clock=NOW):
    repo, store, _ = repository(clock=clock, uuids=lambda: str(uuid.uuid4()))
    auth = seed_auth(store, expires_at=EXPIRES_AT)
    session = session_key(auth.session_id)

    def snapshot():
        return {key: deepcopy(value) for key, value in store.items.items()
                if key != (session["PK"], session["SK"])}
    return Context(repo, store, auth, make_bundle(), snapshot)


def expire_before_first_transaction(context, expires_at=NOW):
    """Stored session expires between the read verdict and the commit.

    Only expires_at changes (status/revision/principal stay equal), so only
    the expiry condition can refuse the transaction.
    """
    store = context.store
    original = store.transact
    fired = []

    def transact(actions):
        if not fired:
            fired.append(True)
            session = store.get_item(session_key(context.auth.session_id))
            store.seed({**session, "expires_at": expires_at})
        return original(actions)

    store.transact = transact
    return fired


def _inventory(context):
    repo, auth, bundle = context.repo, context.auth, context.bundle
    ticket = repo.begin_inventory(auth, bundle.scope.learner)
    repo.apply_inventory(auth, ticket, (binding_of(bundle),))
    return ticket


def _refresh(context):
    ticket = _inventory(context)
    context.repo.ensure_epoch(context.auth, binding_of(context.bundle))
    return context.repo.begin_refresh(context.auth, binding_of(context.bundle), ticket)


def _view(context):
    return provision(context.repo, context.auth, context.bundle)


def _training_template(context, view, link_id=1003):
    placement = next(item for item in context.bundle.placements if item.public_link_id == link_id)
    return placement, AttemptTemplate(
        {"attempt_id": str(uuid.uuid4()), "definition_json": placement.execution_json.decode()},
        CourseBinding(view.scope_key, placement_key(view.scope_key, placement.source_id), "training",
                      context.bundle.definition_hash, placement.content_version, EPOCH, POLICY_VERSION),
    )


def _final_ready(context, view):
    progress = completed_progress(context.bundle, 1001, 1002, 1003, 1004)
    head = context.store.get_item(course_head_key(view.scope_key, EPOCH))
    context.store.seed({**head, "progress_json": json_bytes(progress).decode("utf-8"),
                        "completed_placements": progress["completed_placements"]})
    final = context.bundle.placements[-1]
    command = StartCommand(str(uuid.uuid4()), 501, 101, final.public_link_id, context.bundle.definition_hash)
    template = AttemptTemplate(
        {"attempt_id": str(uuid.uuid4()), "definition_json": final.execution_json.decode()},
        CourseBinding(view.scope_key, placement_key(view.scope_key, final.source_id), "final_assessment",
                      context.bundle.definition_hash, final.content_version, EPOCH, POLICY_VERSION),
    )
    return command, template


def op_begin_inventory(context):
    return lambda: context.repo.begin_inventory(context.auth, context.bundle.scope.learner)


def op_apply_inventory(context):
    ticket = context.repo.begin_inventory(context.auth, context.bundle.scope.learner)
    return lambda: context.repo.apply_inventory(context.auth, ticket, (binding_of(context.bundle),))


def op_ensure_epoch(context):
    _inventory(context)
    return lambda: context.repo.ensure_epoch(context.auth, binding_of(context.bundle))


def op_begin_refresh(context):
    ticket = _inventory(context)
    context.repo.ensure_epoch(context.auth, binding_of(context.bundle))
    return lambda: context.repo.begin_refresh(context.auth, binding_of(context.bundle), ticket)


def op_apply_refresh(context):
    refresh = _refresh(context)
    return lambda: context.repo.apply_refresh(context.auth, refresh, context.bundle)


def op_apply_refresh_error(context):
    refresh = _refresh(context)
    return lambda: context.repo.apply_refresh(context.auth, refresh, CourseError("CONTRACT_PENDING"))


def op_start_content(context):
    view = _view(context)
    command = StartCommand(REQUEST, 501, 101, 1001, context.bundle.definition_hash)
    return lambda: context.repo.start(context.auth, command, kind="content", view=view, template=None)


def op_start_training(context):
    view = _view(context)
    placement, template = _training_template(context, view)
    command = StartCommand(str(uuid.uuid4()), 501, 101, placement.public_link_id, context.bundle.definition_hash)
    return lambda: context.repo.start(context.auth, command, kind="attempt", view=view, template=template)


def op_start_final(context):
    view = _view(context)
    command, template = _final_ready(context, view)
    fresh = context.repo.load_start_view(context.auth, command)
    return lambda: context.repo.start(context.auth, command, kind="attempt", view=fresh, template=template)


def op_report(context):
    view = _view(context)
    command = StartCommand(REQUEST, 501, 101, 1001, context.bundle.definition_hash)
    receipt = context.repo.start(context.auth, command, kind="content", view=view, template=None)
    report = ContentReport(REPORT_A, receipt.start_id, "video-v1", "video_segments", ((0, 10000),))
    return lambda: context.repo.report(context.auth, course_id=101, enrollment_id=501,
                                       placement_id=1001, report=report)


COMMIT_OPERATIONS = {
    "begin_inventory": op_begin_inventory,
    "apply_inventory": op_apply_inventory,
    "ensure_epoch": op_ensure_epoch,
    "begin_refresh": op_begin_refresh,
    "apply_refresh": op_apply_refresh,
    "apply_refresh_error": op_apply_refresh_error,
    "start_content": op_start_content,
    "start_training": op_start_training,
    "start_final": op_start_final,
    "report": op_report,
}


def assert_expiry_between_read_and_commit_is_session_expired(context, name):
    run = COMMIT_OPERATIONS[name](context)
    before = context.snapshot()
    fired = expire_before_first_transaction(context)
    with pytest.raises(CourseError) as raised:
        run()
    assert fired == [True]
    assert raised.value.code == "SESSION_EXPIRED"
    assert context.snapshot() == before


def assert_unexpired_session_still_commits(context, name):
    run = COMMIT_OPERATIONS[name](context)
    fired = expire_before_first_transaction(context, expires_at=NOW + 1)
    run()
    assert fired == [True]

"""Characterization of DynamoCourseRepository read/commit order (S7-04/X3-04).

Written before course_state.py was split into use-case modules. The expected
tables below are written out by hand from ARCHITECTURE §4/§8-6 and the
repository source, not captured from a run: for every command they fix

- the rows read, in order (SESSION/USER first, then the use-case rows; blob
  reads and writes are logged in the same sequence),
- the TransactItems-level action list: op, row kind, and each condition dict
  with its key order, and
- that a retry re-reads the whole snapshot and builds its conditions from that
  same iteration's rows (a concurrent HEAD bump shows up in the next attempt).

The DynamoDB request shapes of the main flows are pinned separately by
tests/fixtures/v2_baseline/write_requests.json; these tables add the branches
that baseline does not reach (waiting errors, retries, historical reports).
"""

import uuid

import pytest

from mock_journey.course_contracts import ContentReport, StartCommand
from mock_journey.course_errors import CourseError
from mock_journey.course_policy import learner_key
from mock_journey.models import AuthContext
from tests.test_vcc_state import (
    EPOCH, REPORT_A, REPORT_B, REQUEST, REQUEST_B, SESSION, START, binding_of, learner_from, make_bundle,
    repository, seed_auth,
)
from tests.course_store_fakes import InMemoryBlobStore, InMemoryCourseStore


NOW = 1_000_000
EXPIRES_AT = 2_000_000
EPOCH_2 = "90000000-0000-4000-8000-000000000009"
START_2 = "41000000-0000-4000-8000-000000000001"
START_3 = "42000000-0000-4000-8000-000000000001"
REPORT_C = "32000000-0000-4000-8000-000000000001"
REPORT_D = "33000000-0000-4000-8000-000000000001"
REPORT_E = "34000000-0000-4000-8000-000000000001"


def label(key):
    pk, sk = key["PK"], key["SK"]
    prefix = pk.split("#", 1)[0]
    if prefix == "SESSION":
        return "SESSION" if sk == "AUTH" else ("CREATE" if sk.startswith("COURSE_CREATE#") else "?")
    if prefix == "COURSE":
        part = sk.split("#")[2]
        return part if part in {"HEAD", "FINAL", "ITEM", "START", "REPORT"} else "?"
    return {"USER": "USER", "COURSE_LEARNER": "LEARNER", "COURSE_PRINCIPAL": "PRINCIPAL",
            "COURSE_START": "LOCATOR", "ATTEMPT": "ATTEMPT"}.get(prefix, "?")


def shape(action):
    """(op, row kind, ((condition, ordered items), ...)) in the action's own key order."""
    assert list(action)[:2] == ["op", "key"]
    if action["op"] == "put":
        assert list(action)[2] == "item"
        assert action["key"] == {"PK": action["item"]["PK"], "SK": action["item"]["SK"]}
    conditions = []
    for name, value in action.items():
        if name in {"op", "key", "item"}:
            continue
        conditions.append((name, tuple(value.items()) if type(value) is dict else value))
    return action["op"], label(action["key"]), tuple(conditions)


def session(revision=0):
    return ("condition_check", "SESSION", (
        ("if_match", (("status", "active"), ("revision", revision), ("principal", PRINCIPAL))),
        ("if_greater", (("expires_at", NOW),)),
    ))


def user(epoch=EPOCH, revision=0):
    return ("condition_check", "USER", (("if_match", (("epoch", epoch), ("revision", revision))),))


def put(kind, **conditions):
    return ("put", kind, tuple((name, tuple(value.items()) if type(value) is dict else value)
                               for name, value in conditions.items()))


def check(kind, **fields):
    return ("condition_check", kind, (("if_match", tuple(fields.items())),))


PRINCIPAL = learner_from().principal
LEARNER_KEY = learner_key(learner_from())


class Log:
    """Wrap one repository's store/blob store and log reads, blob I/O and commits."""

    def __init__(self, repo, store, blobs):
        self.events, self.calls = [], []
        get_item, transact = store.get_item, store.transact
        get_bytes, put_bytes = blobs.get_bytes, blobs.put_bytes
        self.before_commit = None

        def logged_get(key):
            self.events.append(label(key))
            return get_item(key)

        def logged_transact(actions):
            self.calls.append([shape(action) for action in actions])
            if self.before_commit is not None:
                hook, self.before_commit = self.before_commit, None
                hook()
            return transact(actions)

        def logged_get_bytes(digest_value):
            self.events.append("BLOB_GET")
            return get_bytes(digest_value)

        def logged_put_bytes(body):
            self.events.append("BLOB_PUT")
            return put_bytes(body)

        store.get_item, store.transact = logged_get, logged_transact
        blobs.get_bytes, blobs.put_bytes = logged_get_bytes, logged_put_bytes

    def take(self):
        events, calls = self.events, self.calls
        self.events, self.calls = [], []
        return events, calls


def bump_head(store, scope_key_value, epoch=EPOCH):
    """A concurrent writer's HEAD revision bump, applied without logging a read."""
    key = ("COURSE#" + scope_key_value, f"EPOCH#{epoch}#HEAD")
    store.items[key]["revision"] += 1


@pytest.fixture(scope="module")
def run():
    ids = iter([START, START_2, START_3])
    repo, store, blobs = repository(clock=NOW, uuids=lambda: next(ids))
    assert type(store) is InMemoryCourseStore and type(blobs) is InMemoryBlobStore
    auth = seed_auth(store, expires_at=EXPIRES_AT)
    log = Log(repo, store, blobs)
    bundle = make_bundle()
    steps = {}

    def step(name, call):
        try:
            result = call()
        except CourseError as error:
            result = error
        steps[name] = (result, *log.take())
        return result

    learner = learner_from()
    step("begin_inventory_new", lambda: repo.begin_inventory(auth, learner))
    first = steps["begin_inventory_new"][0]
    step("apply_inventory_contract_pending",
         lambda: repo.apply_inventory(auth, first, CourseError("CONTRACT_PENDING")))
    ticket = step("begin_inventory_existing", lambda: repo.begin_inventory(auth, learner))
    step("apply_inventory_ready", lambda: repo.apply_inventory(auth, ticket, (binding_of(bundle),)))
    step("ensure_epoch_new", lambda: repo.ensure_epoch(auth, binding_of(bundle)))
    refresh = step("begin_refresh_1", lambda: repo.begin_refresh(auth, binding_of(bundle), ticket))
    step("apply_refresh_error",
         lambda: repo.apply_refresh(auth, refresh, CourseError("ARC_PROGRESS_UNAVAILABLE")))
    refresh = step("begin_refresh_2", lambda: repo.begin_refresh(auth, binding_of(bundle), ticket))
    step("apply_refresh_bundle", lambda: repo.apply_refresh(auth, refresh, bundle))
    command = StartCommand(REQUEST, 501, 101, 1001, bundle.definition_hash)
    view = step("load_start_view", lambda: repo.load_start_view(auth, command))
    video = step("start_video", lambda: repo.start(auth, command, kind="content", view=view, template=None))

    def report(report_id, start_id, event_type, *extra, placement_id=1001, version="video-v1", auth=auth):
        return repo.report(auth, course_id=101, enrollment_id=501, placement_id=placement_id,
                           report=ContentReport(report_id, start_id, version, event_type, *extra))

    step("report_video_1", lambda: report(REPORT_A, video.start_id, "video_segments", [[0, 10000]]))
    store.conflict_remaining = 1
    step("report_video_2_after_conflict",
         lambda: report(REPORT_B, video.start_id, "video_segments", [[10000, 20000]]))
    document_command = StartCommand(REQUEST_B, 501, 101, 1002, bundle.definition_hash)
    log.before_commit = lambda: bump_head(store, view.scope_key)
    document = step("start_document_after_head_bump",
                    lambda: repo.start(auth, document_command, kind="content", view=view, template=None))
    log.before_commit = lambda: bump_head(store, view.scope_key)
    step("report_document_after_head_bump",
         lambda: report(REPORT_C, document.start_id, "document_displayed", placement_id=1002,
                        version="document-v1"))
    # A reset moves USER to a new epoch; the old START only takes historical evidence.
    user_row = store.items[("USER#" + PRINCIPAL, "STATE")]
    user_row["epoch"], user_row["revision"] = EPOCH_2, 1
    store.items[("SESSION#" + SESSION, "AUTH")]["revision"] = 1
    late = AuthContext(SESSION, PRINCIPAL, 1, EXPIRES_AT)
    step("report_historical",
         lambda: report(REPORT_D, video.start_id, "video_segments", [[0, 20000]], auth=late))
    store.always_conflict = True
    step("begin_inventory_exhausted", lambda: repo.begin_inventory(late, learner))
    step("report_exhausted",
         lambda: report(REPORT_E, video.start_id, "video_segments", [[0, 20000]], auth=late))
    return steps, store, bundle, view, video, document


CURRENT_REPORT = ["SESSION", "USER", "LOCATOR", "START", "REPORT", "HEAD", "BLOB_GET", "ITEM"]
READS = {
    "begin_inventory_new": ["SESSION", "USER", "LEARNER"],
    "apply_inventory_contract_pending": ["SESSION", "USER", "LEARNER"],
    "begin_inventory_existing": ["SESSION", "USER", "LEARNER"],
    "apply_inventory_ready": ["SESSION", "USER", "LEARNER"],
    "ensure_epoch_new": ["SESSION", "USER", "HEAD", "FINAL"],
    "begin_refresh_1": ["SESSION", "USER", "LEARNER", "HEAD", "FINAL"],
    "apply_refresh_error": ["SESSION", "USER", "HEAD", "LEARNER"],
    "begin_refresh_2": ["SESSION", "USER", "LEARNER", "HEAD", "FINAL"],
    "apply_refresh_bundle": ["SESSION", "USER", "HEAD", "LEARNER", "FINAL", "BLOB_PUT"],
    "load_start_view": ["SESSION", "USER", "PRINCIPAL", "LEARNER", "HEAD", "FINAL", "BLOB_GET"],
    # D133: the receipt read happens once before the loop; only a retry re-reads it.
    "start_video": ["SESSION", "USER", "CREATE",
                    "SESSION", "USER",
                    "PRINCIPAL", "LEARNER", "HEAD", "FINAL", "BLOB_GET", "ITEM"],
    "report_video_1": CURRENT_REPORT,
    "report_video_2_after_conflict": CURRENT_REPORT * 2,
    "start_document_after_head_bump": ["SESSION", "USER", "CREATE"] + [
        "SESSION", "USER",
        "PRINCIPAL", "LEARNER", "HEAD", "FINAL", "BLOB_GET", "ITEM",
    ] + [
        "SESSION", "USER", "CREATE", "SESSION", "USER",
        "PRINCIPAL", "LEARNER", "HEAD", "FINAL", "BLOB_GET", "ITEM",
    ],
    "report_document_after_head_bump": CURRENT_REPORT * 2,
    "report_historical": ["SESSION", "USER", "LOCATOR", "START", "REPORT"],
    "begin_inventory_exhausted": ["SESSION", "USER", "LEARNER"] * 4,
    "report_exhausted": ["SESSION", "USER", "LOCATOR", "START", "REPORT"] * 4,
}


def _refresh_head(generation, revision):
    return put("HEAD", if_match={"refresh_generation": generation, "revision": revision, "epoch": EPOCH})


def _start_actions(head_revision):
    return [
        session(), user(),
        check("LEARNER", state="ready", revision=4, inventory_generation=2),
        put("HEAD", if_match={"revision": head_revision, "epoch": EPOCH, "gate_state": "ready",
                              "definition_hash": DEFINITION_HASH}),
        put("START", if_not_exists=True), put("LOCATOR", if_not_exists=True), put("CREATE", if_not_exists=True),
    ]


def _report_actions(start_revision, item, head_revision):
    return [
        session(), user(),
        put("REPORT", if_not_exists=True),
        put("START", if_match={"revision": start_revision}),
        item,
        put("HEAD", if_match={"revision": head_revision, "epoch": EPOCH}),
    ]


DEFINITION_HASH = make_bundle().definition_hash
def _historical(start_revision):
    return [
        session(revision=1), user(epoch=EPOCH_2, revision=1),
        put("REPORT", if_not_exists=True), put("START", if_match={"revision": start_revision}),
    ]
LEARNER_BY_EPOCH = put("PRINCIPAL", if_missing_or_match={"epoch": EPOCH, "learner_key": LEARNER_KEY})
COMMITS = {
    "begin_inventory_new": [[session(), user(), put("LEARNER", if_not_exists=True), LEARNER_BY_EPOCH]],
    "apply_inventory_contract_pending": [[
        session(), user(),
        put("LEARNER", if_match={"inventory_generation": 1, "revision": 1, "epoch": EPOCH}),
    ]],
    "begin_inventory_existing": [[
        session(), user(),
        put("LEARNER", if_match={"inventory_generation": 1, "revision": 2, "epoch": EPOCH}), LEARNER_BY_EPOCH,
    ]],
    "apply_inventory_ready": [[
        session(), user(),
        put("LEARNER", if_match={"inventory_generation": 2, "revision": 3, "epoch": EPOCH}),
    ]],
    "ensure_epoch_new": [[session(), user(), put("HEAD", if_not_exists=True), put("FINAL", if_not_exists=True)]],
    "begin_refresh_1": [[
        session(), user(), check("LEARNER", inventory_generation=2, epoch=EPOCH), _refresh_head(0, 0),
    ]],
    "apply_refresh_error": [[
        session(), user(), check("LEARNER", inventory_generation=2, revision=4, epoch=EPOCH), _refresh_head(1, 1),
    ]],
    "begin_refresh_2": [[
        session(), user(), check("LEARNER", inventory_generation=2, epoch=EPOCH), _refresh_head(1, 2),
    ]],
    "apply_refresh_bundle": [[
        session(), user(), check("LEARNER", inventory_generation=2, revision=4, epoch=EPOCH),
        check("FINAL", revision=0, epoch=EPOCH, phase="free", active_attempt_id=None),
        _refresh_head(2, 3),
    ]],
    "load_start_view": [],
    "start_video": [_start_actions(4)],
    "report_video_1": [_report_actions(0, put("ITEM", if_not_exists=True), 5)],
    # The conflicting attempt and its retry read the same unchanged rows.
    "report_video_2_after_conflict": [_report_actions(1, put("ITEM", if_match={"revision": 0}), 6)] * 2,
    # The retry takes the concurrently bumped HEAD revision from its own re-read.
    "start_document_after_head_bump": [_start_actions(7), _start_actions(8)],
    "report_document_after_head_bump": [
        _report_actions(0, put("ITEM", if_not_exists=True), 9),
        _report_actions(0, put("ITEM", if_not_exists=True), 10),
    ],
    "report_historical": [_historical(2)],
    "begin_inventory_exhausted": [[
        session(revision=1), user(epoch=EPOCH_2, revision=1), put("LEARNER", if_not_exists=True),
        put("PRINCIPAL", if_missing_or_match={"epoch": EPOCH_2, "learner_key": LEARNER_KEY}),
    ]] * 4,
    # The historical report above advanced START to revision 3.
    "report_exhausted": [_historical(3)] * 4,
}


def test_tables_cover_the_same_steps():
    assert list(READS) == list(COMMITS)


@pytest.mark.parametrize("name", list(READS))
def test_rows_are_read_in_the_documented_order(run, name):
    steps = run[0]
    assert steps[name][1] == READS[name]


@pytest.mark.parametrize("name", list(COMMITS))
def test_commits_carry_the_documented_actions_and_condition_order(run, name):
    steps = run[0]
    assert steps[name][2] == COMMITS[name]


def test_step_results_and_stored_rows(run):
    steps, store, bundle, view, video, document = run
    pending = steps["apply_inventory_contract_pending"][0]
    assert (pending.state, pending.reason) == ("waiting", "contract_pending")
    assert (pending.generation, pending.revision) == (1, 2)
    ticket = steps["begin_inventory_existing"][0]
    assert (ticket.learner_key, ticket.epoch, ticket.generation) == (LEARNER_KEY, EPOCH, 2)
    ready = steps["apply_inventory_ready"][0]
    assert (ready.state, ready.reason, ready.revision, len(ready.assignments)) == ("ready", None, 4, 1)
    gate = steps["ensure_epoch_new"][0]
    assert (gate.state, gate.reason) == ("waiting", "arc_progress_unavailable")
    assert (gate.revision, gate.definition_hash) == (0, None)
    first = steps["begin_refresh_1"][0]
    assert (first.inventory_generation, first.generation, first.head_revision) == (2, 1, 1)
    waiting = steps["apply_refresh_error"][0]
    assert (waiting.state, waiting.reason, waiting.revision) == ("waiting", "arc_progress_unavailable", 2)
    ready_gate = steps["apply_refresh_bundle"][0]
    assert (ready_gate.state, ready_gate.reason, ready_gate.revision) == ("ready", None, 4)
    assert ready_gate.definition_hash == bundle.definition_hash
    assert (video.created, video.start_id, document.start_id) == (True, START, START_3)
    # The conflicting document attempt wrote nothing, not even under its own start id.
    assert not [key for key in store.items if key[1].endswith(f"#START#{START_2}") or key[0].endswith(START_2)]
    assert steps["report_historical"][0].scope_key == view.scope_key
    for name in ("begin_inventory_exhausted", "report_exhausted"):
        error = steps[name][0]
        assert type(error) is CourseError and error.code == "TEMPORARILY_UNAVAILABLE", name


def test_waiting_inventory_row_keeps_its_field_order(run):
    steps, store, *_ = run
    row = store.items[("COURSE_LEARNER#" + LEARNER_KEY, f"EPOCH#{EPOCH}#HEAD")]
    assert list(row) == [
        "PK", "SK", "schema_version", "learner_key", "epoch", "revision", "inventory_generation", "state",
        "reason", "assignments", "provider", "tenant_id", "learner_id", "principal", "is_dummy", "updated_at",
    ]
    assert (row["schema_version"], row["updated_at"]) == (1, NOW)


def test_logger_detects_a_changed_order():
    # Negative control: the shape helper keeps condition key order.
    a = {"op": "put", "key": {"PK": "X#1", "SK": "S"}, "item": {"PK": "X#1", "SK": "S"},
         "if_match": {"revision": 1, "epoch": "e"}}
    b = {"op": "put", "key": {"PK": "X#1", "SK": "S"}, "item": {"PK": "X#1", "SK": "S"},
         "if_match": {"epoch": "e", "revision": 1}}
    assert a == b and shape(a) != shape(b)
    assert shape({"op": "condition_check", "key": {"PK": "COURSE#h", "SK": "EPOCH#e#FINAL"}, "if_match": {}}) == (
        "condition_check", "FINAL", (("if_match", ()),))
    assert label({"PK": "OTHER#1", "SK": "S"}) == "?"


def test_uuid_factory_is_consumed_once_per_content_attempt():
    ids = iter([str(uuid.UUID(int=index + 1, version=4)) for index in range(8)])
    consumed = []

    def factory():
        value = next(ids)
        consumed.append(value)
        return value
    repo, store, _ = repository(clock=NOW, uuids=factory)
    auth = seed_auth(store, expires_at=EXPIRES_AT)
    bundle = make_bundle()
    ticket = repo.begin_inventory(auth, bundle.scope.learner)
    repo.apply_inventory(auth, ticket, (binding_of(bundle),))
    repo.ensure_epoch(auth, binding_of(bundle))
    repo.apply_refresh(auth, repo.begin_refresh(auth, binding_of(bundle), ticket), bundle)
    command = StartCommand(REQUEST, 501, 101, 1001, bundle.definition_hash)
    view = repo.load_start_view(auth, command)
    assert consumed == []
    store.conflict_remaining = 2
    receipt = repo.start(auth, command, kind="content", view=view, template=None)
    assert len(consumed) == 3 and receipt.start_id == consumed[-1]

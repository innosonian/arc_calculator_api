"""Scripted flows and the matcher for the DynamoDB write-request baseline.

tests/fixtures/v2_baseline/write_requests.json holds, per flow, every write
request (PutItem and TransactWriteItems, including the cancelled attempts of a
conflict) that the runtime sent to the table, in call order and in the exact
request shape: ConditionExpression strings, ExpressionAttributeNames/Values,
typed items and action order. It is a characterization baseline captured
before the storage-key/constant extraction (P4), not a calculation golden.

Rows written by test seeding (the captured legacy fixture rows, the fresh
session token bound to them and the assigned learner's SESSION/USER rows) are
excluded, and so is the setup of the restart-limit, Relay and assigned-learner
flows that golden_flow already covers (login, starts, uploads). Everything
else the application, Worker and Relay write is kept. The table name is the
only normalized value ("<table>"). Other run-dependent values use the
placeholders of tests/v2_baseline_support.py with one-to-one bindings over one
flow. On the in-memory table the key order of every object is compared as
well (except decoded GSI cursors, whose order comes from the query response).
"""

from contextlib import contextmanager
from copy import deepcopy
import json
from types import SimpleNamespace
import uuid

from mock_journey.contracts import CURRENT_ADAPTER_VERSION, PENDING_GOAL_ADAPTER_VERSION
from tests.calculator_doubles import InterruptedCalculation, ProcessKilled, TrackedCalculator  # noqa: F401
from tests.dynamodb_doubles import RecordingClient  # noqa: F401 (re-export)
from tests.journey_support import (
    EventLog, HARNESS_QUEUE_URL, HARNESS_STAGE, JourneyStore, V2Journey, dummy_course,
)
from tests.legacy_rows_support import seed_legacy_rows
from tests.v2_baseline_support import (
    GOLDEN, _Bindings, _match, golden_flow, legacy_active_session, legacy_new_session,
)


WRITE_BASELINE = "write_requests.json"
WRITE_OPERATIONS = frozenset({"PutItem", "TransactWriteItems"})
READ_OPERATIONS = frozenset({"GetItem", "TransactGetItems", "Query", "Scan"})
TABLE = "<table>"
RELAY_SCOPE = {"environment": "journey-harness", "partition": "aws", "account_id": "000000000000",
               "region": "us-east-2"}


def load_write_baseline():
    return json.loads((GOLDEN / WRITE_BASELINE).read_text())


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------

class WriteRecorder:
    """Collect the write requests of one flow from MemoryDynamoDB's call record."""

    def __init__(self, store):
        self.store = store
        self.start = len(store.client.calls)
        self.excluded = []

    @contextmanager
    def excluded_calls(self):
        """Requests issued inside this block are test seeding, not runtime writes."""
        begin = len(self.store.client.calls)
        try:
            yield
        finally:
            self.excluded.append((begin, len(self.store.client.calls)))

    def _table(self, request):
        assert request["TableName"] == self.store.table, "a write went to another table"
        request["TableName"] = TABLE

    def writes(self):
        recorded = []
        for index, (operation, request) in enumerate(self.store.client.calls):
            if index < self.start or any(begin <= index < end for begin, end in self.excluded):
                continue
            if operation in READ_OPERATIONS:
                continue
            assert operation in WRITE_OPERATIONS, operation
            request = deepcopy(request)
            if operation == "PutItem":
                self._table(request)
            else:
                assert set(request) == {"TransactItems"}, sorted(request)
                for action in request["TransactItems"]:
                    ((_, body),) = action.items()
                    self._table(body)
            recorded.append({"operation": operation, "request": request})
        return recorded


def _excluded_seeding(recorder):
    """The legacy fixture seeding with its writes (and the fresh token binding) excluded.

    Passed to the shared legacy scripts as their ``seed`` argument (E-07), so
    the exclusion does not depend on where those scripts import the seeding
    function from.
    """
    def seeding(store, objects=None):
        with recorder.excluded_calls():
            seeded = seed_legacy_rows(store, objects)
        issue = seeded.issue_session_token

        def issue_session_token():
            with recorder.excluded_calls():
                return issue()
        seeded.issue_session_token = issue_session_token
        return seeded
    return seeding


# ---------------------------------------------------------------------------
# Flows
# ---------------------------------------------------------------------------

def _interrupting_adapter(version=CURRENT_ADAPTER_VERSION):
    """The bundled calculator; while ``adapter.mode`` is set every call is interrupted before it runs."""
    def interrupt(loaded, binding):
        if adapter.mode is None:
            return
        adapter.interrupted += 1
        if adapter.mode == "killed":
            raise ProcessKilled()
        raise InterruptedCalculation("test-only calculator interruption")

    adapter = TrackedCalculator(stage=HARNESS_STAGE, version=version, before_calculate=interrupt)
    adapter.mode = None
    adapter.interrupted = 0
    return adapter


def _interrupted_runs(h, adapter, job_id, mode):
    from mock_journey.jobs import CALCULATION_RESTART_LIMIT
    adapter.mode = mode
    for number in range(CALCULATION_RESTART_LIMIT + 1):
        if mode == "killed":
            try:
                h.worker.process(job_id)
            except ProcessKilled:
                pass
            else:
                raise AssertionError("the killed call returned")
        else:
            assert h.worker.process(job_id) is False
        assert adapter.interrupted == number + 1
        h.advance(61)  # Past the 60s lease of a killed call and the 5s retry of a deferral.
    adapter.mode = None


def flow_golden(store, recorder):
    golden_flow(store)


def flow_legacy_active_session(store, recorder):
    legacy_active_session(store, seed=_excluded_seeding(recorder))


def flow_legacy_new_session(store, recorder):
    legacy_new_session(store, seed=_excluded_seeding(recorder))


def flow_legacy_restart_limit(store, recorder):
    """The queued legacy fixture job: six interrupted calls, Relay wake, restart-limit failure."""
    with recorder.excluded_calls():
        seeded = seed_legacy_rows(store)
    # The legacy fixture job is bound to the retained pending-v3 adapter (D136, 3A).
    adapter = _interrupting_adapter(version=PENDING_GOAL_ADAPTER_VERSION)
    h = V2Journey(store, objects=seeded.objects, start=seeded.meta["capture_clock"] + 60,
                  events=EventLog(), adapters=[adapter])
    job_id = seeded.attempts["queued"]["job_id"]
    _interrupted_runs(h, adapter, job_id, "error")
    assert set(h.relay()) == {job_id}
    assert h.deliver() == {"batchItemFailures": []}
    assert h.store.row(f"JOB#{job_id}", "STATE")["failure_basis"] == "calculation_restart_limit"
    assert h.relay() == []


def _course_final(h, adapter):
    course = dummy_course("mock-compression-only", "adult")
    session = h.login()
    practice = h.start(session.token, course, course.practice_link_id)
    h.upload(session.token, practice["attemptId"], practice["condition"])
    h.advance(1)
    assert h.work(practice["attemptId"]) is True
    final = h.start(session.token, course, course.final_link_id)
    h.upload(session.token, final["attemptId"], final["condition"])
    h.advance(1)
    return session, final


def _course_restart_limit(store, recorder, mode):
    adapter = _interrupting_adapter()
    h = V2Journey(store, events=EventLog(), adapters=[adapter])
    with recorder.excluded_calls():  # Login, practice and final upload writes are in golden_flow.
        session, final = _course_final(h, adapter)
    job_id = h.job_id(final["attemptId"])
    _interrupted_runs(h, adapter, job_id, mode)
    assert h.work(job_id=job_id) is True
    assert h.store.row(f"JOB#{job_id}", "STATE")["terminal_seal"]["basis"] == "calculation_restart_limit"
    h.result(session.token, final["attemptId"], expected=503)
    assert h.work(job_id=job_id) is True  # Sealed: no further claim or write.


def flow_course_restart_limit_error(store, recorder):
    """Final assessment: six calculator errors (deferred recovery each), then the restart-limit seal."""
    _course_restart_limit(store, recorder, "error")


def flow_course_restart_limit_killed(store, recorder):
    """Final assessment: six killed calls (lease expiry each), then the restart-limit seal."""
    _course_restart_limit(store, recorder, "killed")


def flow_relay_checkpoint(store, recorder):
    """AWS-shaped Relay with the persistent checkpoint: two uploads, one wake pass, an idle pass."""
    from mock_journey.assembly import build_relay
    from mock_journey.relay_progress import DynamoRelayProgress
    from mock_journey.settings import RelaySettings
    from mock_journey.state import DynamoStateRepository

    h = V2Journey(store, events=EventLog())
    attempts = []
    with recorder.excluded_calls():  # Login, start and upload writes are in golden_flow.
        session = h.login()
        for program in ("mock-compression-only", "mock-ventilation-only"):
            course = dummy_course(program, "adult")
            started = h.start(session.token, course, course.practice_link_id)
            h.upload(session.token, started["attemptId"], started["condition"])
            attempts.append(started["attemptId"])
            h.advance(1)  # Distinct due times fix the wake (and so the write) order.
    state = DynamoStateRepository(store.client, store.table, clock=h.clock, max_conflict_retries=4)
    DynamoRelayProgress(state, queue_url=HARNESS_QUEUE_URL, **RELAY_SCOPE).initialize()
    context = SimpleNamespace(get_remaining_time_in_millis=lambda: 100_000)

    def relay():
        settings = RelaySettings(h.api_settings.state, HARNESS_QUEUE_URL, h.worker_settings.lease_seconds,
                                 h.worker_settings.retry_seconds, 1, 4)
        target = build_relay(settings, dynamodb_client=store.client, sqs_client=h.queue, clock=h.clock,
                             progress_scope=dict(RELAY_SCOPE))
        target.processing_reserve_ms = 100
        target.relay_budget = SimpleNamespace(call_ms=10, step_ms=1000, acquire_ms=20, reserve_ms=100)
        return target.reconcile(context=context)

    first = relay()
    assert first["outbox_wakes"] == 2, first
    assert h.deliver() == {"batchItemFailures": []}
    h.advance(61)
    assert relay() == {"outbox_wakes": 0, "job_wakes": 0, "failures": 0}
    for attempt_id in attempts:
        h.result(session.token, attempt_id)


def flow_vcc_course_lifecycle(store, recorder):
    """Assigned fixture learner: video/document starts and reports, trainings, final cancel/fail/pass.

    The fixture learner's SESSION/USER rows and first refresh are the runtime()
    setup (excluded). Covers COURSE_START/START/REPORT/ITEM rows and the
    final-assessment cancel that releases its own FINAL.
    """
    from mock_journey.handler import handle
    from tests.vcc_runtime_support import measurement_event, runtime
    from tests.vcc_support import event

    context = SimpleNamespace(aws_request_id="write-baseline")
    with recorder.excluded_calls():
        env = runtime(store.client, store.table)

    def request(method, path, *, expected, body=None, query=None):
        response = handle(event(method, path, token=env.token, body=body, query=query), context, env.app)
        assert response["statusCode"] == expected, response["body"]
        return json.loads(response["body"])["data"] if response["body"] else None

    def start(placement, *, content=False):
        definition = request("GET", "/api/v2/courses/101/progress/", expected=200,
                             query={"enrollmentId": "501"})["definitionHash"]
        body = {"clientRequestId": str(uuid.uuid4()), "courseId": 101, "enrollmentId": 501,
                "courseItemLinkId": placement, "definitionHash": definition}
        path = "/api/v2/learning-starts/" if content else "/api/v2/attempts/"
        return request("POST", path, body=body, expected=201), body

    def report(started, content_event, *, report_id=None):
        body = {"enrollmentId": 501, "courseItemLinkId": started["courseItemLinkId"],
                "startId": started["startId"], "reportId": report_id or str(uuid.uuid4()),
                "contentVersion": started["contentVersion"], "event": content_event}
        return request("PUT", "/api/v2/courses/101/progress/", body=body, expected=200), body

    def calculate(attempt, *, count=None):
        stored = env.app.state.get_attempt(env.auth, attempt["attemptId"])
        upload = measurement_event(stored, count=count)
        upload["path"] = f"/api/v2/attempts/{attempt['attemptId']}/calculation/"
        upload["headers"]["Authorization"] = f"Bearer {env.token}"
        assert handle(upload, context, env.app)["statusCode"] == 202
        env.now[0] += 1
        job_id = env.app.state.get_attempt(env.auth, attempt["attemptId"])["job_id"]
        assert env.worker.process(job_id) is True
        return request("GET", upload["path"], expected=200)

    video, _ = start(1001, content=True)
    first, first_body = report(video, {"type": "video_segments", "intervalsMs": [[0, 10000]]})
    report(video, {"type": "video_segments", "intervalsMs": [[10000, 20000]]})
    assert request("PUT", "/api/v2/courses/101/progress/", body=first_body, expected=200) == first
    document, _ = start(1002, content=True)
    display_id = str(uuid.uuid4())
    report(document, {"type": "document_confirmed", "displayReportId": display_id})
    report(document, {"type": "document_displayed"}, report_id=display_id)
    for placement in (1003, 1004):
        practice, _ = start(placement)
        assert calculate(practice)["evaluation"]["program_completed"] is True
    cancelled, _ = start(1005)
    request("POST", f"/api/v2/attempts/{cancelled['attemptId']}/cancel/", body={"reason": "user_cancelled"},
            expected=204)
    failed, _ = start(1005)
    assert calculate(failed, count=2)["evaluation"]["program_completed"] is False
    final, final_body = start(1005)
    assert calculate(final)["evaluation"]["program_completed"] is True
    assert request("POST", "/api/v2/attempts/", body=final_body, expected=200)["attemptId"] == final["attemptId"]


FLOWS = {
    "golden_flow": flow_golden,
    "legacy_active_session": flow_legacy_active_session,
    "legacy_new_session": flow_legacy_new_session,
    "legacy_restart_limit": flow_legacy_restart_limit,
    "course_restart_limit_error": flow_course_restart_limit_error,
    "course_restart_limit_killed": flow_course_restart_limit_killed,
    "relay_checkpoint": flow_relay_checkpoint,
    "vcc_course_lifecycle": flow_vcc_course_lifecycle,
}


def observe(name, store=None):
    """Run one flow on ``store`` (a fresh in-memory table by default); return its write requests."""
    store = store if store is not None else JourneyStore.memory()
    recorder = WriteRecorder(store)
    FLOWS[name](store, recorder)
    return recorder.writes()


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

# A decoded GSI cursor (Relay checkpoint scans) keeps the key order of the query
# response, which the in-memory table derives from a set; it is not code order.
_CURSOR_KEYS = frozenset({"PK", "SK", "GSI1PK", "GSI1SK"})


def _match_key_order(expected, observed, path):
    if type(expected) is dict:
        if list(expected) != list(observed) and not set(expected) == set(observed) == _CURSOR_KEYS:
            raise AssertionError(f"{path}: key order differs {list(observed)}")
        for key in expected:
            _match_key_order(expected[key], observed[key], f"{path}.{key}")
    elif type(expected) is list:
        for index, (left, right) in enumerate(zip(expected, observed)):
            _match_key_order(left, right, f"{path}[{index}]")


def assert_writes_match(template, observed, name="writes", *, key_order=True):
    """Placeholder-bound match of one flow's requests, then the exact key order of every object.

    ``key_order=False`` is for a real DynamoDB backend: it returns attribute
    maps in its own order, so a row rebuilt from a read (``{**row, ...}``)
    carries that order into the next Put.
    """
    _match(template, observed, _Bindings(), name)
    if key_order:
        _match_key_order(template, observed, name)

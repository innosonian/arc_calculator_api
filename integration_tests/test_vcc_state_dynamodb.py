"""Course repository and login against DynamoDB Local. No user DB reuse.

Legacy attempts are the captured /mock/v1 rows (tests/legacy_rows_support.py);
sessions for the fixture learner are seeded in the current row shape (a USER
row without slots, design Q10), never through the removed slot-list API.
"""

from pathlib import Path
import json
import uuid

from tests.vcc_application_support import (  # noqa: F401 (re-export)
    BUNDLE, CONTEXT, MAPPING, application, definitions, execution, login,
)
from mock_journey.course_contracts import ContentReport, StartCommand
from mock_journey.course_errors import CourseError
from mock_journey.course_fixture import FixtureCourseProvider
from mock_journey.course_policy import CoursePolicy, scope_key
from mock_journey.course_provider import UnavailableCourseProvider
from mock_journey.course_settings import fixture_course_settings
from mock_journey.course_state import DynamoCourseRepository
from tests.course_store_fakes import InMemoryBlobStore
from mock_journey.handler import handle
from mock_journey.state import DynamoCourseStore, DynamoStateRepository
from mock_journey.typed import parse_json
from tests.journey_support import JourneyStore
from tests.legacy_attempt_seeds import seed_session_without_slots
from tests.legacy_rows_support import seed_legacy_rows
from tests.vcc_support import event


DATA = Path(__file__).resolve().parents[1]


def test_dummy_login_ready_empty_and_old_routes_404(dynamodb_client, dynamodb_table):
    app = application(dynamodb_client, dynamodb_table)
    token, data = login(app)
    assert data["learningAvailability"]["state"] == "ready"
    listed = handle(event("GET", "/api/v2/courses/progress/", token=token, query={"page": "1", "pageSize": "10"}),
                    CONTEXT, app)
    assert listed["statusCode"] == 200
    payload = json.loads(listed["body"])["data"]
    assert payload["count"] == 0
    assert payload["results"] == []
    replay = handle(event("GET", "/api/v2/courses/progress/", token=token, query={"page": "1", "pageSize": "10"}),
                    CONTEXT, app)
    assert json.loads(replay["body"])["data"]["count"] == 0
    old = handle(event("GET", "/mock/v1/programs", token=token), CONTEXT, app)
    assert old["statusCode"] == 404
    alias = handle(event("POST", "/cpr-analysis", token=token, body="{}"), CONTEXT, app)
    assert alias["statusCode"] == 404


def test_get_does_not_begin_inventory(dynamodb_client, dynamodb_table):
    app = application(dynamodb_client, dynamodb_table)
    token, _ = login(app)
    repo = app.repository
    original = repo.begin_inventory
    calls = []

    def wrapped(*args, **kwargs):
        calls.append("begin")
        return original(*args, **kwargs)

    repo.begin_inventory = wrapped
    handle(event("GET", "/api/v2/courses/progress/", token=token, query={"page": "1", "pageSize": "10"}),
           CONTEXT, app)
    assert calls == []


def test_unavailable_provider_keeps_session_waiting(dynamodb_client, dynamodb_table):
    app = application(dynamodb_client, dynamodb_table, provider=UnavailableCourseProvider())
    token, data = login(app)
    assert data["learningAvailability"]["state"] == "waiting"
    assert data["learningAvailability"]["reason"] == "contract_pending"
    listed = handle(event("GET", "/api/v2/courses/progress/", token=token, query={"page": "1", "pageSize": "10"}),
                    CONTEXT, app)
    assert listed["statusCode"] == 503
    assert json.loads(listed["body"])["error"]["code"] == "ARC_PROGRESS_UNAVAILABLE"


def test_legacy_attempt_get_without_provider(dynamodb_client, dynamodb_table):
    app = application(dynamodb_client, dynamodb_table, provider=UnavailableCourseProvider())
    store = JourneyStore(dynamodb_client, dynamodb_table)
    seeded = seed_legacy_rows(store)
    token = seeded.issue_session_token()
    before = store.rows()
    for label, captured in seeded.attempts.items():
        response = handle(event("GET", f"/api/v2/attempts/{captured['attempt_id']}/", token=token), CONTEXT, app)
        assert response["statusCode"] == 200, (label, response["body"])
        data = json.loads(response["body"])["data"]
        assert data["attemptId"] == captured["attempt_id"]
        assert data["state"] == captured["row"]["state"]
        assert data["courseId"] is None
        assert data["enrollmentId"] is None
        assert data["courseItemLinkId"] is None
        assert data["definitionHash"] is None
        assert data["role"] is None
        assert data["condition"] == json.loads(captured["row"]["definition_json"])["condition"]
        assert data["condition"]["target"] == "adult"
    assert store.rows() == before  # A read never rewrites legacy rows (R2/R4).


def fixture_learner_session(dynamodb_client, dynamodb_table, token_hash):
    return seed_session_without_slots(
        dynamodb_client, dynamodb_table, clock=lambda: 1_800_000_000,
        principal=BUNDLE["learners"]["real"]["principal"], token_hash=token_hash, expires_at=1_808_640_000,
    )


def test_video_intervals_complete_and_historical_null_on_old_epoch(dynamodb_client, dynamodb_table):
    settings = fixture_course_settings()
    policy = CoursePolicy(settings)
    state = DynamoStateRepository(dynamodb_client, dynamodb_table, clock=lambda: 1_800_000_000)
    store = DynamoCourseStore(state)
    repo = DynamoCourseRepository(store, settings, policy, InMemoryBlobStore(),
                                  clock=lambda: 1_800_000_000, uuid_factory=lambda: str(uuid.uuid4()))
    provider = FixtureCourseProvider(document=BUNDLE, settings=settings, mapping_document=MAPPING)
    auth = fixture_learner_session(dynamodb_client, dynamodb_table, "a" * 64)
    learner = provider.resolve_learner(auth)
    ticket = repo.begin_inventory(auth, learner)
    assignments = provider.list_assignments(learner)
    inventory = repo.apply_inventory(auth, ticket, assignments)
    assert inventory.state == "ready"
    binding = assignments[0]
    repo.ensure_epoch(auth, binding)
    refresh = repo.begin_refresh(auth, binding, ticket)
    bundle = provider.fetch_bundle(binding)
    gate = repo.apply_refresh(auth, refresh, bundle)
    assert gate.state == "ready"
    command = StartCommand(str(uuid.uuid4()), binding.public_ids.enrollment_id, binding.public_ids.course_id,
                           1001, bundle.definition_hash)
    start = repo.start(auth, command, kind="content", view=repo.load_start_view(auth, command), template=None)
    report_a = ContentReport(str(uuid.uuid4()), start.start_id, bundle.placements[0].content_version,
                             "video_segments", ((0, 10000),), None)
    first = repo.report(auth, course_id=command.course_id, enrollment_id=command.enrollment_id,
                        placement_id=1001, report=report_a)
    report_b = ContentReport(str(uuid.uuid4()), start.start_id, bundle.placements[0].content_version,
                             "video_segments", ((10000, 20000),), None)
    second = repo.report(auth, course_id=command.course_id, enrollment_id=command.enrollment_id,
                         placement_id=1001, report=report_b)
    payload = parse_json(second.response_json)
    assert payload["isCompleted"] is True
    replay = repo.report(auth, course_id=command.course_id, enrollment_id=command.enrollment_id,
                         placement_id=1001, report=report_b)
    assert parse_json(replay.response_json)["reportId"] == payload["reportId"]


def test_head_and_final_init_together(dynamodb_client, dynamodb_table):
    settings = fixture_course_settings()
    policy = CoursePolicy(settings)
    clock = lambda: 1_800_000_000
    state = DynamoStateRepository(dynamodb_client, dynamodb_table, clock=clock, max_conflict_retries=4)
    store = DynamoCourseStore(state)
    repo = DynamoCourseRepository(store, settings, policy, InMemoryBlobStore(),
                                  clock=clock, uuid_factory=lambda: str(uuid.uuid4()))
    provider = FixtureCourseProvider(document=BUNDLE, settings=settings, mapping_document=MAPPING)
    auth = fixture_learner_session(dynamodb_client, dynamodb_table, "b" * 64)
    learner = provider.resolve_learner(auth)
    ticket = repo.begin_inventory(auth, learner)
    assignments = provider.list_assignments(learner)
    repo.apply_inventory(auth, ticket, assignments)
    binding = assignments[0]
    gate = repo.ensure_epoch(auth, binding)
    assert gate.state in {"waiting", "ready"}
    pk = f"COURSE#{scope_key(binding.scope)}"
    epoch = JourneyStore(dynamodb_client, dynamodb_table).row(f"USER#{auth.principal}", "STATE")["epoch"]
    head = store.get_item({"PK": pk, "SK": f"EPOCH#{epoch}#HEAD"})
    final = store.get_item({"PK": pk, "SK": f"EPOCH#{epoch}#FINAL"})
    assert head is not None and final is not None
    assert final.get("phase") == "free"
    user = dynamodb_client.delete_item(
        TableName=dynamodb_table, Key={"PK": {"S": pk}, "SK": {"S": f"EPOCH#{epoch}#HEAD"}},
        ReturnValues="ALL_OLD",
    )
    assert user
    try:
        repo.ensure_epoch(auth, binding)
        raise AssertionError("one-row corruption must be 503")
    except CourseError as error:
        assert error.code == "TEMPORARILY_UNAVAILABLE"

"""Course repository and login against DynamoDB Local. No user DB reuse."""

from pathlib import Path
import json
from types import SimpleNamespace
import uuid

from mock_journey.assembly import ExecutionCatalog, build_course_application
from mock_journey.catalog import PROGRAMS, TARGETS, slot_key
from mock_journey.course_contracts import ContentReport, StartCommand
from mock_journey.course_errors import CourseError
from mock_journey.course_fixture import FixtureCourseProvider
from mock_journey.course_policy import CoursePolicy, learner_key, placement_key, scope_key
from mock_journey.course_provider import UnavailableCourseProvider
from mock_journey.course_settings import fixture_course_settings
from mock_journey.course_state import DynamoCourseRepository, InMemoryBlobStore
from mock_journey.handler import handle
from mock_journey.models import AuthContext
from mock_journey.projection import ProjectionSchema
from mock_journey.settings import ApiSettings, StateSettings, StorageSettings
from mock_journey.state import DynamoCourseStore, DynamoStateRepository
from mock_journey.typed import parse_json
from tests.mock_storage_support import MemoryLegacyBindings, MemoryS3
from tests.vcc_support import dummy_learner, event, load_fixture, mapping_document


DATA = Path(__file__).resolve().parents[1]
BUNDLE = load_fixture("course_bundle.json")
MAPPING = mapping_document()
CONTEXT = SimpleNamespace(aws_request_id="vcc-ddb")


def definitions():
    return {slot_key(program[0], target): {
        "condition": {"target": target}, "calculation_profile": {
            "Custom": {"PassThreshold": 80.0, "CertificateAdult": False}},
        "profile_version": "test-profile", "adapter_version": "test-adapter",
        "projection_version": "test-projection",
    } for program in PROGRAMS for target in TARGETS}


def execution():
    return ExecutionCatalog(definitions(), {
        "test-projection": ProjectionSchema("test-projection", {"CompressionDepth": {"value": "scalar"}}),
    })


def application(client, table, *, provider=None, clock=None):
    now = clock if clock is not None else (lambda: 1_800_000_000)
    objects = MemoryS3()
    bindings = MemoryLegacyBindings(objects)
    state = StateSettings(table, 4)
    storage = StorageSettings("development", bindings.bucket, bindings.directory, 1000000, 2000000)
    settings = ApiSettings(state, storage, "vcc-ddb", 2000000)
    keys = {"v1": b"K" * 32}
    source = provider
    if source is None:
        source = FixtureCourseProvider(
            document=BUNDLE, settings=fixture_course_settings(), mapping_document=MAPPING,
        )
    return build_course_application(
        settings, dynamodb_client=client, s3_client=objects, legacy_bindings=bindings,
        resume_keys=keys, current_key_version="v1", execution=execution(), provider=source,
        course_settings=fixture_course_settings(), clock=now, mapping_document=MAPPING,
        dummy_learner=dummy_learner(),
    )


def login(app):
    response = handle(event("POST", "/api/v2/sessions/", body={
        "loginId": "test@test.com", "password": "2222",
    }), CONTEXT, app)
    body = json.loads(response["body"])
    assert response["statusCode"] == 201, body
    return body["data"]["accessToken"], body["data"]


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
    token, _ = login(app)
    auth = app.auth.authenticate(token)
    template = app.auth.prepare_resume({
        "attempt_id": str(uuid.uuid4()), "principal": auth.principal,
        "creator_session_id": auth.session_id, "bound_session_id": auth.session_id,
        "program_id": "mock-cpr", "target": "adult", "profile_name": "tester",
        "definition_json": json.dumps({
            "condition": {
                "mode": "training", "target": "adult", "training_type": "cpr",
                "guideline": "ARC2025", "cpr_cycle_type": "302", "is_2rescuers": False,
            },
            "calculation_profile": {}, "profile_version": "tester",
            "adapter_version": "test-adapter", "projection_version": "test-projection",
            "goal": {"kind": "cycles", "required": 3}, "catalog_version": "mock-catalog-v1",
        }),
    })
    attempt = app.state.create_attempt(auth, str(uuid.uuid4()), "digest-a", template)
    response = handle(event("GET", f"/api/v2/attempts/{attempt['attempt_id']}/", token=token), CONTEXT, app)
    assert response["statusCode"] == 200
    data = json.loads(response["body"])["data"]
    assert data["courseId"] is None
    assert data["enrollmentId"] is None
    assert data["courseItemLinkId"] is None
    assert data["definitionHash"] is None
    assert data["role"] is None
    assert data["condition"]["target"] == "adult"


def test_video_intervals_complete_and_historical_null_on_old_epoch(dynamodb_client, dynamodb_table):
    settings = fixture_course_settings()
    policy = CoursePolicy(settings)
    state = DynamoStateRepository(dynamodb_client, dynamodb_table, clock=lambda: 1_800_000_000)
    store = DynamoCourseStore(state)
    repo = DynamoCourseRepository(store, settings, policy, InMemoryBlobStore(),
                                  clock=lambda: 1_800_000_000, uuid_factory=lambda: str(uuid.uuid4()))
    provider = FixtureCourseProvider(document=BUNDLE, settings=settings, mapping_document=MAPPING)
    session = {
        "session_id": str(uuid.uuid4()), "principal": BUNDLE["learners"]["real"]["principal"],
        "token_hash": "a" * 64, "issued_at": 1_800_000_000, "expires_at": 1_808_640_000,
        "status": "active", "revision": 0,
    }
    state.create_session(session, [slot_key(program[0], target) for program in PROGRAMS for target in TARGETS])
    auth = AuthContext(session["session_id"], session["principal"], 0, session["expires_at"])
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
    session = {
        "session_id": str(uuid.uuid4()), "principal": BUNDLE["learners"]["real"]["principal"],
        "token_hash": "b" * 64, "issued_at": 1_800_000_000, "expires_at": 1_808_640_000,
        "status": "active", "revision": 0,
    }
    state.create_session(session, [slot_key(program[0], target) for program in PROGRAMS for target in TARGETS])
    auth = AuthContext(session["session_id"], session["principal"], 0, session["expires_at"])
    learner = provider.resolve_learner(auth)
    ticket = repo.begin_inventory(auth, learner)
    assignments = provider.list_assignments(learner)
    repo.apply_inventory(auth, ticket, assignments)
    binding = assignments[0]
    gate = repo.ensure_epoch(auth, binding)
    assert gate.state in {"waiting", "ready"}
    pk = f"COURSE#{scope_key(binding.scope)}"
    epoch = state.get_progress(auth)["epoch"]
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

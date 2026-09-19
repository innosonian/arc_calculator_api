"""Audit A01–A06 counterexamples, with independent typed values and stored state."""

from copy import deepcopy
from dataclasses import replace

import pytest

from mock_journey.course_contracts import RefreshResult, StartCommand
from mock_journey.course_errors import CourseError
from mock_journey.course_fixture import FixtureCourseProvider
from mock_journey.course_provider import validate_bundle
from mock_journey.course_response import aggregate_availability, calculation_view_data, course_detail_data, item_detail_data
from mock_journey.course_service import CourseService
from mock_journey.course_settings import fixture_course_settings
from mock_journey.typed import digest, parse_json
from tests.test_vcc_api import World, decode, event, view_of
from tests.test_vcc_provider import BUNDLE_DOC, make_auth, real_principal
from tests.test_vcc_state import REQUEST, repository, seed_auth


def source_bundle(document=None):
    source = FixtureCourseProvider(document=document or BUNDLE_DOC, settings=fixture_course_settings())
    learner = source.resolve_learner(make_auth(real_principal()))
    bundle = source.fetch_bundle(source.list_assignments(learner)[0])
    return source, bundle


def change_at(value, path, replacement):
    for key in path[:-1]:
        value = value[key]
    value[path[-1]] = replacement


@pytest.mark.parametrize("placement_index,path,bad", [
    (0, ("id",), True),
    (0, ("id",), 999),
    (0, ("courseItemLinkId",), 1002),
    (0, ("displayOrder",), True),
    (0, ("displayOrder",), 99),
    (0, ("description",), {}),
    (0, ("detail", "id"), True),
    (0, ("detail", "fileName"), {}),
    (0, ("detail", "order"), True),
    (0, ("detail", "url"), False),
    (0, ("detail", "contentUrl"), []),
    (2, ("detail", "id"), "401"),
    (2, ("detail", "trainingType"), None),
    (2, ("detail", "training"), None),
    (2, ("detail", "training", "manikinType"), {}),
    (2, ("detail", "training", "duration"), True),
    (2, ("detail", "training", "compressionLimit"), "60"),
    (2, ("detail", "training", "compressionVentilationRatio"), {}),
    (2, ("detail", "training", "aed"), {}),
    (2, ("detail", "training", "cprGuideline"), {}),
    (2, ("detail", "training", "twoRescuers"), {"cycleChangeCount": False}),
    (2, ("detail", "assessment", "passThreshold", "cpr"), 80.5),
    (2, ("detail", "assessment", "passThreshold", "aed"), True),
    (2, ("detail", "content"), {}),
    (2, ("detail", "content"), [{"id": 1, "fileName": "file", "order": 1, "url": False, "contentUrl": None}]),
])
def test_bad_nested_wire_is_rejected_by_provider_and_serializer(placement_index, path, bad):
    _, bundle = source_bundle()
    placement = bundle.placements[placement_index]
    detail = parse_json(placement.detail_json)
    change_at(detail, path, bad)
    mutated = replace(placement, detail_json=detail)
    placements = list(bundle.placements)
    placements[placement_index] = mutated
    invalid = replace(bundle, placements=tuple(placements))
    with pytest.raises(CourseError) as raised:
        validate_bundle(invalid, fixture_course_settings())
    assert raised.value.code == "UPSTREAM_CONTRACT_MISMATCH"
    view, _, _ = view_of(0, placements=placements)
    with pytest.raises(CourseError) as raised:
        item_detail_data(view, placement.public_link_id)
    assert raised.value.code == "UPSTREAM_CONTRACT_MISMATCH"


@pytest.mark.parametrize("key,value", [
    ("id", True), ("id", 999), ("courseId", 102), ("status", False),
    ("courseTitle", None), ("loginAt", []), ("finishedAt", 1),
    ("elapsedSeconds", True), ("centerName", {}), ("enrollStatusCode", 1),
])
def test_bad_enrollment_is_rejected_before_ios_decode(key, value):
    _, bundle = source_bundle()
    course = parse_json(bundle.course_json)
    course["enrollment_metadata"]["501"][key] = value
    invalid = replace(bundle, course_json=course)
    with pytest.raises(CourseError) as raised:
        validate_bundle(invalid, fixture_course_settings())
    assert raised.value.code == "UPSTREAM_CONTRACT_MISMATCH"
    view, _, _ = view_of(0)
    with pytest.raises(CourseError) as raised:
        course_detail_data(replace(view, bundle=invalid))
    assert raised.value.code == "UPSTREAM_CONTRACT_MISMATCH"


def test_nullable_and_nonnull_training_domains_keep_exact_valid_values():
    document = deepcopy(BUNDLE_DOC)
    detail = document["placements"][2]["detail"]["detail"]
    detail["training"] = {
        "manikinType": None, "duration": 0, "compressionLimit": None,
        "ventilationLimit": 8, "cycleLimit": None,
        "compressionVentilationRatio": {"title": None, "cvrVentilation": 2, "cvrCompression": 30},
        "aed": {"id": 1, "cprGuide": "guide", "shockMode": "mode", "scenarioNo": 1,
                "volume": 0, "language": "en", "padsDetection": False, "arrivalSeconds": 0},
        "cprGuideline": {
            "title": None, "manikinType": None, "name": None,
            "compressionDepthMax": None, "compressionDepthMin": None,
            "compressionRateMax": None, "compressionRateMin": None,
            "ventilationVolumeMax": None, "ventilationVolumeMin": None,
            "ventilationRateMax": None, "ventilationRateMin": None,
            "compressionDepthMaxInch": 2.4, "compressionDepthMinInch": 2,
        },
        "twoRescuers": {"cycleChangeCount": 0},
    }
    detail["assessment"] = {"passThreshold": {"cpr": 80, "aed": None}}
    detail["content"] = [{"id": 1, "fileName": "guide", "order": 0, "url": None, "contentUrl": "content"}]
    _, bundle = source_bundle(document)
    view, _, _ = view_of(0, placements=bundle.placements)
    assert item_detail_data(view, 1003)["detail"] == detail


@pytest.mark.parametrize("remove_path", [("detail", "url"), ("description",)])
def test_missing_nullable_is_not_synthesized(remove_path):
    document = deepcopy(BUNDLE_DOC)
    target = document["placements"][0]["detail"]
    for key in remove_path[:-1]:
        target = target[key]
    target.pop(remove_path[-1])
    with pytest.raises(CourseError) as raised:
        source_bundle(document)
    assert raised.value.code == "UPSTREAM_CONTRACT_MISMATCH"


def repeated_course_item_document():
    document = deepcopy(BUNDLE_DOC)
    first, repeated = document["placements"][2:4]
    repeated["public_item_id"] = first["public_item_id"]
    repeated["detail"] = {**deepcopy(first["detail"]), "displayOrder": 4, "courseItemLinkId": 1004}
    document["id_map"]["placements"][3]["public_item_id"] = first["public_item_id"]
    return document


def test_same_course_item_has_two_distinct_placements_and_independent_wire_progress():
    _, bundle = source_bundle(repeated_course_item_document())
    first, second = bundle.placements[2:4]
    assert first.public_item_id == second.public_item_id == 203
    assert first.public_link_id == 1003 and second.public_link_id == 1004
    view, _, _ = view_of(0, placements=bundle.placements)
    progress = parse_json(view.progress_json)
    progress["items"][digest([view.scope_key, first.source_id])].update(completed=True, passed=True)
    progress["course_status"] = "IN_PROGRESS"
    wire = course_detail_data(replace(view, progress_json=progress))["courseItems"]
    assert wire[2]["isCompleted"] is True
    assert wire[3]["isCompleted"] is False
    assert wire[2]["id"] == wire[3]["id"]
    assert wire[2]["courseItemLinkId"] != wire[3]["courseItemLinkId"]


def test_same_item_with_conflicting_content_is_a_contract_error():
    document = repeated_course_item_document()
    document["placements"][3]["detail"]["detail"]["id"] = 999
    with pytest.raises(CourseError) as raised:
        source_bundle(document)
    assert raised.value.code == "UPSTREAM_CONTRACT_MISMATCH"


def test_out_of_order_positions_are_not_reordered_into_a_different_final_role():
    document = deepcopy(BUNDLE_DOC)
    document["placements"][0]["position"] = 99
    document["placements"][0]["detail"]["displayOrder"] = 99
    with pytest.raises(CourseError) as raised:
        source_bundle(document)
    assert raised.value.code == "UPSTREAM_CONTRACT_MISMATCH"


@pytest.mark.parametrize("field,value", [("kind", []), ("source_id", {}), ("public_item_id", True)])
def test_malformed_normalized_identity_cannot_be_coerced(field, value):
    document = deepcopy(BUNDLE_DOC)
    document["placements"][0][field] = value
    with pytest.raises(CourseError) as raised:
        source_bundle(document)
    assert raised.value.code == "UPSTREAM_CONTRACT_MISMATCH"


@pytest.mark.parametrize("event_type", [[], {}, True, False, 1, 1.0, None])
def test_invalid_event_enum_type_is_400_without_repository_write(event_type):
    world = World()
    body = {
        "enrollmentId": 501, "courseItemLinkId": 1001,
        "startId": "40000000-0000-4000-8000-000000000001",
        "reportId": "30000000-0000-4000-8000-000000000001",
        "contentVersion": "video-v1", "event": {"type": event_type},
    }
    response = world.http.dispatch(event("PUT", "/api/v2/courses/101/progress/", body=body))
    assert response["statusCode"] == 400
    assert decode(response)["error"]["code"] == "INVALID_REQUEST"
    assert "report" not in world.repository.calls
    assert not world.repository.reports


@pytest.mark.parametrize("failure", [CourseError("CONTRACT_PENDING"), RuntimeError("untrusted upstream")])
def test_resolve_failure_persists_waiting_and_blocks_new_start(failure):
    source, bundle = source_bundle()
    repo, store, _ = repository()
    auth = seed_auth(store)
    service = CourseService(source, repo)
    assert service.refresh_for_session(auth).inventory.state == "ready"

    class FailingSource:
        def resolve_learner(self, _auth):
            raise failure

    failing = CourseService(FailingSource(), repo)
    result = failing.refresh_for_session(auth)
    assert result.inventory.state == "waiting"
    assert repo.load_inventory_for_session(auth).state == "waiting"
    with pytest.raises(CourseError) as raised:
        failing.start_content(auth, StartCommand(REQUEST, 501, 101, 1001, bundle.definition_hash))
    assert raised.value.code == "ARC_PROGRESS_UNAVAILABLE"


def test_delayed_resolve_failure_cannot_overwrite_newer_success():
    source, _ = source_bundle()
    repo, store, _ = repository()
    auth = seed_auth(store)
    good = CourseService(source, repo)
    good.refresh_for_session(auth)

    class DelayedFailure:
        def resolve_learner(self, _auth):
            good.refresh_for_session(auth)
            raise CourseError("CONTRACT_PENDING")

    result = CourseService(DelayedFailure(), repo).refresh_for_session(auth)
    assert result.inventory.state == "ready"
    assert repo.load_inventory_for_session(auth).state == "ready"
    assert aggregate_availability(result) == {"state": "ready", "reason": None}


def test_session_aggregation_does_not_invent_ready_for_missing_gate():
    view, binding, _ = view_of(0)
    assert aggregate_availability(RefreshResult(view.inventory, ())) == {
        "state": "waiting", "reason": "arc_progress_unavailable",
    }
    assert aggregate_availability(RefreshResult(view.inventory, (view.gate,))) == {"state": "ready", "reason": None}


def test_get_courses_never_calls_resolve_learner():
    world = World()
    response = world.http.dispatch(event("GET", "/api/v2/courses/progress/"))
    assert response["statusCode"] == 200
    assert world.provider.calls == []


def test_stored_refresh_without_course_head_is_waiting_and_never_calls_provider():
    source, bundle = source_bundle()
    repo, store, _ = repository()
    auth = seed_auth(store)
    learner = bundle.scope.learner
    ticket = repo.begin_inventory(auth, learner)
    repo.apply_inventory(auth, ticket, source.list_assignments(learner))

    class NeverCalled:
        def resolve_learner(self, _auth):
            raise AssertionError("GET must not resolve")

    result = CourseService(NeverCalled(), repo).stored_refresh(auth)
    assert result.inventory.state == "ready"
    assert aggregate_availability(result) == {"state": "waiting", "reason": "arc_progress_unavailable"}


def test_stored_refresh_uses_course_gate_after_successful_inventory():
    source, bundle = source_bundle()
    repo, store, _ = repository()
    auth = seed_auth(store)
    service = CourseService(source, repo)
    initial = service.refresh_for_session(auth)
    ticket = repo.begin_refresh(auth, initial.inventory.assignments[0],
                                repo.begin_inventory_for_session(auth))
    repo.apply_refresh(auth, ticket, CourseError("CONTRACT_PENDING"))
    stored = service.stored_refresh(auth)
    assert stored.inventory.state == "ready"
    assert aggregate_availability(stored) == {"state": "waiting", "reason": "contract_pending"}


def test_ios_wire_preserves_nullable_progress_and_separate_placement_identity():
    world = World()
    response = world.http.dispatch(event("GET", "/api/v2/courses/101/progress/",
                                         query={"enrollmentId": "501"}))
    assert response["statusCode"] == 200
    body = decode(response)
    assert body["success"] is True
    assert type(body["timestamp"]) is str
    rows = body["data"]["courseItems"]
    assert len({row["courseItemLinkId"] for row in rows}) == 5
    for row in rows:
        assert type(row["id"]) is int
        assert type(row["courseItemLinkId"]) is int
        assert type(row["isCompleted"]) is bool
        assert row["isPassed"] is None
        assert row["description"] is None
    enrollment = body["data"]["enrollment"]
    for key in ("loginAt", "finishedAt", "elapsedSeconds", "centerName", "enrollStatusCode"):
        assert key in enrollment and enrollment[key] is None
    assert body["data"]["learningAvailability"] == {"state": "ready", "reason": None}


@pytest.mark.parametrize("change", ["missing_item", "wrong_link", "wrong_source", "bool_complete", "old_wire_shape"])
def test_unknown_progress_shape_is_not_projected_as_false(change):
    view, _, _ = view_of(0)
    progress = parse_json(view.progress_json)
    key = digest([view.scope_key, view.bundle.placements[0].source_id])
    if change == "missing_item":
        progress["items"].pop(key)
    elif change == "wrong_link":
        progress["items"][key]["public_link_id"] = 999
    elif change == "wrong_source":
        progress["items"][key]["source_id"] = "other-placement"
    elif change == "bool_complete":
        progress["items"][key]["completed"] = 1
    else:
        progress = {"status": "NOT_STARTED", "items": {"1001": {"isCompleted": False, "isPassed": None}}}
    with pytest.raises(CourseError) as raised:
        course_detail_data(replace(view, progress_json=progress))
    assert raised.value.code == "UPSTREAM_CONTRACT_MISMATCH"


@pytest.mark.parametrize("missing", ["calculation", "evaluation", "progress_application"])
def test_succeeded_calculation_cannot_hide_a_missing_stored_component(missing):
    components = {"calculation": {}, "evaluation": {}, "progress_application": {}}
    components[missing] = None
    with pytest.raises(CourseError) as raised:
        calculation_view_data(attempt_id="50000000-0000-4000-8000-000000000001",
                              calculation_status="succeeded", **components)
    assert raised.value.code == "STORED_INPUT_INVALID"

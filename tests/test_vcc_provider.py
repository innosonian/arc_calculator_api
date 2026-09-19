"""VCC course provider unit tests. Fixture load matches W0; DTO shapes are not duplicated."""

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import json
import subprocess
import sys
import threading

import pytest

from mock_journey.catalog import PROGRAMS
from mock_journey.course_contracts import (
    CONTRACT_VERSION, EXECUTION_KEYS, FIXTURE_CATALOG_VERSION, FIXTURE_MAPPING_VERSION,
    ITEM_TYPE_WIRE, LearnerContext, MappingRegistry, AssignmentBinding, CourseScope, PublicIds,
    definition_digest, definition_identity, item_type_wire, owned_json_bytes,
)
from mock_journey.course_errors import CourseError
from mock_journey.course_fixture import FIXTURE_SYMBOLS, FixtureCourseProvider, fixture_mapping_registry
from mock_journey.course_provider import (
    PROVIDER_SYMBOLS, UnavailableCourseProvider, map_execution, validate_assignments,
    validate_bundle,
)
from mock_journey.course_settings import fixture_course_settings
from mock_journey.models import AuthContext
from mock_journey.typed import parse_json


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "vcc_contract" / "v1"
PYTHON = sys.executable
GOLDEN_501 = "5de0ca77a8007471240fb4ec49b413a4b524d0f7adb73613c0da3b6d13c2709c"


def load(name):
    return parse_json((FIXTURES / name).read_bytes())


BUNDLE_DOC = load("course_bundle.json")
MAPPING_DOC = load("execution_mapping.json")
VECTORS = load("typed_id_vectors.json")


def settings():
    return fixture_course_settings()


def make_auth(principal):
    return AuthContext("60000000-0000-4000-8000-000000000001", principal, 1, 2000000000)


def provider(**overrides):
    payload = dict(document=BUNDLE_DOC, settings=settings(), mapping_document=MAPPING_DOC)
    payload.update(overrides)
    return FixtureCourseProvider(**payload)


def real_principal():
    return BUNDLE_DOC["learners"]["real"]["principal"]


def dummy_principal():
    return BUNDLE_DOC["learners"]["dummy"]["principal"]


def vector(case_id):
    return next(item for item in VECTORS["cases"] if item["id"] == case_id)


class TestImportAndConstruction:
    def test_provider_modules_do_not_read_environment_or_open_network(self):
        script = r"""
import os, sys
sys.path.insert(0, ROOT)
class NoEnvironment(dict):
    def __getitem__(self, key): raise AssertionError('Environment read')
    def get(self, key, *args): raise AssertionError('Environment read')
    def __iter__(self): raise AssertionError('Environment read')
    def __contains__(self, key): raise AssertionError('Environment read')
os.environ = NoEnvironment()
def guard(event, args):
    if event.startswith('socket.') or event in ('subprocess.Popen', 'os.system'):
        raise AssertionError('Unexpected I/O')
    if event == 'import' and args[0].split('.')[0] in ('boto3', 'botocore', 'sentry_sdk'):
        raise AssertionError('SDK import')
sys.addaudithook(guard)
from mock_journey.course_provider import UnavailableCourseProvider
from mock_journey.course_fixture import FixtureCourseProvider
from mock_journey.course_errors import CourseError
from mock_journey.models import AuthContext
from mock_journey.course_contracts import LearnerContext, AssignmentBinding, CourseScope, PublicIds
provider = UnavailableCourseProvider()
auth = AuthContext('session', 'anyone', 1, 1)
for call in (
    lambda: provider.resolve_learner(auth),
    lambda: provider.list_assignments(LearnerContext('x', 't', 1, 'anyone', False)),
    lambda: provider.fetch_bundle(AssignmentBinding(
        CourseScope(LearnerContext('x', 't', 1, 'anyone', False), 'e', 'c'),
        PublicIds(101, 501, 601),
    )),
):
    try:
        call()
    except CourseError as error:
        assert error.code == 'CONTRACT_PENDING'
    else:
        raise AssertionError('empty success')
assert FixtureCourseProvider is not UnavailableCourseProvider
""".replace("ROOT", json.dumps(str(ROOT)))
        completed = subprocess.run(
            [PYTHON, "-I", "-S", "-B", "-c", script], env={}, capture_output=True, text=True, timeout=10,
        )
        assert completed.returncode == 0, completed.stderr

    def test_fixture_provider_is_keyword_only_and_not_a_singleton(self):
        with pytest.raises(TypeError):
            FixtureCourseProvider(BUNDLE_DOC, settings())
        first = provider()
        second = provider(list_error="NOT_FOUND")
        learner = first.resolve_learner(make_auth(real_principal()))
        assert len(first.list_assignments(learner)) == 2
        with pytest.raises(CourseError) as raised:
            second.list_assignments(learner)
        assert raised.value.code == "NOT_FOUND"

    def test_public_symbols_and_contract_versions_match_w0(self):
        assert CONTRACT_VERSION == "vcc-internal-v1"
        assert FIXTURE_MAPPING_VERSION == "vcc-fixture-mapping-v1"
        assert FIXTURE_CATALOG_VERSION == "vcc-fixture-catalog-v1"
        assert BUNDLE_DOC["contract_version"] == CONTRACT_VERSION
        assert MAPPING_DOC["mapping_version"] == FIXTURE_MAPPING_VERSION
        for name in PROVIDER_SYMBOLS:
            assert name in dir(__import__("mock_journey.course_provider", fromlist=[name]))
        for name in FIXTURE_SYMBOLS:
            assert name in dir(__import__("mock_journey.course_fixture", fromlist=[name]))
        assert GOLDEN_501 == VECTORS["baseline_digest"]
        assert GOLDEN_501 == BUNDLE_DOC["assignments"][0]["definition_hash"]


class TestUnavailableV04:
    def test_unavailable_is_contract_pending_with_no_empty_success(self):
        source = UnavailableCourseProvider()
        auth = make_auth("anyone@example.test")
        with pytest.raises(CourseError) as raised:
            source.resolve_learner(auth)
        assert raised.value.code == "CONTRACT_PENDING"
        learner = LearnerContext("fixture", "tenant-vcc-fixture", 1, real_principal(), False)
        with pytest.raises(CourseError) as raised:
            source.list_assignments(learner)
        assert raised.value.code == "CONTRACT_PENDING"
        fixture = provider()
        binding = fixture.list_assignments(fixture.resolve_learner(make_auth(real_principal())))[0]
        with pytest.raises(CourseError) as raised:
            source.fetch_bundle(binding)
        assert raised.value.code == "CONTRACT_PENDING"


class TestV02DefinitionHash:
    def test_provider_hash_matches_w0_golden_not_a_captured_output(self):
        source = provider()
        learner = source.resolve_learner(make_auth(real_principal()))
        binding = source.list_assignments(learner)[0]
        bundle = source.fetch_bundle(binding)
        assert binding.public_ids.enrollment_id == 501
        assert bundle.definition_hash == GOLDEN_501
        assert bundle.definition_hash == vector("baseline")["digest"]
        assert definition_digest(bundle) == GOLDEN_501
        assert definition_identity(bundle) == vector("baseline")["projection"]
        sealed = validate_bundle(bundle, settings())
        assert sealed is not bundle
        assert sealed.definition_hash == GOLDEN_501

    def test_url_and_title_changes_do_not_change_definition_hash(self):
        source = provider()
        bundle = source.fetch_bundle(source.list_assignments(source.resolve_learner(make_auth(real_principal())))[0])
        detail = parse_json(bundle.placements[0].detail_json)
        detail["title"] = "Renamed"
        detail["detail"]["url"] = "https://fixture.invalid/video/other.mp4?sig=rotated"
        mutated = replace(
            bundle,
            placements=(replace(bundle.placements[0], detail_json=detail),) + bundle.placements[1:],
            course_json={**parse_json(bundle.course_json), "courseName": "Renamed Course"},
            source_revision="rev-bundle-9",
        )
        sealed = validate_bundle(mutated, settings())
        assert sealed.definition_hash == GOLDEN_501
        assert sealed.definition_hash == vector("display_url_and_title_omitted")["digest"]

    def test_content_order_and_version_changes_use_w0_vectors(self):
        source = provider()
        bundle = source.fetch_bundle(source.list_assignments(source.resolve_learner(make_auth(real_principal())))[0])
        versioned = replace(bundle, placements=(replace(bundle.placements[0], content_version="video-v2"),) + bundle.placements[1:])
        assert validate_bundle(versioned, settings()).definition_hash == vector("content_version_change")["digest"]
        swapped = replace(
            bundle,
            placements=(
                bundle.placements[0],
                bundle.placements[1],
                replace(bundle.placements[3], position=3,
                        detail_json={**parse_json(bundle.placements[3].detail_json), "displayOrder": 3}),
                replace(bundle.placements[2], position=4,
                        detail_json={**parse_json(bundle.placements[2].detail_json), "displayOrder": 4}),
                bundle.placements[4],
            ),
        )
        assert validate_bundle(swapped, settings()).definition_hash == vector("placement_order_change")["digest"]
        assert validate_bundle(swapped, settings()).definition_hash != GOLDEN_501


class TestV03AssignedOnly:
    def test_real_learner_sees_two_distinct_enrollments_of_course_101(self):
        source = provider()
        learner = source.resolve_learner(make_auth(real_principal()))
        assigned = source.list_assignments(learner)
        assert learner.is_dummy is False
        assert type(learner.learner_id) is int and learner.learner_id == 1
        assert len(assigned) == 2
        first, second = assigned
        assert first.public_ids.course_id == second.public_ids.course_id == 101
        assert first.public_ids.enrollment_id == 501
        assert second.public_ids.enrollment_id == 502
        assert first.scope.enrollment_id != second.scope.enrollment_id
        bundles = (source.fetch_bundle(first), source.fetch_bundle(second))
        assert bundles[0].definition_hash == GOLDEN_501
        assert bundles[1].definition_hash == BUNDLE_DOC["assignments"][1]["definition_hash"]
        assert bundles[1].definition_hash == vector("enrollment_502_scope")["digest"]
        assert bundles[0].definition_hash != bundles[1].definition_hash
        kinds = [item.kind for item in bundles[0].placements]
        assert kinds == ["video", "document", "training", "training", "assessment"]
        trainings = [item for item in bundles[0].placements if item.kind == "training"]
        assert trainings[0].public_link_id == 1003
        assert trainings[1].public_link_id == 1004
        assert trainings[0].source_id != trainings[1].source_id
        assert parse_json(trainings[0].content_identity_json)["training_program_id"] == parse_json(
            trainings[1].content_identity_json
        )["training_program_id"]

    def test_dummy_has_no_invented_enrollments_and_unknown_principal_is_not_found(self):
        source = provider()
        dummy = source.resolve_learner(make_auth(dummy_principal()))
        assert dummy.is_dummy is True
        assert type(dummy.learner_id) is str
        assert source.list_assignments(dummy) == ()
        with pytest.raises(CourseError) as raised:
            source.resolve_learner(make_auth("unknown@example.test"))
        assert raised.value.code == "NOT_FOUND"
        real = source.resolve_learner(make_auth(real_principal()))
        binding = source.list_assignments(real)[0]
        foreign = AssignmentBinding(
            CourseScope(dummy, binding.scope.enrollment_id, binding.scope.course_id),
            binding.public_ids,
        )
        with pytest.raises(CourseError) as raised:
            source.fetch_bundle(foreign)
        assert raised.value.code == "NOT_FOUND"
        missing = AssignmentBinding(
            CourseScope(real, "src-enroll-unknown", binding.scope.course_id),
            PublicIds(101, 599, 699),
        )
        with pytest.raises(CourseError) as raised:
            source.fetch_bundle(missing)
        assert raised.value.code == "NOT_FOUND"

    def test_missing_final_and_unsupported_kinds_are_not_filled_with_fake_items(self):
        missing_final = deepcopy(BUNDLE_DOC)
        missing_final["placements"] = missing_final["placements"][:-1]
        with pytest.raises(CourseError) as raised:
            FixtureCourseProvider(document=missing_final, settings=settings())
        assert raised.value.code == "UPSTREAM_CONTRACT_MISMATCH"
        scorm = deepcopy(BUNDLE_DOC)
        scorm["placements"][2]["kind"] = "scorm"
        with pytest.raises(CourseError) as raised:
            FixtureCourseProvider(document=scorm, settings=settings())
        assert raised.value.code == "UPSTREAM_CONTRACT_MISMATCH"
        quiz = deepcopy(BUNDLE_DOC)
        quiz["placements"][2]["detail"]["itemType"] = "quiz"
        with pytest.raises(CourseError) as raised:
            FixtureCourseProvider(document=quiz, settings=settings())
        assert raised.value.code == "UPSTREAM_CONTRACT_MISMATCH"

    def test_failed_list_is_not_an_empty_success(self):
        source = provider(list_error="ARC_PROGRESS_UNAVAILABLE")
        learner = source.resolve_learner(make_auth(real_principal()))
        with pytest.raises(CourseError) as raised:
            result = source.list_assignments(learner)
            assert result == ()
        assert raised.value.code == "ARC_PROGRESS_UNAVAILABLE"
        source = provider(list_script=(CourseError("UPSTREAM_CONTRACT_MISMATCH"), None))
        with pytest.raises(CourseError) as raised:
            source.list_assignments(learner)
        assert raised.value.code == "UPSTREAM_CONTRACT_MISMATCH"
        assigned = source.list_assignments(learner)
        assert len(assigned) == 2

    def test_public_ids_come_from_the_correspondence_table_only(self):
        missing = deepcopy(BUNDLE_DOC)
        missing["id_map"]["course"] = {"source_id": "101"}
        for row in missing["assignments"]:
            row["scope"][4] = "101"
        with pytest.raises(CourseError) as raised:
            FixtureCourseProvider(document=missing, settings=settings())
        assert raised.value.code == "CONTRACT_PENDING"
        coerced = deepcopy(BUNDLE_DOC)
        coerced["id_map"]["course"]["public_id"] = "101"
        with pytest.raises(CourseError) as raised:
            FixtureCourseProvider(document=coerced, settings=settings())
        assert raised.value.code == "UPSTREAM_CONTRACT_MISMATCH"
        mismatch_public = deepcopy(BUNDLE_DOC)
        mismatch_public["id_map"]["enrollments"][0]["public_id"] = 777
        with pytest.raises(CourseError) as raised:
            FixtureCourseProvider(document=mismatch_public, settings=settings())
        assert raised.value.code == "UPSTREAM_CONTRACT_MISMATCH"


class TestV04FixtureBindAndExecution:
    def test_explicit_bind_and_caller_mutation_does_not_change_owned_catalog(self):
        document = deepcopy(BUNDLE_DOC)
        source = FixtureCourseProvider(document=document, settings=settings())
        document["placements"][0]["content_version"] = "video-v2"
        learner = source.resolve_learner(make_auth(real_principal()))
        bundle = source.fetch_bundle(source.list_assignments(learner)[0])
        assert bundle.definition_hash == GOLDEN_501
        assert bundle.placements[0].content_version == "video-v1"

    def test_detail_null_is_preserved_and_execution_is_absent(self):
        document = deepcopy(BUNDLE_DOC)
        document["placements"][2]["detail"]["detail"] = None
        document["placements"][2]["execution"] = None
        document["placements"][2]["execution_status"] = "absent"
        source = FixtureCourseProvider(
            document=document, settings=settings(), mapping_document=MAPPING_DOC,
        )
        bundle = source.fetch_bundle(source.list_assignments(source.resolve_learner(make_auth(real_principal())))[0])
        training = bundle.placements[2]
        assert parse_json(training.detail_json)["detail"] is None
        assert training.execution_status == "absent"
        assert training.execution_json is None
        with pytest.raises(CourseError) as raised:
            map_execution(
                training, mapping_version=FIXTURE_MAPPING_VERSION, mappings=source.mappings,
            )
        assert raised.value.code == "EXECUTION_DEFINITION_MISSING"

    def test_unsupported_definition_is_not_started_via_map_execution(self):
        document = deepcopy(BUNDLE_DOC)
        document["placements"][2]["execution"] = None
        document["placements"][2]["execution_status"] = "unsupported"
        source = FixtureCourseProvider(
            document=document, settings=settings(), mapping_document=MAPPING_DOC,
        )
        bundle = source.fetch_bundle(source.list_assignments(source.resolve_learner(make_auth(real_principal())))[0])
        training = bundle.placements[2]
        assert training.execution_status == "unsupported"
        with pytest.raises(CourseError) as raised:
            map_execution(
                training, mapping_version=FIXTURE_MAPPING_VERSION, mappings=source.mappings,
            )
        assert raised.value.code == "EXECUTION_DEFINITION_UNSUPPORTED"

    def test_five_key_execution_is_not_ready(self):
        five = {
            key: MAPPING_DOC["mappings"]["src-place-1003"]["execution"][key]
            for key in EXECUTION_KEYS[:5]
        }
        document = deepcopy(BUNDLE_DOC)
        document["placements"][2]["execution"] = five
        document["placements"][2]["execution_status"] = "ready"
        source = FixtureCourseProvider(document=document, settings=settings())
        bundle = source.fetch_bundle(source.list_assignments(source.resolve_learner(make_auth(real_principal())))[0])
        assert bundle.placements[2].execution_status == "unsupported"
        assert bundle.placements[2].execution_json is None


class TestV13FrozenSnapshot:
    def test_url_change_on_a_new_catalog_keeps_the_same_hash_and_old_snapshot(self):
        source = provider()
        learner = source.resolve_learner(make_auth(real_principal()))
        binding = source.list_assignments(learner)[0]
        frozen = source.fetch_bundle(binding)
        frozen_hash = frozen.definition_hash
        frozen_identity = definition_identity(frozen)
        updated = deepcopy(BUNDLE_DOC)
        updated["placements"][0]["detail"]["detail"]["url"] = "https://fixture.invalid/video/rotated.mp4?sig=new"
        updated["placements"][0]["detail"]["title"] = "Rotated"
        later = FixtureCourseProvider(document=updated, settings=settings())
        refreshed = later.fetch_bundle(later.list_assignments(later.resolve_learner(make_auth(real_principal())))[0])
        assert frozen.definition_hash == GOLDEN_501
        assert refreshed.definition_hash == frozen_hash
        assert definition_identity(refreshed) == frozen_identity
        assert parse_json(frozen.placements[0].detail_json)["detail"]["url"] != parse_json(
            refreshed.placements[0].detail_json
        )["detail"]["url"]
        rewritten = deepcopy(BUNDLE_DOC)
        rewritten["placements"][0]["content_version"] = "video-v2"
        changed = FixtureCourseProvider(document=rewritten, settings=settings())
        new_snapshot = changed.fetch_bundle(changed.list_assignments(changed.resolve_learner(make_auth(real_principal())))[0])
        assert new_snapshot.definition_hash == vector("content_version_change")["digest"]
        assert frozen.definition_hash == GOLDEN_501
        assert frozen.placements[0].content_version == "video-v1"


class TestMapExecution:
    def test_ready_rows_consume_execution_mapping_exactly(self):
        source = provider()
        bundle = source.fetch_bundle(source.list_assignments(source.resolve_learner(make_auth(real_principal())))[0])
        registry = fixture_mapping_registry(MAPPING_DOC)
        for source_id, row in MAPPING_DOC["mappings"].items():
            placement = next(item for item in bundle.placements if item.source_id == source_id)
            body = map_execution(placement, mapping_version=FIXTURE_MAPPING_VERSION, mappings=registry)
            parsed = parse_json(body)
            assert parsed == row["execution"]
            assert tuple(sorted(parsed)) == tuple(sorted(EXECUTION_KEYS))
            assert "program_id" not in parsed
            assert row["program_id"] in {item[0] for item in PROGRAMS}
            assert row["program_id"] not in {placement.source_id, "src-course-101", 101, 1003, 1004, 1005}
            assert row["target"] == parsed["condition"]["target"]
            assert parsed["catalog_version"] == FIXTURE_CATALOG_VERSION

    def test_video_document_have_no_execution_and_unknown_mapping_is_unsupported(self):
        source = provider()
        bundle = source.fetch_bundle(source.list_assignments(source.resolve_learner(make_auth(real_principal())))[0])
        registry = source.mappings
        for item in bundle.placements[:2]:
            assert item.execution_status == "absent"
            assert item.execution_json is None
            with pytest.raises(CourseError) as raised:
                map_execution(item, mapping_version=FIXTURE_MAPPING_VERSION, mappings=registry)
            assert raised.value.code == "EXECUTION_DEFINITION_MISSING"
        training = bundle.placements[2]
        with pytest.raises(CourseError) as raised:
            map_execution(training, mapping_version="vcc-unknown-mapping-v0", mappings=registry)
        assert raised.value.code == "EXECUTION_DEFINITION_UNSUPPORTED"
        five = {
            key: MAPPING_DOC["mappings"]["src-place-1003"]["execution"][key]
            for key in EXECUTION_KEYS[:5]
        }

        def transform(_placement):
            return owned_json_bytes(five)

        with pytest.raises(CourseError) as raised:
            map_execution(
                training, mapping_version=FIXTURE_MAPPING_VERSION,
                mappings=MappingRegistry({FIXTURE_MAPPING_VERSION: transform}),
            )
        assert raised.value.code == "EXECUTION_DEFINITION_UNSUPPORTED"


class TestLimitsDuplicatesAndWire:
    def test_settings_limits_reject_without_truncating_to_success(self):
        tiny_items = replace(settings(), max_course_items=4)
        with pytest.raises(CourseError) as raised:
            FixtureCourseProvider(document=BUNDLE_DOC, settings=tiny_items)
        assert raised.value.code == "UPSTREAM_CONTRACT_MISMATCH"
        tiny_assignments = replace(settings(), max_assignments=1)
        source = FixtureCourseProvider(document=BUNDLE_DOC, settings=tiny_assignments)
        learner = source.resolve_learner(make_auth(real_principal()))
        with pytest.raises(CourseError) as raised:
            source.list_assignments(learner)
        assert raised.value.code == "UPSTREAM_CONTRACT_MISMATCH"
        source = provider()
        bundle = source.fetch_bundle(source.list_assignments(source.resolve_learner(make_auth(real_principal())))[0])
        with pytest.raises(CourseError) as raised:
            validate_bundle(bundle, replace(settings(), max_bundle_bytes=100))
        assert raised.value.code == "UPSTREAM_CONTRACT_MISMATCH"
        assert len(validate_assignments((source.list_assignments(learner)[0],), tiny_assignments)) == 1

    def test_duplicate_ids_are_rejected(self):
        duplicate = deepcopy(BUNDLE_DOC)
        duplicate["placements"][1]["source_id"] = "src-place-1001"
        duplicate["placements"][1]["public_link_id"] = 1001
        duplicate["placements"][1]["public_item_id"] = 201
        with pytest.raises(CourseError) as raised:
            FixtureCourseProvider(document=duplicate, settings=settings())
        assert raised.value.code == "UPSTREAM_CONTRACT_MISMATCH"

    def test_source_to_internal_to_wire_item_types(self):
        source = provider()
        bundle = source.fetch_bundle(source.list_assignments(source.resolve_learner(make_auth(real_principal())))[0])
        for item in bundle.placements:
            wire = item_type_wire(item.kind)
            assert wire == dict(ITEM_TYPE_WIRE[item.kind])
            assert wire == BUNDLE_DOC["item_type_wire"][item.kind]
            detail = parse_json(item.detail_json)
            assert detail["itemType"] == wire["detail_item_type"]
        assert item_type_wire("document")["summary_item_type"] == "pdf"
        assert item_type_wire("video")["course_item_type"] == "content"

    def test_provider_does_not_generate_scores(self):
        source = provider()
        bundle = source.fetch_bundle(source.list_assignments(source.resolve_learner(make_auth(real_principal())))[0])
        assert "score" not in parse_json(bundle.course_json)
        assert "cpr_score" not in parse_json(bundle.source_progress_json)
        for item in bundle.placements:
            detail = parse_json(item.detail_json)
            assert "score" not in detail
            if item.execution_json is not None:
                execution = parse_json(item.execution_json)
                assert "score" not in execution
                assert set(execution) == set(EXECUTION_KEYS)


class TestControllableHooks:
    def test_delayed_failing_and_out_of_order_hooks(self):
        started = []
        finished = []
        release_first = threading.Event()
        first_started = threading.Event()

        def list_hook(index, _learner):
            started.append(index)
            if index == 1:
                first_started.set()
                assert release_first.wait(timeout=2)
            finished.append(index)

        source = provider(list_hook=list_hook)
        learner = source.resolve_learner(make_auth(real_principal()))
        holder = []

        def first_call():
            holder.append(source.list_assignments(learner))

        worker = threading.Thread(target=first_call)
        worker.start()
        assert first_started.wait(timeout=2)
        second = source.list_assignments(learner)
        release_first.set()
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert finished == [2, 1]
        assert len(second) == 2
        assert len(holder[0]) == 2
        fetch_order = []

        def fetch_hook(index, binding):
            fetch_order.append((index, binding.public_ids.enrollment_id))

        fetching = provider(fetch_hook=fetch_hook, fetch_script=(CourseError("ARC_PROGRESS_UNAVAILABLE"), None))
        learner = fetching.resolve_learner(make_auth(real_principal()))
        first, second_binding = fetching.list_assignments(learner)
        with pytest.raises(CourseError) as raised:
            fetching.fetch_bundle(second_binding)
        assert raised.value.code == "ARC_PROGRESS_UNAVAILABLE"
        bundle = fetching.fetch_bundle(first)
        assert bundle.public_ids.enrollment_id == 501
        assert fetch_order == [(1, 502), (2, 501)]
        assert fetching.fetch_call_count == 2
        assert fetching.list_call_count == 1

"""Independent VCC contract oracle. Does not execute DB, HTTP, or calculators."""

from copy import deepcopy
from dataclasses import fields, replace
from pathlib import Path
import json
import subprocess
import sys

import pytest

from mock_journey.catalog import PROGRAMS, TARGETS
from mock_journey.course_contracts import (
    APP_ROUTES, ATTEMPT_VIEW_FIELDS, CALCULATION_VIEW_FIELDS, CONTENT_START_VIEW_FIELDS,
    CONTRACT_VERSION, COURSE_DETAIL_FIELDS, COURSE_ITEM_FIELDS, COURSE_LIST_ROW_FIELDS,
    DIGEST_EXCLUDED_WIRE_FIELDS, ENROLLMENT_FIELDS, EXECUTION_KEYS, FIXTURE_CATALOG_VERSION,
    FIXTURE_MAPPING_VERSION, ITEM_DETAIL_OUTER_FIELDS, ITEM_TYPE_WIRE, PAGE_SIZE_MAX,
    POLICY_VERSION, PROGRESS_RECEIPT_FIELDS, PUBLIC_SYMBOLS, SESSION_VIEW_FIELDS,
    START_REQUEST_FIELDS, AssignmentBinding, AttemptTemplate, ContentReport, CourseBinding,
    CourseBundle, CourseScope, CourseView, GateView, InventoryTicket,
    InventoryView, LearnerContext, MappingRegistry, Placement, PublicIds, RefreshResult,
    RefreshTicket, StartCommand, StartReceipt, StoredProgressReceipt, WritePlan,
    definition_digest, definition_identity, item_type_wire, learner_identity, owned_json_bytes,
    sealed_bundle, scope_identity, validate_execution_definition,
)
from mock_journey.course_errors import COURSE_ERROR_CODES, CourseError, course_error_table
from mock_journey.course_settings import CourseSettings, fixture_course_settings
from mock_journey.errors import JourneyError
from mock_journey.typed import digest, parse_json


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "vcc_contract" / "v1"
PYTHON = sys.executable


def load(name):
    return parse_json((FIXTURES / name).read_bytes())


BUNDLE_DOC = load("course_bundle.json")
VECTORS = load("typed_id_vectors.json")
MAPPING_DOC = load("execution_mapping.json")
WIRE = load("wire_cases.json")


def t2_identity(scope, mapping_version, placements):
    """ARCHITECTURE T2 projection copied here so tests do not treat implementation output as truth."""
    return {
        "contract_version": "vcc-internal-v1",
        "mapping_version": mapping_version,
        "scope": list(scope),
        "placements": [{
            "source_id": item["source_id"],
            "position": item["position"],
            "kind": item["kind"],
            "content_identity": deepcopy(item["content_identity"]),
            "content_version": item["content_version"],
            "duration_ms": item["duration_ms"],
            "execution_status": item["execution_status"],
            "execution": deepcopy(item["execution"]),
        } for item in placements],
    }


def learner_from(doc):
    return LearnerContext(**doc)


def placement_from(doc, *, detail=None, content_identity=None, execution=None, **overrides):
    payload = {
        "source_id": doc["source_id"],
        "public_link_id": doc["public_link_id"],
        "public_item_id": doc["public_item_id"],
        "position": doc["position"],
        "kind": doc["kind"],
        "content_version": doc["content_version"],
        "content_identity_json": content_identity if content_identity is not None else deepcopy(doc["content_identity"]),
        "detail_json": detail if detail is not None else deepcopy(doc["detail"]),
        "execution_json": execution if execution is not None else deepcopy(doc["execution"]),
        "execution_status": doc["execution_status"],
        "duration_ms": doc["duration_ms"],
    }
    payload.update(overrides)
    return Placement(**payload)


def bundle_from(doc, assignment, *, placements=None, course_json=None, progress_json=None):
    learner = learner_from(doc["learners"]["real"])
    scope = CourseScope(learner, assignment["scope"][3], assignment["scope"][4])
    public = PublicIds(assignment["course_public_id"], assignment["enrollment_public_id"], assignment["progress_id"])
    items = placements if placements is not None else [placement_from(item) for item in doc["placements"]]
    bundle = CourseBundle(
        scope=scope, public_ids=public, source_revision=assignment["source_revision"],
        mapping_version=doc["mapping_version"], definition_hash=assignment["definition_hash"],
        placements=items,
        course_json=course_json if course_json is not None else deepcopy(doc["course_json"]),
        source_progress_json=progress_json if progress_json is not None else deepcopy(doc["source_progress_json"]),
    )
    return bundle


def assignment_501():
    return BUNDLE_DOC["assignments"][0]


def baseline_bundle():
    return bundle_from(BUNDLE_DOC, assignment_501())


class TestContractVersionAndSymbols:
    def test_versions_and_public_symbols_are_stable(self):
        assert CONTRACT_VERSION == "vcc-internal-v1"
        assert POLICY_VERSION == "vcc-policy-v1"
        assert FIXTURE_MAPPING_VERSION == "vcc-fixture-mapping-v1"
        assert FIXTURE_CATALOG_VERSION == "vcc-fixture-catalog-v1"
        for name in PUBLIC_SYMBOLS:
            assert name in dir(__import__("mock_journey.course_contracts", fromlist=[name]))
        for doc in (BUNDLE_DOC, VECTORS, MAPPING_DOC, WIRE):
            assert doc["contract_version"] == CONTRACT_VERSION
            assert doc["synthetic"] is True

    def test_fixture_files_are_typed_json_without_duplicate_keys(self):
        for name in ("course_bundle.json", "typed_id_vectors.json", "execution_mapping.json", "wire_cases.json"):
            raw = (FIXTURES / name).read_bytes()
            assert parse_json(raw) == json.loads(raw)


class TestImportHasNoSideEffects:
    def test_isolated_import_does_not_read_environment_or_open_network(self):
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
from mock_journey.course_errors import CourseError
from mock_journey.course_settings import fixture_course_settings
from mock_journey.course_contracts import CONTRACT_VERSION, APP_ROUTES
assert CONTRACT_VERSION == 'vcc-internal-v1'
assert len(APP_ROUTES) == 16
fixture_course_settings()
CourseError('CONTRACT_PENDING')
assert not {'boto3', 'botocore', 'sentry_sdk'} & sys.modules.keys()
""".replace("ROOT", json.dumps(str(ROOT)))
        completed = subprocess.run([PYTHON, "-I", "-S", "-B", "-c", script], env={},
                                   capture_output=True, text=True, timeout=10)
        assert completed.returncode == 0, completed.stderr


class TestV01ExactTypesAndOwnership:
    def test_source_id_int_and_str_hashes_differ(self):
        case = next(item for item in VECTORS["cases"] if item["id"] == "source_id_int_1_vs_str_1")
        assert case["left"]["digest"] != case["right"]["digest"]
        tagged = next(item for item in VECTORS["cases"] if item["id"] == "tagged_id_vectors")["values"]
        assert tagged["int_1"] != tagged["str_1"]
        assert tagged["list_int_1"] != tagged["list_str_1"]
        assert tagged["scope_int_learner"] != tagged["scope_str_learner"]
        assert tagged["learner_key_int"] != tagged["learner_key_str"]
        assert tagged["int_1"] == digest(1)
        assert tagged["str_1"] == digest("1")

    def test_scope_identity_keeps_exact_source_types(self):
        real = learner_from(BUNDLE_DOC["learners"]["real"])
        dummy = learner_from(BUNDLE_DOC["learners"]["dummy"])
        assert type(real.learner_id) is int and real.learner_id == 1
        assert type(dummy.learner_id) is str and dummy.learner_id == "dummy-1"
        scope = CourseScope(real, "src-enroll-501", "src-course-101")
        assert scope_identity(scope) == ["fixture", "tenant-vcc-fixture", 1, "src-enroll-501", "src-course-101"]
        assert learner_identity(real) == ["fixture", "tenant-vcc-fixture", 1]
        assert digest(scope_identity(scope)) == BUNDLE_DOC["assignments"][0]["scope_key"]
        assert digest(learner_identity(real)) == BUNDLE_DOC["learner_key"]

    def test_bool_null_and_numeric_strings_are_rejected(self):
        with pytest.raises(CourseError) as raised:
            PublicIds(True, 501, 601)
        assert raised.value.code == "INVALID_REQUEST"
        for invalid in (True, False, 0, -1, 1.0, "101", None, 9007199254740992):
            with pytest.raises(CourseError):
                PublicIds(invalid, 501, 601)
        with pytest.raises(CourseError):
            LearnerContext("fixture", "tenant", True, "principal", False)
        with pytest.raises(CourseError):
            StartCommand("10000000-0000-4000-8000-000000000001", 501, "101", 1001, "a" * 64)

    def test_mutating_caller_containers_does_not_change_dto_or_hash(self):
        identity = {"source_item_id": "src-item-video", "training_program_id": None, "asset_ids": ["src-asset-video"]}
        detail = deepcopy(BUNDLE_DOC["placements"][0]["detail"])
        items = [placement_from(item, content_identity=identity if item["kind"] == "video" else None) for item in BUNDLE_DOC["placements"]]
        original_list = list(items)
        bundle = bundle_from(BUNDLE_DOC, assignment_501(), placements=original_list)
        first = definition_digest(bundle)
        identity["asset_ids"].append("mutated")
        identity["source_item_id"] = "changed"
        detail["title"] = "changed"
        original_list.pop()
        items[0]  # still the frozen placement
        assert parse_json(bundle.placements[0].content_identity_json)["asset_ids"] == ["src-asset-video"]
        assert len(bundle.placements) == 5
        assert definition_digest(bundle) == first

    def test_protocols_and_dtos_share_the_same_public_types(self):
        from mock_journey import course_contracts as contracts
        expected = {
            "CourseProvider": {"resolve_learner", "list_assignments", "fetch_bundle"},
            "CourseRepository": {
                "begin_inventory", "apply_inventory", "ensure_epoch", "begin_refresh", "apply_refresh",
                "load_view", "find_created", "load_start_view", "start", "report",
            },
            "CourseService": {
                "list_courses", "get_course", "get_item", "start_content", "start_attempt",
                "report_content", "refresh_for_session",
            },
            "CoursePolicy": {"can_start", "evaluate_content", "aggregate_progress", "classify_submission"},
            "CourseCalculationBridge": {"prepare"},
            "CourseCompletionPlan": {"build"},
            "CourseRecovery": {"inspect"},
            "ArcGateway": {"submit"},
        }
        for name, methods in expected.items():
            proto = getattr(contracts, name)
            assert proto._is_protocol is True
            assert methods <= {item for item in dir(proto) if not item.startswith("_")}


class TestV02DefinitionHashOracle:
    def test_baseline_hash_matches_independent_t2_projection(self):
        assignment = assignment_501()
        independent = t2_identity(assignment["scope"], BUNDLE_DOC["mapping_version"], BUNDLE_DOC["placements"])
        stored = next(item for item in VECTORS["cases"] if item["id"] == "baseline")
        assert independent == stored["projection"]
        assert digest(independent) == VECTORS["baseline_digest"]
        assert digest(independent) == assignment["definition_hash"]
        bundle = baseline_bundle()
        assert definition_identity(bundle) == stored["projection"]
        assert definition_digest(bundle) == VECTORS["baseline_digest"]
        assert sealed_bundle(replace(bundle, definition_hash="0" * 64)).definition_hash == VECTORS["baseline_digest"]

    def test_display_and_session_fields_do_not_enter_the_hash(self):
        bundle = baseline_bundle()
        changed_detail = deepcopy(BUNDLE_DOC["placements"][0]["detail"])
        changed_detail["detail"]["url"] = "https://fixture.invalid/video/other.mp4?sig=rotated"
        changed_detail["title"] = "Renamed"
        placements = [
            placement_from(item, detail=changed_detail) if item["kind"] == "video" else placement_from(item)
            for item in BUNDLE_DOC["placements"]
        ]
        course_json = deepcopy(BUNDLE_DOC["course_json"])
        course_json["courseName"] = "Renamed Course"
        progress = {"synthetic": True, "progress": {"ignored": True}, "looked_up_at": "2026-09-18T12:00:00Z"}
        mutated = bundle_from(
            BUNDLE_DOC, assignment_501(), placements=placements, course_json=course_json, progress_json=progress,
        )
        mutated = replace(mutated, source_revision="rev-bundle-9")
        assert definition_digest(mutated) == VECTORS["baseline_digest"]
        assert "public_ids" not in definition_identity(mutated)
        assert "detail" not in definition_identity(mutated)["placements"][0]

    def test_content_order_execution_and_id_type_changes_alter_hash(self):
        baseline = VECTORS["baseline_digest"]
        for case_id in VECTORS["different_from_baseline"]:
            if case_id == "source_id_int_1_vs_str_1":
                case = next(item for item in VECTORS["cases"] if item["id"] == case_id)
                assert case["left"]["digest"] != case["right"]["digest"]
                continue
            case = next(item for item in VECTORS["cases"] if item["id"] == case_id)
            assert case["digest"] != baseline
            assert digest(case["projection"]) == case["digest"]

    def test_implementation_hash_follows_stored_change_vectors(self):
        bundle = baseline_bundle()
        versioned = replace(bundle.placements[0], content_version="video-v2")
        items = (versioned,) + bundle.placements[1:]
        changed = replace(bundle, placements=items)
        expected = next(item for item in VECTORS["cases"] if item["id"] == "content_version_change")
        assert definition_digest(changed) == expected["digest"]

    def test_tuple_projection_is_converted_to_list_before_digest(self):
        case = next(item for item in VECTORS["cases"] if item["id"] == "tuple_converted_to_list")
        as_list = ["fixture", "tenant-vcc-fixture", 1, "src-enroll-501", "src-course-101"]
        assert digest(as_list) == case["list_digest"]
        with pytest.raises((TypeError, ValueError)):
            digest(("fixture", "tenant-vcc-fixture", 1, "src-enroll-501", "src-course-101"))


class TestD7FixtureAndExecutionMapping:
    def test_two_enrollments_and_repeated_training_are_distinct(self):
        first, second = BUNDLE_DOC["assignments"]
        assert first["enrollment_public_id"] == 501
        assert second["enrollment_public_id"] == 502
        assert first["course_public_id"] == second["course_public_id"] == 101
        assert first["definition_hash"] != second["definition_hash"]
        kinds = [item["kind"] for item in BUNDLE_DOC["placements"]]
        assert kinds == ["video", "document", "training", "training", "assessment"]
        training = [item for item in BUNDLE_DOC["placements"] if item["kind"] == "training"]
        assert training[0]["content_identity"]["training_program_id"] == training[1]["content_identity"]["training_program_id"]
        assert training[0]["source_id"] != training[1]["source_id"]
        expect = BUNDLE_DOC["completion_expectations"]["training_isolation"]
        assert expect["complete_1003_does_not_complete_1004"] is True
        assert expect["complete_501_does_not_complete_502"] is True

    def test_execution_mapping_is_complete_seven_key_only_and_matches_existing_programs(self):
        program_ids = {row[0] for row in PROGRAMS}
        for source_id, row in MAPPING_DOC["mappings"].items():
            assert row["program_id"] in program_ids
            assert row["target"] in TARGETS
            assert row["target"] == row["execution"]["condition"]["target"]
            assert tuple(sorted(row["execution"])) == tuple(sorted(EXECUTION_KEYS))
            validate_execution_definition(row["execution"])
            assert (ROOT / row["binary_fixture"]).is_file()
            assert (ROOT / row["result_oracle"]).is_file()
            assert row["execution"]["catalog_version"] == FIXTURE_CATALOG_VERSION
            assert row["source_placement_id"] == source_id
        five = {key: MAPPING_DOC["mappings"]["src-place-1003"]["execution"][key] for key in EXECUTION_KEYS[:5]}
        with pytest.raises(CourseError):
            validate_execution_definition(five)
        assert MAPPING_DOC["five_key_rejected"] is True
        assert set(MAPPING_DOC["no_execution"]) == {"src-place-1001", "src-place-1002"}

    def test_item_type_wire_and_video_document_rules_are_the_shared_shape(self):
        assert dict(ITEM_TYPE_WIRE["video"]) == BUNDLE_DOC["item_type_wire"]["video"]
        assert item_type_wire("document")["summary_item_type"] == "pdf"
        video = BUNDLE_DOC["completion_expectations"]["video_1001"]
        assert video["duration_ms"] == 20000
        assert video["complete_intervals"] == [[0, 10000], [10000, 20000]]
        assert video["button_not_required"] is True
        document = BUNDLE_DOC["completion_expectations"]["document_1002"]
        assert document["requires"] == [
            "document_displayed", "document_confirmed referencing that display report",
        ]
        assert "pending_evidence" in document["confirm_first"]

    def test_mapping_registry_does_not_invent_transforms(self):
        registry = MappingRegistry({})
        assert registry.resolve(FIXTURE_MAPPING_VERSION) is None
        called = []

        def transform(placement):
            called.append(placement)
            return owned_json_bytes(MAPPING_DOC["mappings"]["src-place-1003"]["execution"])

        registry = MappingRegistry({FIXTURE_MAPPING_VERSION: transform})
        placement = placement_from(BUNDLE_DOC["placements"][2])
        assert registry.resolve(FIXTURE_MAPPING_VERSION) is transform
        assert parse_json(transform(placement))["goal"]["required"] == 60


class TestV05WireSchema:
    def test_every_app_route_has_a_wire_case(self):
        wire_ids = [route["id"] for route in WIRE["routes"]]
        spec_ids = [route.route_id for route in APP_ROUTES]
        assert wire_ids == spec_ids
        assert len(spec_ids) == 16
        for spec, route in zip(APP_ROUTES, WIRE["routes"]):
            assert spec.method == route["method"]
            assert spec.path == route["path"]
            assert spec.path.endswith("/")
            assert tuple(spec.query_allowed) == tuple(route["query"])
            assert spec.auth_required == route["auth"]
            assert route["success"]
            assert route["errors"]

    def test_error_envelope_and_fixed_messages_match_course_error(self):
        table = course_error_table()
        for code, spec in WIRE["fixed_errors"].items():
            error = CourseError(code)
            assert error.status == spec["http"]
            assert error.message == spec["message"]
            assert error.status == table[code]["status"]
            assert WIRE["envelope"]["error"]["error"]["details"] is None
        reused = JourneyError("NOT_FOUND")
        course = CourseError("NOT_FOUND")
        assert (course.status, course.message) == (reused.status, reused.message)
        pending = CourseError("CONTRACT_PENDING")
        assert pending.status == 503
        assert pending.message == "The integration contract is not available."
        assert "CONTRACT_PENDING" in COURSE_ERROR_CODES

    def test_success_payloads_preserve_c3_c4_fields(self):
        list_case = next(case for case in WIRE["routes"] if case["id"] == "course_list")["success"][0]
        row = list_case["data"]["results"][0]
        assert tuple(row) == COURSE_LIST_ROW_FIELDS
        assert tuple(row["summary"][0]) == ("id", "itemType", "title", "displayOrder")
        detail = next(case for case in WIRE["routes"] if case["id"] == "course_detail")["success"][0]["data"]
        assert tuple(detail) == COURSE_DETAIL_FIELDS
        assert tuple(detail["courseItems"][0]) == COURSE_ITEM_FIELDS
        assert tuple(detail["enrollment"]) == ENROLLMENT_FIELDS
        item = next(case for case in WIRE["routes"] if case["id"] == "item_detail")["success"][0]["data"]
        assert tuple(item) == ITEM_DETAIL_OUTER_FIELDS
        start = next(case for case in WIRE["routes"] if case["id"] == "learning_start")["success"][0]
        assert tuple(start["request"]) == START_REQUEST_FIELDS
        assert tuple(start["data"]) == CONTENT_START_VIEW_FIELDS
        receipt = next(case for case in WIRE["routes"] if case["id"] == "content_report")["success"][0]["data"]
        assert tuple(receipt) == PROGRESS_RECEIPT_FIELDS
        attempt = next(case for case in WIRE["routes"] if case["id"] == "attempt_get")["success"][0]["data"]
        assert tuple(attempt) == ATTEMPT_VIEW_FIELDS
        calc = next(case for case in WIRE["routes"] if case["id"] == "calculation_get")["success"][2]["data"]
        assert tuple(calc) == CALCULATION_VIEW_FIELDS
        session = next(case for case in WIRE["routes"] if case["id"] == "session")["success"][0]
        assert tuple(session["data"]) == SESSION_VIEW_FIELDS
        assert "accessToken" in session["forbidden_fields"]

    def test_null_legacy_waiting_excluded_and_undetermined_expectations_are_fixed(self):
        historical = next(
            case for case in next(route for route in WIRE["routes"] if route["id"] == "content_report")["success"]
            if case["id"] == "historical_only"
        )
        assert historical["data"]["application"] == "historical_only"
        assert historical["data"]["isCompleted"] is None
        assert historical["data"]["isPassed"] is None
        assert historical["data"]["courseStatus"] is None
        legacy = next(
            case for case in next(route for route in WIRE["routes"] if route["id"] == "attempt_get")["success"]
            if case["id"] == "legacy_attempt_null_course_fields"
        )
        for field in WIRE["legacy_attempt"]["null_fields"]:
            assert legacy["data"][field] is None
        assert legacy["data"]["condition"]["target"] == "adult"
        submit = WIRE["submit_arc"]
        assert submit["disabled"] == {
            "status": "disabled", "ok": False, "error": "arc_contract_pending", "exclusionReasons": [],
        }
        assert submit["excluded_dummy"]["ok"] is False
        assert submit["excluded_dummy"]["error"] is None
        assert submit["no_ok_true"] is True
        assert WIRE["undetermined"]["no_fake_pass"] is True
        policy = next(
            case for case in next(route for route in WIRE["routes"] if route["id"] == "calculation_get")["success"]
            if case["id"] == "pending_policy_is_200"
        )
        assert policy["http"] == 200
        assert policy["data"]["evaluation"]["goal"]["status"] == "pending_policy"
        assert set(WIRE["digest_exclusions"]) == DIGEST_EXCLUDED_WIRE_FIELDS
        assert WIRE["cancel_reason_map"] == {"user_cancelled": "user_stopped", "connection_lost": "manikin_disconnected"}
        assert PAGE_SIZE_MAX == 1000

    def test_receipts_reject_secrets_and_start_kinds_are_exclusive(self):
        safe = {
            "startId": "40000000-0000-4000-8000-000000000001",
            "courseId": 101, "enrollmentId": 501, "courseItemLinkId": 1001,
            "contentVersion": "video-v1", "definitionHash": VECTORS["baseline_digest"],
        }
        receipt = StartReceipt(
            created=True, start_id="40000000-0000-4000-8000-000000000001", attempt_id=None,
            scope_key=BUNDLE_DOC["assignments"][0]["scope_key"],
            placement_key=digest(["scope", "src-place-1001"]),
            definition_hash=VECTORS["baseline_digest"], content_version="video-v1",
            bound_session_id="60000000-0000-4000-8000-000000000001",
            epoch="80000000-0000-4000-8000-000000000001", response_json=safe,
        )
        assert "resumeCredential" not in parse_json(receipt.response_json)
        with pytest.raises(CourseError):
            StartReceipt(
                created=True, start_id="40000000-0000-4000-8000-000000000001", attempt_id=None,
                scope_key=receipt.scope_key, placement_key=receipt.placement_key,
                definition_hash=receipt.definition_hash, content_version="video-v1",
                bound_session_id=receipt.bound_session_id, epoch=receipt.epoch,
                response_json={**safe, "resumeCredential": "secret"},
            )
        with pytest.raises(CourseError):
            StartReceipt(
                created=True, start_id="40000000-0000-4000-8000-000000000001",
                attempt_id="50000000-0000-4000-8000-000000000001",
                scope_key=receipt.scope_key, placement_key=receipt.placement_key,
                definition_hash=receipt.definition_hash, content_version="video-v1",
                bound_session_id=receipt.bound_session_id, epoch=receipt.epoch, response_json=safe,
            )


class TestV22CourseSettingsSchema:
    def test_fixture_settings_are_explicit_t7_values(self):
        from dataclasses import MISSING
        settings = fixture_course_settings()
        assert settings == CourseSettings(64, 100, 262144, 16384, 128, 512, 4096, 20, 4)
        for field in fields(CourseSettings):
            assert field.default is MISSING and field.default_factory is MISSING

    def test_missing_negative_bool_and_excess_conflict_retries_are_rejected(self):
        with pytest.raises(TypeError):
            CourseSettings(64, 100, 262144, 16384, 128, 512, 4096, 20)
        for invalid in (0, -1, True, False, 1.0, "4", None):
            with pytest.raises(ValueError) as raised:
                replace(fixture_course_settings(), max_conflict_retries=invalid)
            assert str(raised.value) == "Invalid course settings."
            assert raised.value.__cause__ is None
        with pytest.raises(ValueError):
            replace(fixture_course_settings(), max_conflict_retries=9)
        with pytest.raises(ValueError):
            replace(fixture_course_settings(), max_course_items=-1)

    def test_transaction_budgets_fit_the_configured_action_limit(self):
        settings = fixture_course_settings()
        budgets = BUNDLE_DOC["transaction_action_budgets"]
        numeric = {key: value for key, value in budgets.items() if type(value) is int}
        assert numeric
        assert max(numeric.values()) <= settings.max_transaction_actions
        assert settings.max_transaction_actions == 20
        assert BUNDLE_DOC["settings"] == {
            "max_course_items": 64, "max_assignments": 100, "max_bundle_bytes": 262144,
            "max_control_body_bytes": 16384, "max_intervals_per_report": 128,
            "max_merged_intervals_per_start": 512, "max_reports_per_start": 4096,
            "max_transaction_actions": 20, "max_conflict_retries": 4,
        }


class TestContentReportAndBindingShapes:
    def test_content_events_and_course_binding_are_exact(self):
        video = ContentReport(
            "30000000-0000-4000-8000-000000000001",
            "40000000-0000-4000-8000-000000000001",
            "video-v1", "video_segments", [[0, 10000], [10000, 20000]],
        )
        assert video.intervals_ms == ((0, 10000), (10000, 20000))
        with pytest.raises(CourseError):
            ContentReport(
                "30000000-0000-4000-8000-000000000001",
                "40000000-0000-4000-8000-000000000001",
                "video-v1", "video_segments", [],
            )
        displayed = ContentReport(
            "20000000-0000-4000-8000-000000000001",
            "40000000-0000-4000-8000-000000000001",
            "document-v1", "document_displayed",
        )
        confirmed = ContentReport(
            "30000000-0000-4000-8000-000000000001",
            "40000000-0000-4000-8000-000000000001",
            "document-v1", "document_confirmed",
            display_report_id="20000000-0000-4000-8000-000000000001",
        )
        assert displayed.display_report_id is None
        assert confirmed.event_type == "document_confirmed"
        binding = CourseBinding(
            BUNDLE_DOC["assignments"][0]["scope_key"],
            digest([BUNDLE_DOC["assignments"][0]["scope_key"], "src-place-1005"]),
            "final_assessment", VECTORS["baseline_digest"], "assessment-v1",
            "80000000-0000-4000-8000-000000000001", POLICY_VERSION,
        )
        assert binding.start_role == "final_assessment"
        ticket = InventoryTicket(BUNDLE_DOC["learner_key"], "80000000-0000-4000-8000-000000000001", 1)
        view = InventoryView(
            ticket.learner_key, ticket.epoch, 1, 1, "ready", None,
            (AssignmentBinding(baseline_bundle().scope, baseline_bundle().public_ids),),
        )
        gate = GateView(
            BUNDLE_DOC["assignments"][0]["scope_key"], ticket.epoch, "ready", None, 1, VECTORS["baseline_digest"],
        )
        refresh = RefreshResult(view, (gate,))
        assert refresh.inventory.state == "ready"
        RefreshTicket(gate.scope_key, ticket.epoch, 1, 1, 1)
        StoredProgressReceipt(gate.scope_key, confirmed.start_id, confirmed.report_id, WIRE["routes"][8]["success"][0]["data"])
        WritePlan({"actions": []}, {"status": "disabled"}, (("COURSE#x", "EPOCH#y#HEAD"),))
        AttemptTemplate({"attempt": "existing"}, binding)
        CourseView(gate.scope_key, baseline_bundle().public_ids, baseline_bundle(), {"items": []}, gate, view)
        StartCommand("10000000-0000-4000-8000-000000000001", 501, 101, 1001, VECTORS["baseline_digest"])
        with pytest.raises(CourseError):
            GateView(gate.scope_key, ticket.epoch, "ready", "arc_progress_unavailable", 1, None)
        with pytest.raises(CourseError):
            ContentReport(
                "30000000-0000-4000-8000-000000000001",
                "40000000-0000-4000-8000-000000000001",
                "document-v1", "quiz_answered",
            )

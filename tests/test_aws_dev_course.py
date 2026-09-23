"""Dummy AWS composition only; no test catalog files or real AWS connections."""

from dataclasses import asdict, replace
import json

import pytest

from mock_journey.auth import PRINCIPAL
from mock_journey.aws_runtime import build_runtime
from mock_journey.aws_settings import AwsSettings
from mock_journey.catalog import Catalog, PROGRAMS, TARGETS
from mock_journey.course_contracts import CourseScope, AssignmentBinding
from mock_journey.course_errors import CourseError
from mock_journey.course_settings import fixture_course_settings
from mock_journey.course_state import bundle_record
from mock_journey.course_submission import collect_exclusion_reasons
from mock_journey.dev_course import CATALOG_VERSION, MODE, DummyDevCourseProvider
from mock_journey.errors import JourneyError
from mock_journey.execution_definitions import execution_catalog
from mock_journey.handler import handle
from mock_journey.models import AuthContext
from mock_journey.typed import json_bytes, parse_json
from tests.test_aws_runtime import FakeSdk, configuration, context, environment


def course_configuration(role="api"):
    config = configuration(role)
    config["course"] = {"mode": MODE, "catalog_version": CATALOG_VERSION,
                        "settings": asdict(fixture_course_settings())}
    return config


def provider():
    return DummyDevCourseProvider(settings=fixture_course_settings(), execution=execution_catalog())


@pytest.mark.parametrize("role", ["api", "worker"])
def test_explicit_dummy_dev_mode_is_composed_without_initial_aws_requests(role):
    sdk = FakeSdk()
    runtime = build_runtime(role, environment(role, course_configuration(role)), client_factory=sdk)
    assert [name for name, _ in sdk.calls] == ["dynamodb", "s3"]
    assert sdk.logs == [] and sdk.s3.objects == {}
    if role == "api":
        assert runtime.target.course_mode == "course_v2"
        assert runtime.target.provider.learner.is_dummy is True
        assert runtime.target.gateway.submit({}).status == "disabled"
        # Proves Lambda dispatch selects the new contract before any auth/DB
        # call. Real session/calculation persistence is an integration test.
        old = handle({"httpMethod": "POST", "path": "/mock/v1/sessions", "body": "{}"}, context(), runtime.target)
        new = handle({"httpMethod": "POST", "path": "/api/v2/sessions/", "body": "{}"}, context(), runtime.target)
        assert old["statusCode"] == 404
        assert new["statusCode"] == 400
    else:
        assert runtime.target.completion_plan is not None
        assert runtime.target.course_recovery is not None


@pytest.mark.parametrize("change", ["mode", "version", "production", "beta", "missing_limit", "bool_limit",
                                     "unknown", "small_catalog", "small_bundle", "small_transaction", "relay"])
def test_invalid_dummy_dev_configuration_fails_offline_and_before_any_client(change):
    role = "relay" if change == "relay" else "api"
    config = course_configuration(role)
    if change == "mode":
        config["course"]["mode"] = "PRIVATE-MARKER"
    elif change == "version":
        config["course"]["catalog_version"] = "unreviewed"
    elif change in {"production", "beta"}:
        config["storage"]["stage"] = change
    elif change == "missing_limit":
        config["course"]["settings"].pop("max_assignments")
    elif change == "bool_limit":
        config["course"]["settings"]["max_assignments"] = True
    elif change == "unknown":
        config["course"]["unexpected"] = "PRIVATE-MARKER"
    elif change == "small_catalog":
        config["course"]["settings"]["max_assignments"] = 14
    elif change == "small_bundle":
        config["course"]["settings"]["max_bundle_bytes"] = 1
    elif change == "small_transaction":
        config["course"]["settings"]["max_transaction_actions"] = 6
    with pytest.raises(ValueError):
        AwsSettings.parse(json.dumps(config), role)
    calls = []
    with pytest.raises(JourneyError) as error:
        build_runtime(role, environment(role, config), client_factory=lambda *a, **k: calls.append(a))
    assert calls == []
    assert error.value.code == "TEMPORARILY_UNAVAILABLE"
    assert "PRIVATE-MARKER" not in str(error.value)


def test_complete_catalog_reuses_all_fifteen_existing_definitions_without_content_or_scores():
    source = provider()
    expected = Catalog(execution_catalog())
    bindings = source.list_assignments(source.learner)
    assert len(bindings) == len(PROGRAMS) * len(TARGETS) == 15
    covered = set()
    for binding in bindings:
        bundle = source.fetch_bundle(binding)
        assert bundle.scope.learner.principal == PRINCIPAL and bundle.scope.learner.is_dummy is True
        assert parse_json(bundle.course_json)["courseName"].startswith("[Dummy Dev]")
        assert [item.kind for item in bundle.placements] == ["training", "assessment"]
        for item in bundle.placements:
            row = source.mapping_document["mappings"][item.source_id]
            definition = parse_json(item.execution_json)
            assert definition == json.loads(expected.definition(row["program_id"], row["target"]))
            assert definition == row["execution"]
            assert "Not an ARC course" in parse_json(item.detail_json)["description"]
            assert parse_json(item.detail_json)["detail"]["content"] == []
            covered.add((row["program_id"], row["target"]))
        assert collect_exclusion_reasons(is_dummy=True, guideline="ARC2025", attempt_epoch="a",
                                         current_epoch="a", already_finalized=False) == ("dummy",)
    assert covered == {(program[0], target) for program in PROGRAMS for target in TARGETS}
    assert [source.fetch_bundle(binding).definition_hash for binding in bindings] == [
        provider().fetch_bundle(binding).definition_hash for binding in bindings]


@pytest.mark.parametrize("role", ["api", "worker"])
def test_serialized_course_snapshot_must_fit_explicit_storage_limit_before_client_creation(role):
    source = provider()
    bodies = [json_bytes(bundle_record(source.fetch_bundle(binding)))
              for binding in source.list_assignments(source.learner)]
    required = max(map(len, bodies))
    config = course_configuration(role)
    config["storage"].update(input_bytes=1, artifact_bytes=required - 1)
    with pytest.raises(ValueError):
        AwsSettings.parse(json.dumps(config), role)
    calls = []
    with pytest.raises(JourneyError):
        build_runtime(role, environment(role, config), client_factory=lambda *a, **k: calls.append(a))
    assert calls == []
    # The actual serialized length is the boundary, not an invented operating
    # quota. At the exact limit every complete snapshot can be stored/read.
    config["storage"]["artifact_bytes"] = required
    runtime = build_runtime(role, environment(role, config), client_factory=FakeSdk())
    storage = runtime.target.calculation.storage if role == "api" else runtime.target.storage
    for body in bodies:
        assert storage.get_course_blob(storage.put_course_blob(body)) == body


def test_dummy_provider_cannot_resolve_other_principal_or_return_another_learners_assignment():
    source = provider()
    assert source.resolve_learner(AuthContext("s", PRINCIPAL, 0, 100)) == source.learner
    other = replace(source.learner, principal="other@example.invalid", is_dummy=False)
    with pytest.raises(CourseError):
        source.resolve_learner(AuthContext("s", other.principal, 0, 100))
    with pytest.raises(CourseError):
        source.list_assignments(other)
    original = source.list_assignments(source.learner)[0]
    forged = AssignmentBinding(CourseScope(other, original.scope.enrollment_id, original.scope.course_id), original.public_ids)
    with pytest.raises(CourseError):
        source.fetch_bundle(forged)

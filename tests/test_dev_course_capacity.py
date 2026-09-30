"""Offline Dummy Dev catalog capacity check shared by local (and later AWS) assembly."""

from dataclasses import replace

import pytest

from mock_journey.course_settings import CourseSettings, fixture_course_settings
from mock_journey.course_records import bundle_record
from mock_journey.dev_course import MIN_TRANSACTION_ACTIONS, DummyDevCourseProvider, validate_dummy_catalog
from mock_journey.execution_definitions import execution_catalog
from mock_journey.typed import json_bytes


def largest_bundle_bytes(settings):
    provider = DummyDevCourseProvider(settings=settings, execution=execution_catalog())
    return max(len(json_bytes(bundle_record(provider.fetch_bundle(binding))))
               for binding in provider.list_assignments(provider.learner))


def test_valid_limits_return_the_full_fifteen_course_provider():
    provider = validate_dummy_catalog(fixture_course_settings(), execution=execution_catalog(), artifact_bytes=8_000_000)
    assert type(provider) is DummyDevCourseProvider
    assignments = provider.list_assignments(provider.learner)
    assert [binding.public_ids.course_id for binding in assignments] == list(range(910001, 910016))


def test_transaction_action_floor_is_the_final_assessment_write_set():
    assert MIN_TRANSACTION_ACTIONS == 8
    settings = fixture_course_settings()
    validate_dummy_catalog(replace(settings, max_transaction_actions=8), execution=execution_catalog(),
                           artifact_bytes=8_000_000)
    with pytest.raises(ValueError, match="Dummy Dev catalog"):
        validate_dummy_catalog(replace(settings, max_transaction_actions=7), execution=execution_catalog(),
                               artifact_bytes=8_000_000)


def test_every_serialized_bundle_must_fit_the_artifact_quota_exactly():
    settings = fixture_course_settings()
    largest = largest_bundle_bytes(settings)
    assert 0 < largest < settings.max_bundle_bytes
    validate_dummy_catalog(settings, execution=execution_catalog(), artifact_bytes=largest)
    with pytest.raises(ValueError, match="Dummy Dev catalog"):
        validate_dummy_catalog(settings, execution=execution_catalog(), artifact_bytes=largest - 1)


@pytest.mark.parametrize("settings,artifact_bytes", [
    (None, 8_000_000), ({"max_course_items": 64}, 8_000_000),
    (fixture_course_settings(), 0), (fixture_course_settings(), -1),
    (fixture_course_settings(), True), (fixture_course_settings(), 8_000_000.0),
    (replace(fixture_course_settings(), max_course_items=1), 8_000_000),
    (replace(fixture_course_settings(), max_assignments=14), 8_000_000),
    (replace(fixture_course_settings(), max_bundle_bytes=100), 8_000_000),
])
def test_untyped_or_too_small_limits_fail_with_one_fixed_message(settings, artifact_bytes):
    with pytest.raises(ValueError) as error:
        validate_dummy_catalog(settings, execution=execution_catalog(), artifact_bytes=artifact_bytes)
    assert str(error.value) == "The explicit course limits cannot hold the Dummy Dev catalog."
    assert error.value.__cause__ is None and error.value.__suppress_context__ is True


def test_validation_does_not_change_the_catalog():
    settings = fixture_course_settings()
    validated = validate_dummy_catalog(settings, execution=execution_catalog(), artifact_bytes=8_000_000)
    direct = DummyDevCourseProvider(settings=settings, execution=execution_catalog())
    assert validated.mapping_document == direct.mapping_document
    for binding in direct.list_assignments(direct.learner):
        assert json_bytes(bundle_record(validated.fetch_bundle(binding))) == json_bytes(
            bundle_record(direct.fetch_bundle(binding)))
    assert type(settings) is CourseSettings

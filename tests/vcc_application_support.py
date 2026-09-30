"""Course application assembly and Dummy login on a DynamoDB Local table.

Moved from integration_tests/vcc_application_support.py (E-14) so
http_pipeline_tests and integration_tests depend on tests/ only. The assembly
is tests/journey_support.compose on the ``HARNESS_DDB_*``
values with the fixture course bundle and a reduced execution catalog.
"""

import json
from types import SimpleNamespace

from mock_journey.assembly import ExecutionCatalog
from mock_journey.catalog import PROGRAMS, TARGETS
from mock_journey.course_fixture import FixtureCourseProvider
from mock_journey.course_settings import fixture_course_settings
from mock_journey.handler import handle
from mock_journey.projection import ProjectionSchema
from tests.journey_support import (
    HARNESS_DDB_ENVIRONMENT, HARNESS_DDB_KEYS, HARNESS_DDB_LIMITS, HARNESS_START, JourneyStore, compose,
)
from tests.vcc_support import dummy_learner, event, load_fixture, mapping_document


BUNDLE = load_fixture("course_bundle.json")
MAPPING = mapping_document()
CONTEXT = SimpleNamespace(aws_request_id="vcc-ddb")


def definitions():
    return {f"{program[0]}:{target}": {
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
    now = clock if clock is not None else (lambda: HARNESS_START)
    source = provider
    if source is None:
        source = FixtureCourseProvider(
            document=BUNDLE, settings=fixture_course_settings(), mapping_document=MAPPING,
        )
    composition = compose(
        JourneyStore(client, table), provider=source, keys=HARNESS_DDB_KEYS, environment=HARNESS_DDB_ENVIRONMENT,
        storage_limits=HARNESS_DDB_LIMITS, execution=execution(), mapping_document=MAPPING, clock=now,
    )
    return composition.build_api(dummy_learner=dummy_learner())


def login(app):
    response = handle(event("POST", "/api/v2/sessions/", body={
        "loginId": "test@test.com", "password": "2222",
    }), CONTEXT, app)
    body = json.loads(response["body"])
    assert response["statusCode"] == 201, body
    return body["data"]["accessToken"], body["data"]

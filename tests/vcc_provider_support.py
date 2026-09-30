"""Course fixture documents and provider builders shared by course provider tests.

Moved unchanged from tests/test_vcc_provider.py so other test modules no longer
import a test module; test_vcc_provider re-exports the same objects. ``source_bundle``
comes unchanged from tests/test_vcc_app_contract_hardening.py, which re-exports it.
"""

from pathlib import Path

from mock_journey.course_fixture import FixtureCourseProvider
from mock_journey.course_settings import fixture_course_settings
from mock_journey.models import AuthContext
from mock_journey.typed import parse_json


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "vcc_contract" / "v1"


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




def source_bundle(document=None):
    source = FixtureCourseProvider(document=document or BUNDLE_DOC, settings=fixture_course_settings())
    learner = source.resolve_learner(make_auth(real_principal()))
    bundle = source.fetch_bundle(source.list_assignments(learner)[0])
    return source, bundle

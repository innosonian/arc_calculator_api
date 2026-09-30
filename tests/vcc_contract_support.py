"""Course contract fixture documents and bundle builders shared by course contract tests.

Moved unchanged from tests/test_vcc_contract.py so other test modules no longer
import a test module; test_vcc_contract re-exports the same objects.
"""

from copy import deepcopy
from pathlib import Path

from mock_journey.course_contracts import CourseBundle, CourseScope, LearnerContext, Placement, PublicIds
from mock_journey.typed import parse_json


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "vcc_contract" / "v1"


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

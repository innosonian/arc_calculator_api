"""Course bundle, auth seeding and repository builders shared by course state tests.

Moved unchanged from tests/test_vcc_state.py so other test modules no longer
import a test module; test_vcc_state re-exports the same objects.
"""

from copy import deepcopy
from pathlib import Path

from mock_journey.course_contracts import (
    AssignmentBinding, CourseBundle, CourseScope, LearnerContext, Placement, PublicIds, StartCommand,
    sealed_bundle,
)
from mock_journey.course_policy import CoursePolicy
from mock_journey.course_settings import fixture_course_settings
from mock_journey.course_state import DynamoCourseRepository
from mock_journey.storage_keys import session_key, user_key
from tests.course_store_fakes import InMemoryBlobStore, InMemoryCourseStore
from mock_journey.models import AuthContext
from mock_journey.typed import parse_json
from tests.vcc_support import EPOCH


ROOT = Path(__file__).resolve().parents[1]
BUNDLE_DOC = parse_json((ROOT / "tests" / "fixtures" / "vcc_contract" / "v1" / "course_bundle.json").read_bytes())
SESSION = "60000000-0000-4000-8000-000000000001"
REQUEST = "10000000-0000-4000-8000-000000000001"
REQUEST_B = "11000000-0000-4000-8000-000000000001"
ATTEMPT = "50000000-0000-4000-8000-000000000001"
START = "40000000-0000-4000-8000-000000000001"
REPORT_A = "20000000-0000-4000-8000-000000000001"
REPORT_B = "30000000-0000-4000-8000-000000000001"


def learner_from(name="real"):
    return LearnerContext(**BUNDLE_DOC["learners"][name])


def placement_from(doc):
    return Placement(
        source_id=doc["source_id"], public_link_id=doc["public_link_id"], public_item_id=doc["public_item_id"],
        position=doc["position"], kind=doc["kind"], content_version=doc["content_version"],
        content_identity_json=deepcopy(doc["content_identity"]), detail_json=deepcopy(doc["detail"]),
        execution_json=deepcopy(doc["execution"]), execution_status=doc["execution_status"],
        duration_ms=doc["duration_ms"],
    )


def assignment_doc(enrollment_public_id=501):
    return next(item for item in BUNDLE_DOC["assignments"] if item["enrollment_public_id"] == enrollment_public_id)


def make_bundle(enrollment_public_id=501):
    assignment = assignment_doc(enrollment_public_id)
    learner = learner_from()
    scope = CourseScope(learner, assignment["scope"][3], assignment["scope"][4])
    public = PublicIds(assignment["course_public_id"], assignment["enrollment_public_id"], assignment["progress_id"])
    return sealed_bundle(CourseBundle(
        scope=scope, public_ids=public, source_revision=assignment["source_revision"],
        mapping_version=BUNDLE_DOC["mapping_version"], definition_hash=assignment["definition_hash"],
        placements=[placement_from(item) for item in BUNDLE_DOC["placements"]],
        course_json=deepcopy(BUNDLE_DOC["course_json"]),
        source_progress_json=deepcopy(BUNDLE_DOC["source_progress_json"]),
    ))


def binding_of(bundle):
    return AssignmentBinding(bundle.scope, bundle.public_ids)


def seed_auth(store, *, epoch=EPOCH, session_id=SESSION, principal=None, expires_at=2_000_000):
    learner = learner_from()
    principal = principal or learner.principal
    store.seed({
        **session_key(session_id),
        "session_id": session_id, "principal": principal, "token_hash": "hash-only",
        "issued_at": 1, "expires_at": expires_at, "status": "active", "revision": 0,
    })
    store.seed({
        **user_key(principal),
        "principal": principal, "epoch": epoch, "revision": 0, "slots": {},
    })
    return AuthContext(session_id, principal, 0, expires_at)


class SequencedUuid:
    def __init__(self, values):
        self.values = list(values)

    def __call__(self):
        return self.values.pop(0)


def repository(store=None, blobs=None, clock=1_000_000, uuids=None):
    store = store or InMemoryCourseStore()
    blobs = blobs or InMemoryBlobStore()
    uuid_factory = SequencedUuid(uuids or [START, ATTEMPT, REPORT_B]) if not callable(uuids) else uuids
    repo = DynamoCourseRepository(
        store, fixture_course_settings(), CoursePolicy(fixture_course_settings()), blobs,
        clock=lambda: clock, uuid_factory=uuid_factory,
    )
    return repo, store, blobs


def provision(repo, auth, bundle):
    ticket = repo.begin_inventory(auth, bundle.scope.learner)
    inventory = repo.apply_inventory(auth, ticket, (binding_of(bundle),))
    assert inventory.state == "ready"
    repo.ensure_epoch(auth, binding_of(bundle))
    refresh = repo.begin_refresh(auth, binding_of(bundle), ticket)
    gate = repo.apply_refresh(auth, refresh, bundle)
    assert gate.state == "ready"
    return repo.load_start_view(auth, StartCommand(REQUEST, bundle.public_ids.enrollment_id, bundle.public_ids.course_id, 1001, bundle.definition_hash))

"""Design R7: a local table used by the former --course-v2 provider, then the default Dummy Dev provider.

The former option assembled FixtureCourseProvider (tests/fixtures/vcc_contract)
with a dummy learner binding; the default local server now assembles
DummyDevCourseProvider without one, like AWS Dev. Rows written for the former
learner are never deleted or cleaned automatically, and a refresh cannot make
the new catalog ready in that epoch. The documented recovery is one Dummy
logout, which moves the shared epoch. In-memory store.
"""

from pathlib import Path

from mock_journey.auth import PRINCIPAL
from mock_journey.course_contracts import LearnerContext
from mock_journey.course_fixture import FixtureCourseProvider
from mock_journey.course_settings import fixture_course_settings
from mock_journey.typed import parse_json
from tests.journey_support import JourneyStore, V2Journey, dummy_course


FIXTURE = Path(__file__).parent / "fixtures" / "vcc_contract" / "v1"


class FormerCourseV2Journey(V2Journey):
    """The assembly the removed --course-v2 option used (test data only)."""

    def __init__(self, store, **options):
        document = parse_json((FIXTURE / "course_bundle.json").read_bytes())
        self.former_mapping = parse_json((FIXTURE / "execution_mapping.json").read_bytes())
        row = document["learners"]["dummy"]
        self.former_learner = LearnerContext(row["provider"], row["tenant_id"], row["learner_id"], PRINCIPAL, True)
        provider = FixtureCourseProvider(document=document, settings=fixture_course_settings(),
                                         mapping_document=self.former_mapping)
        super().__init__(store, provider=provider, mapping_document=self.former_mapping, **options)

    def build_api(self):
        from mock_journey.assembly import build_course_application
        return build_course_application(
            self.api_settings, dynamodb_client=self.store.client, s3_client=self.objects,
            legacy_bindings=self.bindings, resume_keys=self.resume_keys, current_key_version=self.key_version,
            execution=self.execution, provider=self.provider, course_settings=self.course_settings,
            mapping_document=self.former_mapping, dummy_learner=self.former_learner, clock=self.clock,
        )


def keys(store):
    return {(row["PK"], row["SK"]) for row in store.rows()}


def test_former_course_v2_rows_are_kept_and_one_dummy_logout_recovers_the_dummy_dev_courses():
    store = JourneyStore.memory()
    former = FormerCourseV2Journey(store)
    former_session = former.login()
    assert former.courses(former_session.token)["count"] == 0  # No synthetic enrollments.
    former_rows = {key for key in keys(store) if key[0].startswith("COURSE_LEARNER#")}
    assert former_rows, "The former learner inventory must exist to reproduce R7."

    current = V2Journey(store, objects=former.objects, start=former.now[0] + 10)
    session = current.login()
    # The shared epoch still holds the former learner's inventory: waiting,
    # and an explicit refresh does not rewrite or clean it.
    assert session.data["learningAvailability"] == {"state": "waiting", "reason": "arc_progress_unavailable"}
    assert current.refresh(session.token)["learningAvailability"]["state"] == "waiting"
    assert current.courses(session.token, expected=503)["code"] == "ARC_PROGRESS_UNAVAILABLE"
    assert former_rows <= keys(store)

    # Documented recovery: one Dummy logout moves the epoch; the next login is ready.
    current.logout(session.token)
    fresh = current.login()
    assert fresh.data["learningAvailability"] == {"state": "ready", "reason": None}
    assert current.courses(fresh.token)["count"] == 15
    course = dummy_course("mock-compression-only", "adult")
    started = current.start(fresh.token, course, course.practice_link_id)
    current.upload(fresh.token, started["attemptId"], started["condition"])
    assert current.work(started["attemptId"]) is True
    assert current.result(fresh.token, started["attemptId"])["evaluation"]["program_completed"] is True
    # The former learner rows are never deleted (design R2/R7).
    assert former_rows <= keys(store)

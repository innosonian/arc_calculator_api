"""D130/D131 repeat-after-pass scenarios on the public /api/v2 harness.

Each scenario takes a JourneyStore (memory in tests/, DynamoDB Local in
integration_tests/) and drives login, starts, uploads, the Worker and the
course views through tests/journey_support.V2Journey only. Expected values come
from docs/DECISIONS.md D130 (practice and assessment repeat regardless of the
result) and D131 (progress never regresses: a held completion/pass survives a
worse later attempt, the FINAL rests at ``passed`` after a retake ends, is
cancelled or is released, and a passing retake becomes the latest pass).
"""

from mock_journey.contracts import CURRENT_ADAPTER_VERSION
from mock_journey.cycle_goal import closed_cycle_count
from mock_journey.errors import JourneyError
from mock_journey.execution_definitions import PROJECTION_VERSION
from mock_journey.internal_calculator import InternalCalculator
from tests.journey_support import HARNESS_STAGE, V2Journey, dummy_course


COURSE = dummy_course("mock-compression-only", "adult")
ALREADY_COMPLETED = {"applied": False, "applied_epoch": None, "reason": "ALREADY_COMPLETED"}
LOGIN_FIELDS = ["sessionId", "expiresAt", "learningAvailability", "accessToken", "tokenType", "userName"]
SESSION_FIELDS = ["sessionId", "expiresAt", "learningAvailability"]


class LocalFailureCalculator(InternalCalculator):
    """The bundled calculator; ``fail_next`` makes one call a proven local CALCULATION_FAILED."""

    def __init__(self):
        super().__init__(version=CURRENT_ADAPTER_VERSION, projection_version=PROJECTION_VERSION,
                         stage=HARNESS_STAGE, cycle_goal_resolver=closed_cycle_count)
        self.fail_next = False

    def calculate(self, loaded, binding, heartbeat):
        if self.fail_next:
            self.fail_next = False
            raise JourneyError("CALCULATION_FAILED")
        return super().calculate(loaded, binding, heartbeat)


def harness(store):
    adapter = LocalFailureCalculator()
    h = V2Journey(store, adapters=[adapter])
    h.calculator = adapter
    return h


def final_row(h, attempt_id):
    attempt = h.store.row(f"ATTEMPT#{attempt_id}", "META")
    binding = attempt["course_binding"]
    return h.store.row(f"COURSE#{binding['scope_key']}", f"EPOCH#{attempt['epoch']}#FINAL")


def item_row(h, attempt_id):
    attempt = h.store.row(f"ATTEMPT#{attempt_id}", "META")
    binding = attempt["course_binding"]
    return h.store.row(f"COURSE#{binding['scope_key']}", f"EPOCH#{attempt['epoch']}#ITEM#{binding['placement_key']}")


def run(h, token, attempt, *, count=None):
    """Upload (optionally a failing measurement), finish the job and return the result view."""
    h.upload(token, attempt["attemptId"], attempt["condition"], count=count)
    assert h.work(attempt["attemptId"]) is True
    return h.result(token, attempt["attemptId"])


def items(h, token):
    detail = h.course(token, COURSE)
    return detail, {row["courseItemLinkId"]: row for row in detail["courseItems"]}


def assert_finished(h, token):
    # The course status is the list row's status; CourseDetail.enrollment.status is the provider's enrollment status.
    _, by_link = items(h, token)
    assert by_link[COURSE.practice_link_id]["isCompleted"] is True
    assert by_link[COURSE.final_link_id]["isCompleted"] is True
    assert by_link[COURSE.final_link_id]["isPassed"] is True
    listing = h.courses(token)
    assert next(row for row in listing["results"] if row["enrollmentId"] == COURSE.enrollment_id)["status"] == "FINISHED"


def pass_course(h, token):
    """Practice and final assessment both passed once; returns (practice, final) attempt views."""
    assert h.start(token, COURSE, COURSE.final_link_id, expected=409)["code"] == "PREREQUISITES_NOT_COMPLETED"
    practice = h.start(token, COURSE, COURSE.practice_link_id)
    assert run(h, token, practice)["progressApplication"]["reason"] == "APPLIED"
    final = h.start(token, COURSE, COURSE.final_link_id)
    assert final["role"] == "final_assessment"
    assert run(h, token, final)["progressApplication"]["reason"] == "APPLIED"
    rested = final_row(h, final["attemptId"])
    assert (rested["phase"], rested["active_attempt_id"], rested["passed_attempt_id"]) == (
        "passed", None, final["attemptId"])
    assert_finished(h, token)
    return practice, final


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

def scenario_login_user_name(store):
    """D129: userName only in the login reply, fixed to "Test User" for the Dummy account."""
    h = harness(store)
    session = h.login()
    assert list(session.data) == LOGIN_FIELDS
    assert session.data["userName"] == "Test User"
    assert list(h.session(session.token)) == SESSION_FIELDS
    assert list(h.refresh(session.token)) == SESSION_FIELDS


def scenario_completed_training_restarts_and_keeps_completion(store):
    """(a) A completed training starts again; a worse result is only recorded."""
    h = harness(store)
    token = h.login().token
    first = h.start(token, COURSE, COURSE.practice_link_id)
    assert run(h, token, first)["progressApplication"]["reason"] == "APPLIED"
    before = item_row(h, first["attemptId"])
    assert before["completed"] is True and before["completed_by_attempt"] == first["attemptId"]
    again = h.start(token, COURSE, COURSE.practice_link_id)
    assert again["role"] == "training" and again["attemptId"] != first["attemptId"]
    worse = run(h, token, again, count=2)
    assert worse["calculationStatus"] == "succeeded"
    assert worse["evaluation"]["program_completed"] is False
    assert worse["progressApplication"] == ALREADY_COMPLETED
    _, by_link = items(h, token)
    assert by_link[COURSE.practice_link_id]["isCompleted"] is True
    assert by_link[COURSE.practice_link_id]["isPassed"] is True
    after = item_row(h, again["attemptId"])
    assert (after["completed"], after["passed"], after["completed_by_attempt"]) == (True, True, first["attemptId"])
    # A better repeat is also only recorded (the completion already exists).
    better = h.start(token, COURSE, COURSE.practice_link_id)
    assert run(h, token, better)["progressApplication"] == ALREADY_COMPLETED
    assert item_row(h, better["attemptId"])["completed_by_attempt"] == first["attemptId"]


def scenario_passed_final_retake_keeps_then_updates_pass(store):
    """(b) A passed final retakes: FINISHED and the pass hold during the retake and after a failure; a pass updates."""
    h = harness(store)
    token = h.login().token
    practice, final = pass_course(h, token)
    retake = h.start(token, COURSE, COURSE.final_link_id)
    assert retake["role"] == "final_assessment" and retake["attemptId"] != final["attemptId"]
    active = final_row(h, retake["attemptId"])
    assert (active["phase"], active["active_attempt_id"], active["passed_attempt_id"]) == (
        "active", retake["attemptId"], final["attemptId"])
    assert_finished(h, token)  # D131: the course view keeps the held pass while the retake is active.
    # (d) one final attempt at a time is unchanged (D80/D82).
    assert h.start(token, COURSE, COURSE.final_link_id, expected=409)["code"] == "FINAL_ASSESSMENT_ACTIVE"
    failed = run(h, token, retake, count=2)
    assert failed["evaluation"]["program_completed"] is False
    assert failed["progressApplication"] == ALREADY_COMPLETED
    rested = final_row(h, retake["attemptId"])
    assert (rested["phase"], rested["active_attempt_id"], rested["passed_attempt_id"]) == (
        "passed", None, final["attemptId"])
    assert rested["passed_definition_hash"] == retake["definitionHash"]
    assert_finished(h, token)
    # A passing retake becomes the latest pass evidence.
    second = h.start(token, COURSE, COURSE.final_link_id)
    assert run(h, token, second)["progressApplication"] == ALREADY_COMPLETED
    updated = final_row(h, second["attemptId"])
    assert (updated["phase"], updated["active_attempt_id"], updated["passed_attempt_id"]) == (
        "passed", None, second["attemptId"])
    assert updated["passed_definition_hash"] == second["definitionHash"]
    assert item_row(h, second["attemptId"])["completed_by_attempt"] == final["attemptId"]
    assert_finished(h, token)
    # The earlier results stay readable as recorded.
    assert h.result(token, retake["attemptId"])["progressApplication"] == ALREADY_COMPLETED
    assert h.result(token, final["attemptId"])["progressApplication"]["reason"] == "APPLIED"


def scenario_retake_cancel_and_release_rest_at_passed(store):
    """(c) A cancelled retake and a released (proven local failure) retake both return to ``passed``."""
    h = harness(store)
    token = h.login().token
    _, final = pass_course(h, token)
    cancelled = h.start(token, COURSE, COURSE.final_link_id)
    h.cancel(token, cancelled["attemptId"])
    assert h.attempt(token, cancelled["attemptId"])["state"] == "cancelled"
    rested = final_row(h, cancelled["attemptId"])
    assert (rested["phase"], rested["active_attempt_id"], rested["passed_attempt_id"]) == (
        "passed", None, final["attemptId"])
    assert_finished(h, token)
    released = h.start(token, COURSE, COURSE.final_link_id)
    h.upload(token, released["attemptId"], released["condition"])
    assert final_row(h, released["attemptId"])["phase"] == "active"
    h.calculator.fail_next = True
    assert h.work(released["attemptId"]) is True
    assert h.calculator.fail_next is False
    job = h.worker.jobs.get_job(h.job_id(released["attemptId"]))
    assert job["state"] == "failed" and job["terminal_seal"]
    assert h.result(token, released["attemptId"], expected=503)["code"] == "CALCULATION_FAILED"
    rested = final_row(h, released["attemptId"])
    assert (rested["phase"], rested["active_attempt_id"], rested["passed_attempt_id"]) == (
        "passed", None, final["attemptId"])
    assert_finished(h, token)
    # The released enrollment accepts the next retake.
    again = h.start(token, COURSE, COURSE.final_link_id)
    assert again["attemptId"] not in {final["attemptId"], cancelled["attemptId"], released["attemptId"]}
    assert final_row(h, again["attemptId"])["active_attempt_id"] == again["attemptId"]


SCENARIOS = {
    "login_user_name": scenario_login_user_name,
    "completed_training_restarts": scenario_completed_training_restarts_and_keeps_completion,
    "passed_final_retake": scenario_passed_final_retake_keeps_then_updates_pass,
    "retake_cancel_and_release": scenario_retake_cancel_and_release_rest_at_passed,
}

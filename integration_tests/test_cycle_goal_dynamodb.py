"""D136 CPR course journey on DynamoDB Local (twin of tests/test_cycle_goal.py's in-memory journey)."""

from integration_tests.worker_journey_support import store  # noqa: F401 (fixture)
from tests.cycle_goal_support import cpr_course_journey


def test_cpr_course_practice_and_final_complete_by_the_cycle_rule(store):
    cpr_course_journey(store)

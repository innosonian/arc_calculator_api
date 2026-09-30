"""D138/D139 journeys on DynamoDB Local (twins of tests/test_eof_truncated_ventilation.py's in-memory journeys)."""

from integration_tests.worker_journey_support import store  # noqa: F401 (fixture)
from tests.eof_vent_support import (
    auto_stopped_ventilation_only_journey, in_flight_v4_attempts_keep_their_meaning_after_the_upgrade,
    recorded_cpr_below_minimum_journey,
)


def test_tester_recording_is_scored_but_fails_the_minimum_and_a_full_session_completes(store):
    recorded_cpr_below_minimum_journey(store)


def test_auto_stopped_ventilation_only_recording_completes_the_practice(store):
    auto_stopped_ventilation_only_journey(store)


def test_in_flight_v4_attempts_keep_their_meaning_after_the_upgrade(store):
    in_flight_v4_attempts_keep_their_meaning_after_the_upgrade(store)

"""Runtime requires explicit execution definitions and operating configuration."""

from mock_journey.errors import JourneyError


def get_worker():
    # Local dependency-injected worker execution is available without inventing
    # operating limits, lease duration, cycle goals or retention values.
    raise JourneyError("CALCULATOR_CONTRACT_MISMATCH")


def get_relay():
    raise JourneyError("TEMPORARILY_UNAVAILABLE")

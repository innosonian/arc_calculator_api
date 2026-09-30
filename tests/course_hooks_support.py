"""CourseHttp hook doubles in the record contract (course_contracts.CourseHooks, D128).

``hooks_with`` builds a complete CourseHooks from the hooks a test names; every
other hook answers 503 TEMPORARILY_UNAVAILABLE when reached. The record
builders take the plain field names the hooks used to return as dicts.
"""

from mock_journey.course_contracts import (
    HOOK_NAMES, AttemptRecord, CalculationRecord, ChartLinkRecord, CourseHooks, SessionRecord,
)
from mock_journey.course_errors import CourseError


def unavailable(*args, **kwargs):
    raise CourseError("TEMPORARILY_UNAVAILABLE")


def hooks_with(**given):
    unknown = set(given) - set(HOOK_NAMES)
    assert not unknown, unknown
    return CourseHooks(**{name: given.get(name, unavailable) for name in HOOK_NAMES})


def session_record(session_id, expires_at, *, auth=None, access_token=None, learning_availability=None,
                   user_name=None):
    return SessionRecord(session_id=session_id, expires_at=expires_at, auth=auth, access_token=access_token,
                         learning_availability=learning_availability, user_name=user_name)


def attempt_record(attempt_id, state, created_at, condition, **course_fields):
    return AttemptRecord(attempt_id=attempt_id, state=state, created_at=created_at, condition=condition,
                         **course_fields)


def calculation_record(attempt_id, state, **fields):
    return CalculationRecord(attempt_id=attempt_id, state=state, **fields)


def chart_link_record(url, expires_at):
    return ChartLinkRecord(url=url, expires_at=expires_at)

"""The operational log error_code allowlist is the fixed public error tables.

The allowlist is the union of the JourneyError table (mock_journey/errors.py)
and the course_v2 table (mock_journey/course_errors.py). Expected codes below
are the published fixed codes, written out independently of either module.
Extending the allowlist is a log-schema decision and is not tested here.
"""

import pytest

from services.operational_logs import validate_record


JOURNEY_CODES = {
    "INVALID_REQUEST", "LOGIN_FAILED", "SESSION_REQUIRED", "SESSION_EXPIRED", "SESSION_REVOKED", "NOT_FOUND",
    "PROGRAM_ALREADY_COMPLETED", "IDEMPOTENCY_CONFLICT", "ATTEMPT_INPUT_CONFLICT", "PROFILE_MISMATCH",
    "INVALID_STATE", "PAYLOAD_TOO_LARGE", "MEASUREMENT_INPUT_INVALID", "TEMPORARILY_UNAVAILABLE",
    "CALCULATOR_CONTRACT_MISMATCH", "CALCULATION_OUTCOME_UNKNOWN", "STORED_INPUT_INVALID", "CALCULATION_FAILED",
}
COURSE_ONLY_CODES = {
    "METHOD_NOT_ALLOWED", "CONTRACT_PENDING", "UPSTREAM_CONTRACT_MISMATCH", "ARC_PROGRESS_UNAVAILABLE",
    "PROGRESS_RECONCILIATION_REQUIRED", "DEFINITION_CHANGED", "EXECUTION_DEFINITION_MISSING",
    "EXECUTION_DEFINITION_UNSUPPORTED", "PREREQUISITES_NOT_COMPLETED",
    "FINAL_ASSESSMENT_ACTIVE", "FINAL_ASSESSMENT_RECOVERY_REQUIRED", "COMPLETION_POLICY_PENDING",
    "PROGRESS_CAPACITY_EXCEEDED", "CONTENT_VERSION_MISMATCH",
}


class Recorder:
    def __init__(self):
        self.records = []

    def record(self, category, event, fields):
        self.records.append((category, event, fields))


def recorded_error_code(code):
    from services.operational_logs import log_context, record_event

    recorder = Recorder()
    with log_context(recorder, request_id="local"):
        record_event("request_rejected", error_code=code, http_status=400)
    [(category, event, fields)] = recorder.records
    assert (category, event) == ("operation", "request_rejected")
    return fields.get("error_code", "<dropped>")


def test_expected_tables_match_the_fixed_error_modules():
    from mock_journey.course_errors import COURSE_ERROR_CODES, course_error_table

    assert set(course_error_table()) == JOURNEY_CODES | COURSE_ONLY_CODES
    assert COURSE_ERROR_CODES == JOURNEY_CODES | COURSE_ONLY_CODES


@pytest.mark.parametrize("code", sorted(JOURNEY_CODES | COURSE_ONLY_CODES))
def test_fixed_codes_are_kept(code):
    assert recorded_error_code(code) == code


@pytest.mark.parametrize("code", ["UNKNOWN_CODE", "invalid_request", " NOT_FOUND", "", 404, None, ["NOT_FOUND"]])
def test_other_codes_are_dropped(code):
    assert recorded_error_code(code) == "<dropped>"


def test_stored_operation_record_with_unknown_code_is_rejected():
    base = {"schema": 1, "log_id": "00000000-0000-4000-8000-000000000000",
            "occurred_at": "2026-09-28T00:00:00.000000Z", "role": "api", "category": "operation",
            "event": "request_rejected"}
    assert validate_record({**base, "fields": {"error_code": "CONTENT_VERSION_MISMATCH"}})
    with pytest.raises(ValueError):
        validate_record({**base, "fields": {"error_code": "UNKNOWN_CODE"}})

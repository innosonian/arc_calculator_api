"""Fixed VCC course errors. Existing JourneyError codes keep their status and message."""

from mock_journey.errors import _ERRORS


# D5 new codes. Existing codes stay in errors.py and are reused by name.
_COURSE_ERRORS = {
    "METHOD_NOT_ALLOWED": (405, "Method not allowed."),
    "CONTRACT_PENDING": (503, "The integration contract is not available."),
    "UPSTREAM_CONTRACT_MISMATCH": (503, "The upstream data contract is not verified."),
    "ARC_PROGRESS_UNAVAILABLE": (503, "Learning is waiting for ARC progress."),
    "PROGRESS_RECONCILIATION_REQUIRED": (409, "Progress reconciliation is required."),
    "DEFINITION_CHANGED": (409, "The course definition has changed."),
    "EXECUTION_DEFINITION_MISSING": (409, "The execution definition is missing."),
    "EXECUTION_DEFINITION_UNSUPPORTED": (422, "The execution definition is not supported."),
    "PREREQUISITES_NOT_COMPLETED": (409, "Prerequisite items are not completed."),
    "FINAL_ASSESSMENT_ACTIVE": (409, "A final assessment is already active."),
    "FINAL_ASSESSMENT_RECOVERY_REQUIRED": (409, "The final assessment requires recovery."),
    "COMPLETION_POLICY_PENDING": (409, "The completion policy is not available."),
    "PROGRESS_CAPACITY_EXCEEDED": (413, "The progress evidence limit is exceeded."),
    "CONTENT_VERSION_MISMATCH": (409, "The content version does not match the learning start."),
}

# Every existing journey code is reused by name (derived, not copied by hand).
REUSED_JOURNEY_CODES = frozenset(_ERRORS)
COURSE_ERROR_CODES = frozenset(_COURSE_ERRORS) | REUSED_JOURNEY_CODES
# Session/login failures always propagate; they are never turned into a
# learning "waiting" availability (D81/D89 cover only ARC progress failures).
AUTH_ERROR_CODES = frozenset({
    "LOGIN_FAILED", "SESSION_REQUIRED", "SESSION_EXPIRED", "SESSION_REVOKED",
})


def error_spec(code: str):
    # An unregistered code raises KeyError, not a CourseError (kept as is).
    if code in _COURSE_ERRORS:
        return _COURSE_ERRORS[code]
    return _ERRORS[code]


class CourseError(Exception):
    """Business/contract failure with a fixed code. Do not interpolate request text."""

    def __init__(self, code: str):
        self.status, self.message = error_spec(code)
        self.code = code
        super().__init__(code)


def course_error_table():
    """Merged code → {status, message} for schema tests. New codes win on name clash."""
    table = {code: {"status": spec[0], "message": spec[1]} for code, spec in _ERRORS.items()}
    table.update({code: {"status": spec[0], "message": spec[1]} for code, spec in _COURSE_ERRORS.items()})
    return table


# JourneyError remains the existing type for reused codes; CourseError looks them up by name.

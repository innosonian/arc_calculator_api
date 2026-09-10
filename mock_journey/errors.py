"""Fixed public errors; never interpolate request or upstream data."""

_ERRORS = {
    "INVALID_REQUEST": (400, "Invalid request."),
    "LOGIN_FAILED": (401, "Login failed."),
    "SESSION_REQUIRED": (401, "A valid session is required."),
    "SESSION_EXPIRED": (401, "The session has expired."),
    "SESSION_REVOKED": (403, "The session has been revoked."),
    "NOT_FOUND": (404, "Not found."),
    "PROGRAM_ALREADY_COMPLETED": (409, "This program and target are already completed."),
    "IDEMPOTENCY_CONFLICT": (409, "The request identifier has different input."),
    "ATTEMPT_INPUT_CONFLICT": (409, "The attempt already has different input."),
    "PROFILE_MISMATCH": (409, "The attempt profile does not match."),
    "INVALID_STATE": (409, "The operation is not allowed in this state."),
    "PAYLOAD_TOO_LARGE": (413, "The request exceeds the verified payload limit."),
    "MEASUREMENT_INPUT_INVALID": (422, "The measurement input is invalid."),
    "TEMPORARILY_UNAVAILABLE": (503, "The service is temporarily unavailable."),
    "CALCULATOR_CONTRACT_MISMATCH": (503, "The calculator contract is not verified."),
    "CALCULATION_OUTCOME_UNKNOWN": (503, "The calculator outcome is unknown."),
    "STORED_INPUT_INVALID": (503, "The stored input could not be verified."),
    "CALCULATION_FAILED": (503, "The calculation could not be completed."),
}


class JourneyError(Exception):
    def __init__(self, code: str):
        self.status, self.message = _ERRORS[code]
        self.code = code
        super().__init__(code)

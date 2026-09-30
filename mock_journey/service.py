"""Typed commands around authenticated, durable control-state operations.

The /api/v2 hooks (mock_journey.course_wiring) call these commands after the
course HTTP layer has parsed the request. Each keeps the checks, event order
and state transactions of the removed v1 body commands (D103).
"""

import json
from operator import itemgetter

from mock_journey.errors import JourneyError
from services.operational_logs import record_event, bind_identifiers


# Internal cancel reasons (course_contracts.CANCEL_REASON_WIRE_TO_INTERNAL values).
CANCEL_REASONS = ("user_stopped", "manikin_disconnected")

_VIEW_FIELDS = itemgetter("attempt_id", "state", "program_id", "target", "epoch", "profile_name")
_VIEW_DEFINITION = itemgetter("condition", "calculation_profile", "goal", "catalog_version", "profile_version")


def _require_viewable(attempt):
    """The reads the removed attempt view made of a reauthorized row, in its order.

    A stored row it could not show still fails (503) before attempt_reauthorized
    is recorded, so the event order for such a row is unchanged (D103).
    """
    definition = json.loads(attempt["definition_json"])
    _VIEW_FIELDS(attempt)
    _VIEW_DEFINITION(definition)


def _require_text(*values):
    """Every value is a nonempty str of at most 256 UTF-8 bytes, else INVALID_REQUEST."""
    if any(type(value) is not str for value in values):
        raise JourneyError("INVALID_REQUEST")
    try:
        if any(not value or len(value.encode("utf-8")) > 256 for value in values):
            raise JourneyError("INVALID_REQUEST")
    except UnicodeError:
        raise JourneyError("INVALID_REQUEST") from None


class JourneyService:
    def __init__(self, state, auth, calculation, *, operations=None):
        if calculation is None:
            raise ValueError("Invalid explicit journey composition.")
        self.state = state
        self.auth = auth
        self.calculation = calculation
        self.operations = operations

    def login_command(self, login_id, password):
        """Create one Dummy session; returns (session row, bearer token)."""
        _require_text(login_id, password)
        session, token = self.auth.login(login_id, password)
        bind_identifiers(session_id=session["session_id"])
        record_event("login_succeeded")
        return session, token

    def check_session(self, auth):
        # Coherent authoritative SESSION+USER recheck; a login does not reset UserState.
        self.state.read_session_user(auth)

    def reauthorize_command(self, auth, attempt_id, resume_credential):
        _require_text(resume_credential)
        attempt = self.state.get_attempt_for_reauthorization(attempt_id)
        digest = self.auth.verify_resume(attempt, auth.principal, resume_credential)
        _require_viewable(self.state.reauthorize_attempt(auth, attempt_id, digest))
        record_event("attempt_reauthorized", attempt_id=attempt_id)

    def cancel_command(self, auth, attempt_id, reason):
        _require_text(reason)
        if reason not in CANCEL_REASONS:
            raise JourneyError("INVALID_REQUEST")
        self.state.cancel_attempt(auth, attempt_id, reason)

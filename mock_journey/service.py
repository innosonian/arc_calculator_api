"""Application commands around authenticated, durable control-state operations."""

from datetime import datetime, timezone
import hashlib
import json
import uuid

from mock_journey.auth import LOGIN_ID, SESSION_SECONDS
from mock_journey.errors import JourneyError
from services.operational_logs import record_event, bind_identifiers


def _rfc3339(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat().replace("+00:00", "Z")


def _fields(body, names):
    if type(body) is not dict or set(body) != set(names) or any(type(body[name]) is not str for name in names):
        raise JourneyError("INVALID_REQUEST")
    try:
        if any(not body[name] or len(body[name].encode("utf-8")) > 256 for name in names):
            raise JourneyError("INVALID_REQUEST")
    except UnicodeError:
        raise JourneyError("INVALID_REQUEST") from None


class JourneyService:
    def __init__(self, state, auth, catalog, calculation=None, *, operations=None):
        self.state = state
        self.auth = auth
        self.catalog = catalog
        self.calculation = calculation
        self.operations = operations

    def require_calculation(self):
        if self.calculation is None:
            raise JourneyError("CALCULATOR_CONTRACT_MISMATCH")
        return self.calculation

    def login(self, body):
        _fields(body, ("login_id", "password"))
        session, token = self.auth.login(body["login_id"], body["password"], self.catalog.slot_keys)
        bind_identifiers(session_id=session["session_id"])
        record_event("login_succeeded")
        return {
            "environment": "mock", "session_id": session["session_id"], "session_token": token,
            "expires_in": SESSION_SECONDS, "expires_at": _rfc3339(session["expires_at"]),
            "mock_user": {"id": session["principal"], "display_id": LOGIN_ID},
        }

    def session(self, auth):
        # Coherent authoritative recheck; a login does not reset UserState.
        self.state.get_progress(auth)
        return {
            "environment": "mock", "session_id": auth.session_id,
            "expires_at": _rfc3339(auth.expires_at),
            "mock_user": {"id": auth.principal, "display_id": LOGIN_ID},
        }

    def programs(self, auth):
        return self.catalog.programs_view(self.state.get_progress(auth))

    def create_attempt(self, auth, body):
        _fields(body, ("client_request_id", "catalog_version", "program_id", "target"))
        digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        existing = self.state.get_created_attempt(auth, body["client_request_id"], digest)
        if existing is not None:
            view = self.attempt_view(existing)
            view["resume_credential"] = self.auth.resume_credential(existing)
            record_event("attempt_create_replayed", attempt_id=existing["attempt_id"], replayed=True)
            return 200, view
        self.catalog.validate_selection(body["program_id"], body["target"], body["catalog_version"])
        definition_json = self.catalog.definition(body["program_id"], body["target"])
        template = self.auth.prepare_resume({
            "attempt_id": str(uuid.uuid4()), "principal": auth.principal,
            "creator_session_id": auth.session_id, "bound_session_id": auth.session_id,
            "program_id": body["program_id"], "target": body["target"],
            "profile_name": "tester", "definition_json": definition_json,
        })
        attempt = self.state.create_attempt(auth, body["client_request_id"], digest, template)
        created = attempt["attempt_id"] == template["attempt_id"]
        view = self.attempt_view(attempt)
        view["resume_credential"] = self.auth.resume_credential(attempt)
        record_event("attempt_created" if created else "attempt_create_replayed",
                     attempt_id=attempt["attempt_id"], progress_epoch=attempt["epoch"],
                     program_id=attempt["program_id"], target=attempt["target"], replayed=not created)
        return (201 if created else 200), view

    def get_attempt(self, auth, attempt_id):
        return self.attempt_view(self.state.get_attempt(auth, attempt_id))

    def cancel(self, auth, attempt_id, body):
        _fields(body, ("reason",))
        if body["reason"] not in ("user_stopped", "manikin_disconnected"):
            raise JourneyError("INVALID_REQUEST")
        self.state.cancel_attempt(auth, attempt_id, body["reason"])

    def reauthorize(self, auth, attempt_id, body):
        _fields(body, ("resume_credential",))
        attempt = self.state.get_attempt_for_reauthorization(attempt_id)
        digest = self.auth.verify_resume(attempt, auth.principal, body["resume_credential"])
        result = self.attempt_view(self.state.reauthorize_attempt(auth, attempt_id, digest))
        record_event("attempt_reauthorized", attempt_id=attempt_id)
        return result

    @staticmethod
    def attempt_view(attempt):
        definition = json.loads(attempt["definition_json"])
        view = {
            "attempt_id": attempt["attempt_id"], "state": attempt["state"],
            "program_id": attempt["program_id"], "target": attempt["target"],
            "progress_epoch": attempt["epoch"], "profile_name": attempt["profile_name"],
            **{key: definition[key] for key in (
                "condition", "calculation_profile", "goal", "catalog_version", "profile_version",
            )},
            "evaluation": attempt.get("evaluation"),
            "progress_application": attempt.get("progress_application"),
        }
        view["calculation_path"] = f'/mock/v1/attempts/{attempt["attempt_id"]}/calculation'
        if attempt["state"] in ("outcome_unknown", "failed"):
            code = attempt.get("error_code", "CALCULATION_OUTCOME_UNKNOWN" if attempt["state"] == "outcome_unknown" else "CALCULATION_FAILED")
            if code not in ("CALCULATION_OUTCOME_UNKNOWN", "STORED_INPUT_INVALID", "CALCULATOR_CONTRACT_MISMATCH", "CALCULATION_FAILED"):
                code = "CALCULATION_FAILED"
            error = JourneyError(code)
            view["error"] = {"code": error.code, "message": error.message}
        return view

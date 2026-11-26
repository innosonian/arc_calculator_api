"""Dummy authentication and attempt-bound proofs, with no plaintext persistence."""

import base64
import hashlib
import hmac
import re
import secrets
import time
import uuid

from mock_journey.errors import JourneyError
from mock_journey.models import AuthContext
from mock_journey.settings import MAX_ENVIRONMENT_LENGTH, SessionSettings
from mock_journey.settings import SESSION_SECONDS  # re-export of the default for the route manifest


PRINCIPAL = "dummy-tester"
LOGIN_ID = "test@test.com"
# D129: the Dummy account's display name, returned only by the login response (userName).
DISPLAY_NAME = "Test User"
_TOKEN = re.compile(r"s1\.([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.([A-Za-z0-9_-]{43})\Z")
_VERSION = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def session_is_dummy(session):
    # The reserved-principal fallback is only for rows written before account metadata.
    value = session.get("is_dummy", session["principal"] == PRINCIPAL)
    if type(value) is not bool:
        raise JourneyError("TEMPORARILY_UNAVAILABLE")
    return value


def session_organization(session):
    organizations = session.get("organizations", [])
    if type(organizations) is not list or any(
        type(row) is not dict or set(row) != {"id", "name"}
        or any(type(value) is not str or not value for value in row.values())
        for row in organizations
    ):
        raise JourneyError("TEMPORARILY_UNAVAILABLE")
    options = tuple((row["id"], row["name"]) for row in organizations)
    ids = {row[0] for row in options}
    selected = session.get("organization_id")
    if len(ids) != len(options) or (selected is not None and (
        type(selected) is not str or not selected or (selected not in ids and not session_is_dummy(session))
    )):
        raise JourneyError("TEMPORARILY_UNAVAILABLE")
    if selected is None and len(options) == 1:
        selected = options[0][0]
    return options, selected


class AuthManager:
    def __init__(self, state, environment, keys, current_key_version, *, clock=time.time,
                 settings=SessionSettings()):
        if type(environment) is not str or not environment or len(environment) > MAX_ENVIRONMENT_LENGTH:
            raise ValueError("Invalid authentication environment.")
        if not keys or current_key_version not in keys or any(
            type(k) is not str or not _VERSION.fullmatch(k)
            or type(v) is not bytes or len(v) < 32 for k, v in keys.items()
        ):
            raise ValueError("Invalid resume key configuration.")
        self.state = state
        self.environment = environment
        self.keys = dict(keys)
        self.current_key_version = current_key_version
        if type(settings) is not SessionSettings:
            raise ValueError("Invalid session settings.")
        self.clock = clock
        self.settings = settings

    def login(self, login_id, password):
        if type(login_id) is not str or type(password) is not str:
            raise JourneyError("LOGIN_FAILED")
        valid_id = hmac.compare_digest(login_id.encode(), LOGIN_ID.encode())
        valid_password = hmac.compare_digest(password.encode(), b"2222")
        if not (valid_id and valid_password):
            raise JourneyError("LOGIN_FAILED")
        session_id = str(uuid.uuid4())
        token = f"s1.{session_id}.{secrets.token_urlsafe(32)}"
        now = int(self.clock())
        ttl = self.settings.ttl_seconds
        if self.settings.hard_cap_seconds is not None:
            ttl = min(ttl, self.settings.hard_cap_seconds)
        session = {
            "session_id": session_id, "principal": PRINCIPAL,
            "token_hash": _digest(token), "issued_at": now,
            "expires_at": now + ttl, "status": "active", "revision": 0,
            "is_dummy": True, "organizations": [],
        }
        self.state.create_session(session)
        return session, token

    def authenticate(self, token, *, allow_logout_receipt=False):
        if type(token) is not str or not (match := _TOKEN.fullmatch(token)):
            raise JourneyError("SESSION_REQUIRED")
        session = self.state.get_session(match[1])
        if not session or type(session.get("token_hash")) is not str or not hmac.compare_digest(
            _digest(token), session["token_hash"]
        ):
            raise JourneyError("SESSION_REQUIRED")
        if session.get("status") == "revoked":
            if not (allow_logout_receipt and (session.get("logout_epoch") or session.get("logout_receipt") is True)):
                raise JourneyError("SESSION_REVOKED")
        elif session.get("status") != "active":
            raise JourneyError("SESSION_REQUIRED")
        elif session["expires_at"] <= int(self.clock()):
            raise JourneyError("SESSION_EXPIRED")
        options, organization_id = session_organization(session)
        return AuthContext(session["session_id"], session["principal"], session["revision"], session["expires_at"],
                           options, organization_id)

    def prepare_resume(self, template):
        prepared = dict(template)
        prepared["resume_nonce"] = secrets.token_urlsafe(32)
        prepared["resume_key_version"] = self.current_key_version
        prepared["resume_digest"] = _digest(self.resume_credential(prepared))
        return prepared

    def resume_credential(self, attempt):
        version = attempt["resume_key_version"]
        key = self.keys.get(version)
        if key is None:
            raise JourneyError("TEMPORARILY_UNAVAILABLE")
        parts = (
            "resume:v1", self.environment, attempt["principal"], attempt["attempt_id"],
            attempt["creator_session_id"], attempt["resume_nonce"],
        )
        encoded = [part.encode("utf-8") for part in parts]
        message = b"".join(len(part).to_bytes(4, "big") + part for part in encoded)
        signature = base64.urlsafe_b64encode(hmac.digest(key, message, "sha256")).decode().rstrip("=")
        return f"r1.{version}.{signature}"

    def verify_resume(self, attempt, principal, credential):
        if not attempt or attempt.get("principal") != principal:
            raise JourneyError("NOT_FOUND")
        if type(credential) is not str or not credential.isascii() or len(credential) > 160:
            raise JourneyError("NOT_FOUND")
        if not hmac.compare_digest(_digest(credential), attempt["resume_digest"]):
            raise JourneyError("NOT_FOUND")
        expected = self.resume_credential(attempt)
        if not hmac.compare_digest(expected, credential):
            raise JourneyError("NOT_FOUND")
        return attempt["resume_digest"]

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


PRINCIPAL = "dummy-tester"
LOGIN_ID = "test@test.com"
SESSION_SECONDS = 86400
_TOKEN = re.compile(r"s1\.([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.([A-Za-z0-9_-]{43})\Z")
_VERSION = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def extract_bearer(event):
    """Resolve REST proxy's duplicate representations without accepting duplicates."""
    headers = event.get("headers") or {}
    multi = event.get("multiValueHeaders") or {}
    if type(headers) is not dict or type(multi) is not dict:
        raise JourneyError("SESSION_REQUIRED")
    single = [v for k, v in headers.items() if type(k) is str and k.lower() == "authorization"]
    multiple = [v for k, v in multi.items() if type(k) is str and k.lower() == "authorization"]
    if len(single) > 1 or len(multiple) > 1:
        raise JourneyError("SESSION_REQUIRED")
    values = single
    if multiple:
        if type(multiple[0]) is not list or len(multiple[0]) != 1:
            raise JourneyError("SESSION_REQUIRED")
        if single and single[0] != multiple[0][0]:
            raise JourneyError("SESSION_REQUIRED")
        values = multiple[0]
    if len(values) != 1 or type(values[0]) is not str:
        raise JourneyError("SESSION_REQUIRED")
    match = re.fullmatch(r"(?i:Bearer) (\S+)", values[0])
    if not match:
        raise JourneyError("SESSION_REQUIRED")
    return match[1]


class AuthManager:
    def __init__(self, state, environment, keys, current_key_version, *, clock=time.time):
        if type(environment) is not str or not environment or len(environment) > 128:
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
        self.clock = clock

    def login(self, login_id, password, slot_keys):
        if type(login_id) is not str or type(password) is not str:
            raise JourneyError("LOGIN_FAILED")
        valid_id = hmac.compare_digest(login_id.encode(), LOGIN_ID.encode())
        valid_password = hmac.compare_digest(password.encode(), b"2222")
        if not (valid_id and valid_password):
            raise JourneyError("LOGIN_FAILED")
        session_id = str(uuid.uuid4())
        token = f"s1.{session_id}.{secrets.token_urlsafe(32)}"
        now = int(self.clock())
        session = {
            "session_id": session_id, "principal": PRINCIPAL,
            "token_hash": _digest(token), "issued_at": now,
            "expires_at": now + SESSION_SECONDS, "status": "active", "revision": 0,
        }
        self.state.create_session(session, slot_keys)
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
            if not (allow_logout_receipt and session.get("logout_epoch")):
                raise JourneyError("SESSION_REVOKED")
        elif session.get("status") != "active":
            raise JourneyError("SESSION_REQUIRED")
        elif session["expires_at"] <= int(self.clock()):
            raise JourneyError("SESSION_EXPIRED")
        return AuthContext(session["session_id"], session["principal"], session["revision"], session["expires_at"])

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

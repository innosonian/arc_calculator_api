"""Allowlisted diagnostics; request data is never a logging payload.

These functions deliberately rebuild records instead of recursively trying to
recognize secrets. An exception's message, locals, request, and breadcrumbs are
not diagnostic fields. Only traceback locations from this checkout survive.
"""

import builtins
import math
import re
from pathlib import Path


_ROOT = Path(__file__).resolve().parent.parent
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_PATH_PART = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,127}\Z")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
_EVENT_ID = re.compile(r"[0-9a-f]{32}\Z")
_LEVELS = frozenset({"debug", "info", "warning", "error", "fatal"})
_EVENTS = frozenset({
    "request_start", "parse_multipart_start", "parse_form_start", "parse_complete",
    "request_failed", "request_complete", "sentry_init_failed",
    "raw_input_save_failed", "chart_upload_failed", "calc_failed",
    "calc_start", "calc_complete", "serialize_complete",
    "calc_response_complete", "chart_complete",
})
_ENUM_FIELDS = {
    "stage": frozenset({"test", "dev", "development", "staging", "prod", "production", "mock"}),
    "path": frozenset({"/cpr-analysis"}),
    "http_method": frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}),
    "condition_mode": frozenset({"training", "assessment"}),
    "condition_target": frozenset({"adult", "child", "infant"}),
    "condition_training_type": frozenset({"cpr", "compression_only", "ventilation_only"}),
    "condition_guideline": frozenset({"AHA2020", "ARC2020", "ARC2025", "ERC2020", "STD2015"}),
    "condition_cpr_cycle_type": frozenset({"302", "152"}),
    "step": frozenset({"calculate_cpr", "serialize_result", "add_chart_data"}),
}
_NUMERIC_FIELDS = frozenset({
    "cpr_bytes", "aed_bytes", "vp_event_count", "parse_ms", "calc_ms",
    "elapsed_ms", "content_length", "prepared_cpr_count", "prepared_aed_count",
    "whole_action_count", "comp_count", "vent_count",
})
_BOOL_FIELDS = frozenset({"is_base64_encoded", "condition_is_2rescuers"})
_ERROR_TYPES = frozenset(
    name for name, value in vars(builtins).items()
    if isinstance(value, type) and issubclass(value, BaseException)
) | {"ClientError", "BadDsn", "BotoCoreError"}
_REDACTED = "Exception details redacted."


def _enum(value, allowed):
    return value if type(value) is str and value in allowed else None


def _error_type(value):
    return value if type(value) is str and value in _ERROR_TYPES else "Exception"


def _traceback_frames(error):
    """Get code locations from a real exception, never supplied frame dictionaries."""
    frames = []
    trace = error.__traceback__ if isinstance(error, BaseException) else None
    while trace is not None and len(frames) < 64:
        code = trace.tb_frame.f_code
        try:
            relative = Path(code.co_filename).resolve().relative_to(_ROOT)
        except (ValueError, OSError):
            trace = trace.tb_next
            continue
        if (
            all(_PATH_PART.fullmatch(part) for part in relative.parts)
            and _IDENTIFIER.fullmatch(code.co_name)
        ):
            frames.append({
                "filename": relative.as_posix(),
                "function": code.co_name,
                "lineno": trace.tb_lineno,
            })
        trace = trace.tb_next
    return frames


def sanitize_log_record(level, message, fields):
    """Preserve the structured log skeleton and known scalar diagnostics only."""
    result = {
        "level": _enum(level, _LEVELS) or "info",
        "message": _enum(message, _EVENTS) or "diagnostic_event",
    }
    for key, value in fields.items():
        if key in _ENUM_FIELDS:
            result[key] = _enum(value, _ENUM_FIELDS[key])
        elif key in {"request_id", "attempt_id", "job_id"}:
            result[key] = value if type(value) is str and (
                _UUID.fullmatch(value) or (key == "request_id" and value == "local")
            ) else None
        elif key in _NUMERIC_FIELDS:
            if key == "content_length" and type(value) is str and re.fullmatch(r"[0-9]{1,19}", value):
                # Keep the existing header-string type without logging arbitrary text.
                result[key] = value
            else:
                result[key] = value if type(value) is int and 0 <= value < 2**63 else None
        elif key in _BOOL_FIELDS:
            result[key] = value if type(value) is bool else None
        elif key == "content_type":
            media_type = value.split(";", 1)[0].strip().lower() if type(value) is str else None
            result[key] = _enum(media_type, {
                "multipart/form-data", "application/x-www-form-urlencoded", "application/json",
            })
        elif key == "error_type":
            result[key] = _error_type(value)
        elif key == "error_message":
            result[key] = _REDACTED
        elif key == "stacktrace":
            result[key] = [_REDACTED]
    error = fields.get("exception")
    if isinstance(error, BaseException):
        result["error_type"] = _error_type(type(error).__name__)
        result["error_message"] = _REDACTED
        frames = _traceback_frames(error)
        result["stacktrace"] = [
            f'{frame["filename"]}:{frame["lineno"]} in {frame["function"]}' for frame in frames
        ] or [_REDACTED]
    return result


def before_send(event, hint):
    """Rebuild Sentry errors after processors have added request/scope data.

    SDK 2.22.0 appends hint attachments after before_send; remove them here too.
    Never copy arbitrary event fields, exception values, or serialized frames.
    """
    hint.pop("attachments", None)
    clean = {"platform": "python", "level": _enum(event.get("level"), _LEVELS) or "error"}
    event_id = event.get("event_id")
    if type(event_id) is str and _EVENT_ID.fullmatch(event_id):
        clean["event_id"] = event_id
    timestamp = event.get("timestamp")
    if type(timestamp) in (int, float) and 0 <= timestamp < 2**63 and math.isfinite(timestamp):
        clean["timestamp"] = timestamp
    # Capture metadata comes from Python's exception object, not scope/user data.
    exc_info = hint.get("exc_info")
    error = exc_info[1] if type(exc_info) is tuple and len(exc_info) == 3 else None
    if isinstance(error, BaseException):
        value = {"type": _error_type(type(error).__name__), "value": _REDACTED}
        frames = _traceback_frames(error)
        if frames:
            value["stacktrace"] = {"frames": frames}
        clean["exception"] = {"values": [value]}
    else:
        clean["message"] = "Application diagnostic event."
    return clean


def before_breadcrumb(breadcrumb, hint):
    """HTTP, SQL, and log breadcrumbs may contain secrets; none are forwarded."""
    return None


def before_send_transaction(event, hint):
    """Drop transactions and their separately attached profiles in SDK 2.22.0."""
    hint.pop("attachments", None)
    return None


def sentry_privacy_options():
    """Options verified against the repository's pinned sentry-sdk==2.22.0."""
    return {
        "send_default_pii": False,
        "include_local_variables": False,
        "include_source_context": False,
        "max_request_body_size": "never",
        "auto_session_tracking": False,
        "before_send": before_send,
        "before_breadcrumb": before_breadcrumb,
        "before_send_transaction": before_send_transaction,
    }

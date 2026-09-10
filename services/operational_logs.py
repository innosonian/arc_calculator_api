"""Bounded, fail-open operational recording; no clients or threads at import.

Only serialized allowlisted records cross the queue. A logging outage is not
an application transaction failure. Accepted in-memory records are not a
promise of durable storage; counters distinguish confirmed and unknown writes.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import queue
import re
import sys
import threading
import uuid

from services.observability import sanitize_log_record

MAX_RECORD_BYTES = 16_384
EVENTS = frozenset({
    "login_succeeded", "login_failed", "logout_succeeded", "progress_reset",
    "attempt_created", "attempt_create_replayed", "attempt_cancelled", "attempt_reauthorized",
    "calculation_accepted", "calculation_replayed", "calculation_started",
    "calculation_completed", "calculation_failed", "calculation_deferred", "progress_application",
    "request_rejected",
})
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z\Z")
_CONTEXT_FIELDS = frozenset({"request_id", "attempt_id", "job_id", "session_id", "progress_epoch"})
_ENUM_FIELDS = {
    "reason": frozenset({"user_stopped", "manikin_disconnected", "APPLIED", "PROGRESS_RESET",
                         "REQUIREMENTS_NOT_MET", "ALREADY_COMPLETED", "GOAL_POLICY_UNRESOLVED"}),
    "state": frozenset({"created", "cancelled", "queued", "processing", "evaluated", "failed", "outcome_unknown"}),
    "program_id": frozenset({"mock-cpr", "mock-compression-only", "mock-ventilation-only",
                              "mock-two-rescuer-cpr", "mock-two-rescuer-aed"}),
    "target": frozenset({"adult", "child", "infant"}),
}
_SCOPE = ContextVar("arc_operational_log_scope", default=(None, {}))


def _context(fields):
    return {key: value for key, value in fields.items() if key in _CONTEXT_FIELDS
            and type(value) is str and (_UUID.fullmatch(value) or (key == "request_id" and value == "local"))}


def _operation_fields(fields):
    from mock_journey.errors import _ERRORS
    clean = _context(fields)
    for key, allowed in _ENUM_FIELDS.items():
        if type(fields.get(key)) is str and fields[key] in allowed:
            clean[key] = fields[key]
    if type(fields.get("error_code")) is str and fields["error_code"] in _ERRORS:
        clean["error_code"] = fields["error_code"]
    for key in ("replayed", "applied", "program_completed"):
        if type(fields.get(key)) is bool:
            clean[key] = fields[key]
    for key in ("http_status", "elapsed_ms"):
        value = fields.get(key)
        if type(value) is int and 0 <= value < 2**63:
            clean[key] = value
    return clean


@contextmanager
def log_context(recorder, **identifiers):
    """Per-thread/task binding, restored on every return or exception."""
    token = _SCOPE.set((recorder, _context(identifiers)))
    try:
        yield
    finally:
        _SCOPE.reset(token)


def bind_identifiers(**identifiers):
    recorder, context = _SCOPE.get()
    _SCOPE.set((recorder, {**context, **_context(identifiers)}))


def record_event(name, **fields):
    try:
        recorder, context = _SCOPE.get()
        if recorder is not None and name in EVENTS:
            recorder.record("operation", name, _operation_fields({**context, **fields}))
    except Exception:
        # No exception payload or fallback request data is logged here.
        pass


def write_diagnostic(level, message, fields):
    """Keep unbound legacy stdout; bound runtimes hand off without waiting."""
    try:
        clean = sanitize_log_record(level, message, fields)
        recorder, context = _SCOPE.get()
        if recorder is None:
            print(json.dumps(clean))
        else:
            recorder.record("diagnostic", clean["message"], {**clean, **context})
    except Exception:
        # Broken stdout and serialization are logging failures, not scoring errors.
        pass


def validate_record(value):
    """Reject malformed persisted/queued records rather than exposing raw JSON."""
    if type(value) is not dict or set(value) != {"schema", "log_id", "occurred_at", "role", "category", "event", "fields"}:
        raise ValueError("Invalid operational log record.")
    if (type(value["schema"]) is not int or value["schema"] != 1
            or type(value["log_id"]) is not str or not _UUID.fullmatch(value["log_id"])
            or type(value["occurred_at"]) is not str or not _TIMESTAMP.fullmatch(value["occurred_at"])
            or value["role"] not in ("api", "worker", "relay")
            or type(value["event"]) is not str or type(value["fields"]) is not dict):
        raise ValueError("Invalid operational log record.")
    datetime.strptime(value["occurred_at"], "%Y-%m-%dT%H:%M:%S.%fZ")
    fields = value["fields"]
    if value["category"] == "operation":
        if value["event"] not in EVENTS or _operation_fields(fields) != fields:
            raise ValueError("Invalid operational log record.")
    elif value["category"] == "diagnostic":
        clean = sanitize_log_record(fields.get("level"), value["event"], fields)
        # Existing sanitizer derives these locations from real exceptions. Do
        # not accept arbitrary exception strings when reading a stored record.
        stack = fields.get("stacktrace")
        if stack is not None:
            frame = re.compile(r"[A-Za-z0-9_./-]{1,512}:[1-9][0-9]{0,8} in [A-Za-z_][A-Za-z0-9_]{0,127}\Z")
            if (type(stack) is not list or not 1 <= len(stack) <= 64
                    or any(type(s) is not str or not (s == "Exception details redacted." or frame.fullmatch(s)) for s in stack)):
                raise ValueError("Invalid operational log record.")
            clean["stacktrace"] = stack
        clean.update(_context(fields))
        if clean != fields or clean["message"] != value["event"]:
            raise ValueError("Invalid operational log record.")
    else:
        raise ValueError("Invalid operational log record.")
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()
    if len(raw) > MAX_RECORD_BYTES:
        raise ValueError("Operational log record exceeds its bound.")
    return raw


class AsyncLogRecorder:
    """One owned daemon writer, finite memory, bounded shutdown, no SDK on emit.

    store_factory runs only on the writer. It supplies a private client/store
    with bounded network timeouts. Runtime freeze/SIGKILL may lose pending logs.
    """
    def __init__(self, store_factory, *, role, capacity=256, clock=None, warning=None):
        if (not callable(store_factory) or role not in ("api", "worker", "relay")
                or type(capacity) is not int or not 1 <= capacity <= 4096):
            raise ValueError("Invalid operational log configuration.")
        self.factory, self.role = store_factory, role
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.warning = warning or self._warning
        self._queue = queue.Queue(maxsize=capacity)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._accepted = self._stored = self._unconfirmed = self._dropped = 0
        self._closed = False
        self._warned = set()
        self._thread = threading.Thread(target=self._run, name=f"arc-{role}-operational-logs", daemon=True)
        try:
            self._thread.start()
        except Exception:
            # Starting a log thread cannot veto an otherwise usable runtime.
            self._closed = True

    def record(self, category, event, fields):
        try:
            occurred = self.clock().astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            raw = validate_record({"schema": 1, "log_id": str(uuid.uuid4()), "occurred_at": occurred,
                                   "role": self.role, "category": category, "event": event, "fields": fields})
            with self._lock:
                if self._closed or not self._thread.is_alive():
                    self._dropped += 1
                    return False
                try:
                    self._queue.put_nowait(raw)
                except queue.Full:
                    self._dropped += 1
                    return False
                self._accepted += 1
            return True
        except Exception:
            with self._lock:
                self._dropped += 1
            return False

    def status(self):
        with self._lock:
            return {"scope": self.role + "_process", "accepted": self._accepted, "stored": self._stored,
                    "unconfirmed": self._unconfirmed, "dropped": self._dropped,
                    "pending": max(0, self._accepted - self._stored - self._unconfirmed),
                    "running": self._thread.is_alive() and not self._closed}

    def _warning(self, reason):
        # This is on the writer, never an HTTP/scoring thread. Even blocked
        # stderr must not hold the queue/counter lock or stop normal training.
        print(json.dumps({"level": "warning", "message": reason, **self.status()}), file=sys.stderr, flush=True)

    def _warn_once(self, reason):
        if reason not in self._warned:
            self._warned.add(reason)
            try:
                self.warning(reason)
            except Exception:
                pass

    def _run(self):
        store = None
        try:
            while not self._stop.is_set() or not self._queue.empty():
                try:
                    raw = self._queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                try:
                    if store is None:
                        store = self.factory()
                    store.write(raw)
                except Exception:
                    with self._lock:
                        self._unconfirmed += 1
                    self._warn_once("operational_log_write_unconfirmed")
                else:
                    with self._lock:
                        self._stored += 1
                finally:
                    self._queue.task_done()
                if self.status()["dropped"]:
                    self._warn_once("operational_logs_dropped")
        finally:
            if store is not None:
                try:
                    store.close()
                except Exception:
                    pass

    def close(self, timeout=1.0):
        if type(timeout) not in (int, float) or not 0 <= timeout <= 5:
            raise ValueError("Invalid operational log shutdown bound.")
        with self._lock:
            self._closed = True
        self._stop.set()
        if self._thread.ident is not None:
            self._thread.join(timeout=timeout)
        return not self._thread.is_alive()

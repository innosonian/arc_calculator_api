"""Invocation-scoped, bounded best-effort logs; never a durable-delivery promise.

Only one writer may be outstanding per manager/execution environment. A stuck
factory/write/close/warning occupies that slot instead of spawning more threads.
The caller waits a bounded time, and never performs log network I/O or stdout.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import threading
import time
import uuid

from services.operational_logs import log_context, validate_record


def remaining_ms(context):
    try:
        value = context.get_remaining_time_in_millis()
        return value if type(value) in (int, float) and 0 <= value < float("inf") else 0
    except Exception:
        return 0


class InvocationBuffer:
    def __init__(self, role, settings):
        self.role, self.settings = role, settings
        self.lock = threading.Lock()
        self.records, self.states = [], []
        self.bytes = self.dropped = 0
        self.sealed = False

    def record(self, category, event, fields):
        try:
            raw = validate_record({"schema": 1, "log_id": str(uuid.uuid4()),
                                   "occurred_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                                   "role": self.role, "category": category, "event": event, "fields": fields})
            with self.lock:
                if (self.sealed or len(self.records) >= self.settings.capacity
                        or self.bytes + len(raw) > self.settings.max_bytes):
                    self.dropped += 1
                    return False
                self.records.append(raw)
                self.states.append("pending")
                self.bytes += len(raw)
            return True
        except Exception:
            with self.lock:
                self.dropped += 1
            return False

    def status(self):
        with self.lock:
            return {"scope": self.role + "_invocation", "accepted": len(self.records),
                    "stored": self.states.count("stored"), "unconfirmed": self.states.count("unconfirmed"),
                    "dropped": self.dropped + self.states.count("dropped"),
                    "pending": self.states.count("pending"), "running": not self.sealed}

    def seal(self):
        with self.lock:
            self.sealed = True
            return tuple(self.records)

    def mark(self, index, state):
        with self.lock:
            self.states[index] = state

    def abandon_pending(self, state="dropped"):
        with self.lock:
            self.states = [state if value == "pending" else value for value in self.states]


class InvocationLogs:
    def __init__(self, store_factory, *, role, settings, warning=None):
        self.factory, self.role, self.settings = store_factory, role, settings
        self.warning = warning or self._warning
        self.current = ContextVar("aws_journey_logs_" + role, default=None)
        self.slot = threading.Lock()
        self.writer = None
        self.last_buffer = None
        self.count_lock = threading.Lock()
        self.unreported_dropped = self.unreported_unconfirmed = 0

    def _defer(self, buffer):
        """Retain counts only, never a skipped invocation's records or context."""
        with self.count_lock:
            if getattr(buffer, "_deferred", False):
                return
            status = buffer.status()
            self.unreported_dropped += status["dropped"]
            self.unreported_unconfirmed += status["unconfirmed"]
            buffer._deferred = True

    def _warn_pending(self, buffer):
        with self.count_lock:
            dropped, unconfirmed = self.unreported_dropped, self.unreported_unconfirmed
        if not (dropped or unconfirmed):
            return
        # A failed/blocked warning is never marked delivered. A later writer can
        # report the accumulated counts if this execution environment survives.
        self.warning({**buffer.status(), "unreported_dropped": dropped,
                      "unreported_unconfirmed": unconfirmed})
        with self.count_lock:
            self.unreported_dropped -= dropped
            self.unreported_unconfirmed -= unconfirmed

    def record(self, category, event, fields):
        buffer = self.current.get()
        return buffer.record(category, event, fields) if buffer is not None else False

    def status(self):
        buffer = self.current.get() or self.last_buffer
        return buffer.status() if buffer else {"scope": self.role + "_invocation", "accepted": 0,
                                              "stored": 0, "unconfirmed": 0, "dropped": 0,
                                              "pending": 0, "running": False}

    @staticmethod
    def _warning(status):
        print(json.dumps({"level": "warning", "message": "operational_log_batch_unconfirmed", **status}), flush=True)

    @contextmanager
    def invocation(self, context):
        buffer = InvocationBuffer(self.role, self.settings)
        token = self.current.set(buffer)
        try:
            with log_context(self, request_id=getattr(context, "aws_request_id", "local")):
                yield buffer
        finally:
            self.current.reset(token)
            self.last_buffer = buffer
            try:
                self._flush(buffer, context)
            except Exception:
                # Even thread creation and clock failures cannot alter business results.
                buffer.seal()
                buffer.abandon_pending("unconfirmed")
                self._defer(buffer)

    def _flush(self, buffer, context):
        batch = buffer.seal()
        budget = min(self.settings.flush_budget_ms,
                     max(0, remaining_ms(context) - self.settings.response_reserve_ms)) / 1000
        with self.count_lock:
            deferred = self.unreported_dropped or self.unreported_unconfirmed
        if not batch and not buffer.status()["dropped"] and not deferred:
            return
        if budget <= 0 or not self.slot.acquire(blocking=False):
            buffer.abandon_pending()
            self._defer(buffer)
            return
        deadline = time.monotonic() + budget
        started = False
        try:
            writer = threading.Thread(target=self._write, args=(buffer, batch, deadline),
                                      name="arc-aws-" + self.role + "-logs", daemon=True)
            self.writer = writer
            writer.start()
            started = True
            writer.join(timeout=max(0, deadline - time.monotonic()))
            if writer.is_alive():
                # In-flight writes may already have committed. Do not call them lost.
                buffer.abandon_pending("unconfirmed")
        finally:
            if not started:
                self.slot.release()

    def _write(self, buffer, batch, deadline):
        store = None
        try:
            for index, raw in enumerate(batch):
                if time.monotonic() >= deadline:
                    for remaining in range(index, len(batch)):
                        buffer.mark(remaining, "dropped")
                    break
                buffer.mark(index, "unconfirmed")
                try:
                    if store is None:
                        store = self.factory()
                    if time.monotonic() >= deadline:
                        buffer.mark(index, "dropped")
                        continue
                    store.write(raw)
                    buffer.mark(index, "stored")
                except Exception:
                    # No retry loop or exception text. Continue only inside the batch budget.
                    pass
        except Exception:
            buffer.abandon_pending("unconfirmed")
        finally:
            try:
                if store is not None:
                    try:
                        store.close()
                    except Exception:
                        pass
                self._defer(buffer)
                try:
                    self._warn_pending(buffer)
                except Exception:
                    pass
            finally:
                self.slot.release()

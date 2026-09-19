"""One owned Lambda renewal slot, bounded waits, and sticky ownership loss.

A callback can ignore its SDK timeout. In that case the lease is lost locally,
the final transaction is prohibited and the occupied slot blocks new work until
the callback ends. No callback or thread is reassigned to another job.
"""

import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar

from mock_journey.jobs import JobLeaseLost


class AwsLeaseGuardFactory:
    def __init__(self, *, lease_seconds, interval_seconds, renewal_timeout_seconds, response_reserve_ms=0):
        if (interval_seconds <= 0 or renewal_timeout_seconds <= 0
                or interval_seconds > lease_seconds / 3
                or interval_seconds + renewal_timeout_seconds >= lease_seconds):
            raise ValueError("Invalid AWS lease configuration.")
        self.interval, self.timeout = interval_seconds, renewal_timeout_seconds
        self.slot = threading.Lock()
        self.deadline = ContextVar("aws_lease_deadline", default=None)
        self.response_reserve_ms = response_reserve_ms

    @contextmanager
    def invocation(self, context):
        from mock_journey.aws_logs import remaining_ms
        deadline = time.monotonic() + max(0, remaining_ms(context) - self.response_reserve_ms) / 1000
        token = self.deadline.set(deadline)
        try:
            yield
        finally:
            self.deadline.reset(token)

    def __call__(self, heartbeat):
        return _Guard(self, heartbeat, self.deadline.get())

    def final_renew(self, heartbeat):
        with self(heartbeat):
            pass


class _Guard:
    def __init__(self, factory, heartbeat, deadline):
        self.factory, self.heartbeat = factory, heartbeat
        self.condition = threading.Condition()
        self.requested = self.completed = 0
        self.failed = self.stopped = False
        self.thread = None
        self.deadline = deadline

    def _lose(self):
        with self.condition:
            self.failed = self.stopped = True
            self.condition.notify_all()
        raise JobLeaseLost() from None

    def renew(self):
        if self.deadline is not None and time.monotonic() >= self.deadline:
            self._lose()
        deadline = time.monotonic() + self.factory.timeout
        if self.deadline is not None:
            deadline = min(deadline, self.deadline)
        with self.condition:
            if self.failed or self.stopped:
                raise JobLeaseLost()
            self.requested += 1
            request = self.requested
            self.condition.notify_all()
            while self.completed < request and not self.failed:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.failed = self.stopped = True
                    self.condition.notify_all()
                    raise JobLeaseLost()
                self.condition.wait(timeout=remaining)
            if self.failed or (self.deadline is not None and time.monotonic() >= self.deadline):
                self.failed = self.stopped = True
                self.condition.notify_all()
                raise JobLeaseLost()

    def _run(self):
        next_due = time.monotonic() + self.factory.interval
        try:
            while True:
                with self.condition:
                    while not self.stopped and self.completed == self.requested and time.monotonic() < next_due:
                        self.condition.wait(timeout=max(0, next_due - time.monotonic()))
                    if self.stopped:
                        return
                    request = self.requested
                started = time.monotonic()
                if self.deadline is not None and started >= self.deadline:
                    self._lose()
                try:
                    self.heartbeat()
                except Exception:
                    self._lose()
                with self.condition:
                    finished = time.monotonic()
                    if (finished - started > self.factory.timeout or self.failed
                            or (self.deadline is not None and finished >= self.deadline)):
                        self.failed = self.stopped = True
                        self.condition.notify_all()
                        return
                    self.completed = request
                    next_due = time.monotonic() + self.factory.interval
                    self.condition.notify_all()
        except BaseException:
            with self.condition:
                self.failed = self.stopped = True
                self.condition.notify_all()
        finally:
            self.factory.slot.release()

    def __enter__(self):
        if self.thread is not None or not self.factory.slot.acquire(blocking=False):
            raise JobLeaseLost()
        try:
            self.thread = threading.Thread(target=self._run, name="arc-aws-lease", daemon=True)
            self.thread.start()
        except Exception:
            self.factory.slot.release()
            raise JobLeaseLost() from None
        try:
            self.renew()
            return self.renew
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, exc_type, exc, traceback):
        with self.condition:
            self.stopped = True
            self.condition.notify_all()
        timeout = self.factory.timeout
        if self.deadline is not None:
            timeout = min(timeout, max(0, self.deadline - time.monotonic()))
        self.thread.join(timeout=timeout)
        if self.thread.is_alive() or self.failed:
            self.failed = True
            raise JobLeaseLost() from None
        return False

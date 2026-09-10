"""An explicitly owned local worker lease renewer; no SDK or HTTP runtime.

The callback must use the existing bounded local SDK client. This thread never
cancels a calculation. Renewal failures become sticky and block finalization.
A callback that cannot stop within the configured bound terminates the owning
worker process instead of accumulating abandoned renewal threads.
"""

import math
import threading
import time

from mock_journey.jobs import JobLeaseLost


class LocalLeaseGuardShutdown(SystemExit):
    """Fixed nonzero process exit; contains no callback exception or secrets."""

    def __init__(self):
        super().__init__(1)


class LocalLeaseGuardFactory:
    def __init__(self, *, lease_seconds, interval_seconds, renewal_timeout_seconds):
        if (type(lease_seconds) is not int or lease_seconds <= 0
                or any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0
                       for value in (interval_seconds, renewal_timeout_seconds))
                or interval_seconds > lease_seconds / 3
                or interval_seconds + renewal_timeout_seconds >= lease_seconds):
            raise ValueError("Invalid local lease renewal configuration.")
        self.lease_seconds = lease_seconds
        self.interval = interval_seconds
        self.timeout = renewal_timeout_seconds

    def __call__(self, heartbeat):
        if not callable(heartbeat):
            raise ValueError("Invalid local lease renewal callback.")
        return _LeaseGuard(heartbeat, self.interval, self.timeout)


class _LeaseGuard:
    def __init__(self, heartbeat, interval, timeout):
        self._heartbeat, self._interval, self._timeout = heartbeat, interval, timeout
        self._mutex = threading.Lock()
        self._stop = threading.Event()
        self._failed = threading.Event()
        self._thread = None
        self._entered = False

    def _lost(self):
        self._failed.set()
        self._stop.set()
        raise JobLeaseLost() from None

    def renew(self):
        return self._renew(periodic=False)

    def _renew(self, *, periodic):
        if self._failed.is_set():
            self._lost()
        if self._stop.is_set():
            if periodic:
                return
            self._lost()
        if not self._mutex.acquire(timeout=self._timeout):
            self._lost()
        try:
            if self._failed.is_set():
                self._lost()
            if self._stop.is_set():
                if periodic:
                    return
                self._lost()
            started = time.monotonic()
            try:
                self._heartbeat()
            except Exception:
                self._lost()
            if time.monotonic() - started > self._timeout:
                self._lost()
        finally:
            self._mutex.release()

    def _run(self):
        deadline = time.monotonic() + self._interval
        try:
            while not self._stop.wait(max(0, deadline - time.monotonic())):
                self._renew(periodic=True)
                deadline = max(deadline + self._interval, time.monotonic())
        except BaseException:
            # A thread exception cannot silently permit a completed result.
            # Keep no exception object/traceback or callback output.
            self._failed.set()
            self._stop.set()

    def __enter__(self):
        if self._entered:
            raise ValueError("A local lease guard cannot be reused.")
        self._entered = True
        self.renew()
        # The worker process owns this thread. If an SDK bug ignores its socket
        # timeout, a bounded stop raises nonzero SystemExit and the supervisor
        # observes failure; interpreter shutdown must not wait on this thread.
        self._thread = threading.Thread(target=self._run, name="arc-local-lease", daemon=True)
        try:
            self._thread.start()
        except BaseException:
            self._stop.set()
            if self._thread.is_alive():
                self._thread.join(timeout=self._timeout)
                if self._thread.is_alive():
                    raise LocalLeaseGuardShutdown() from None
            raise
        return self.renew

    def __exit__(self, exc_type, exc, traceback):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._timeout)
            if self._thread.is_alive():
                self._failed.set()
                raise LocalLeaseGuardShutdown() from None
        if self._failed.is_set():
            raise JobLeaseLost() from None
        return False

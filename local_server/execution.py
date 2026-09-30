"""Caller-owned durable local job execution.

No catalog, client, storage destination, operating limit, or background thread
is created implicitly. The owner (local_server.runtime's spawned worker, or an
explicitly assembled test) supervises ``run`` independently of HTTP.
Jobs/outboxes remain in DynamoDB if this process stops; the next supervised
runner queries the existing due index and reuses the existing lease fences.
"""

import math
import time

from mock_journey.dispatch import OutboxRelay
from mock_journey.errors import JourneyError


_ERROR = "Invalid explicit local execution configuration."


class _WorkerSender:
    """A local wake runs the existing worker; it is not an external queue."""

    def __init__(self, worker):
        self.worker = worker

    def send(self, job_id):
        # Returning False includes a busy/expired lease or a transient failure.
        # Do not acknowledge an outbox until the worker reports a terminal job.
        if self.worker.process(job_id) is not True:
            raise JourneyError("TEMPORARILY_UNAVAILABLE")


class LocalJobRunner:
    """Poll durable outboxes and jobs, with no in-request daemon or new queue.

    ``jobs`` must expose the existing GSI1 due queries and outbox CAS methods.
    This class never creates or migrates that index. ``run_once`` is bounded
    by the explicitly supplied page size/page count; individual calculations
    are not forcibly interrupted. A stop request takes effect after the
    current bounded reconciliation finishes, preserving worker cleanup.
    """

    def __init__(self, jobs, worker, *, lease_seconds, retry_seconds, page_size,
                 max_pages, clock=time.time):
        if (any(not callable(getattr(jobs, name, None)) for name in (
                "claim_outbox", "mark_outbox_sent", "release_outbox", "due_outbox", "due_jobs"))
                or not callable(getattr(worker, "process", None)) or not callable(clock)):
            raise ValueError(_ERROR)
        self._relay = OutboxRelay(
            jobs, _WorkerSender(worker), lease_seconds=lease_seconds,
            retry_seconds=retry_seconds, page_size=page_size, max_pages=max_pages, clock=clock,
        )

    def run_once(self):
        """Return existing relay counts; repository failures propagate safely.

        Sent outboxes do not hide due jobs. Repeated wakes are expected and
        remain harmless through JourneyWorker/DynamoJobRepository fencing.
        """
        return self._relay.reconcile()

    def run(self, stop_event, *, poll_interval):
        """Run under an explicit process/thread owner until stop or failure.

        Poll intervals have no default and must be finite positive seconds.
        No exception is logged here: a fatal repository/configuration error
        exits to the supervisor instead of silently reporting a live worker.
        Restarting retries durable due rows after their recorded lease/due time.
        """
        if (not callable(getattr(stop_event, "is_set", None))
                or not callable(getattr(stop_event, "wait", None))
                or type(poll_interval) not in (int, float)
                or not math.isfinite(poll_interval) or poll_interval <= 0):
            raise ValueError(_ERROR)
        while not stop_event.is_set():
            self.run_once()
            if stop_event.wait(poll_interval):
                break

"""Explicit local composition and caller-owned durable job execution.

No catalog, client, storage destination, operating limit, or background thread
is created implicitly. The caller supervises ``run`` independently of HTTP.
Jobs/outboxes remain in DynamoDB if this process stops; the next supervised
runner queries the existing due index and reuses the existing lease fences.
"""

from dataclasses import dataclass
import math
import time

from mock_journey.assembly import build_application, build_worker, ExecutionCatalog
from mock_journey.dispatch import OutboxRelay
from mock_journey.errors import JourneyError
from mock_journey.internal_calculator import InternalCalculator
from mock_journey.settings import ApiSettings, WorkerSettings


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


@dataclass(frozen=True)
class LocalExecution:
    service: object
    worker: object
    runner: LocalJobRunner


def build_local_execution(api_settings, worker_settings, *, dynamodb_client,
                          s3_client, legacy_bindings, resume_keys,
                          current_key_version, execution, adapters,
                          relay_lease_seconds, relay_retry_seconds, page_size,
                          max_pages, clock=time.time, lease_guard_factory=None):
    """Assemble existing API/worker roles with one explicit storage/state scope.

    Only bundled InternalCalculator adapters are accepted. Their current and
    retained versions must be supplied by the caller. Calculation uses this
    repository's core. Factory success proves composition, not active
    supervision, persistence/retention policy, GSI1 existence, or deployment.
    The default local CLI does not call this factory automatically.
    """
    try:
        if (type(api_settings) is not ApiSettings or type(worker_settings) is not WorkerSettings
                or type(execution) is not ExecutionCatalog
                or api_settings.state != worker_settings.state
                or api_settings.storage != worker_settings.storage
                or type(adapters) not in (list, tuple) or not adapters
                or any(type(adapter) is not InternalCalculator
                       or adapter.stage != worker_settings.storage.stage for adapter in adapters)):
            raise ValueError(_ERROR)
        service = build_application(
            api_settings, dynamodb_client=dynamodb_client, s3_client=s3_client,
            legacy_bindings=legacy_bindings, resume_keys=resume_keys,
            current_key_version=current_key_version, execution=execution, clock=clock,
        )
        # Keep explicitly supplied old versions available for previously
        # accepted jobs; never discover or replace their stored definitions.
        required_bindings = tuple(sorted(set(execution.required_bindings) | {
            (adapter.version, adapter.projection_version) for adapter in adapters
        }))
        worker = build_worker(
            worker_settings, dynamodb_client=dynamodb_client, s3_client=s3_client,
            legacy_bindings=legacy_bindings, adapters=adapters,
            required_bindings=required_bindings, clock=clock,
            lease_guard_factory=lease_guard_factory,
        )
        runner = LocalJobRunner(
            worker.jobs, worker, lease_seconds=relay_lease_seconds,
            retry_seconds=relay_retry_seconds, page_size=page_size,
            max_pages=max_pages, clock=clock,
        )
        return LocalExecution(service, worker, runner)
    except Exception:
        # Explicit configuration can contain credentials/paths. Preserve no
        # dependency message in the public composition exception.
        raise ValueError(_ERROR) from None

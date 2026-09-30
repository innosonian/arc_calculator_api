"""Local product composition and supervision of one owned spawned worker.

No SDK session, sockets, keys, policies or processes are created at import.
The CLI has already isolated the environment and holds the installation lock.
"""

from dataclasses import dataclass, field
import math
import multiprocessing
import os
import signal
import threading
import time


from local_server.constants import (
    BODY_LIMIT, DEFAULT_ARTIFACT_BYTES, DEFAULT_CALCULATION_BODY_BYTES, DEFAULT_STORAGE_QUOTA_BYTES,
    DEFAULT_WORKER_LEASE_SECONDS, DEFAULT_WORKER_POLL_SECONDS, DEFAULT_WORKER_RETRY_SECONDS,
)
from mock_journey.course_settings import (
    FIXTURE_MAX_ASSIGNMENTS, FIXTURE_MAX_BUNDLE_BYTES, FIXTURE_MAX_CONFLICT_RETRIES, FIXTURE_MAX_CONTROL_BODY_BYTES,
    FIXTURE_MAX_COURSE_ITEMS, FIXTURE_MAX_INTERVALS_PER_REPORT, FIXTURE_MAX_MERGED_INTERVALS_PER_START,
    FIXTURE_MAX_REPORTS_PER_START, FIXTURE_MAX_TRANSACTION_ACTIONS,
)
from mock_journey.execution_definitions import PROJECTION_VERSION, execution_catalog
from mock_journey.settings import base64_body_bytes
STORAGE_BUCKET = "arc-local-private"
STORAGE_DIRECTORY = "calculator_result/interpreted_rtdata/arc"
STORAGE_STAGE = "local"
_ERROR = "The owned local journey runtime is unavailable."
# Local role wiring values (not deployment policy). The journey state
# repository retries a conflicting transaction this many times.
_STATE_CONFLICT_RETRIES = 4
# Lease renewal every lease/4 seconds (LocalLeaseGuardFactory requires at
# most lease/3), each renewal bounded by min(15 s, lease/4).
_LEASE_RENEWAL_DIVISOR = 4
_LEASE_RENEWAL_TIMEOUT_CAP_SECONDS = 15
# Outbox/job reconciliation bounds per worker poll.
_RELAY_PAGE_SIZE = 20
_RELAY_MAX_PAGES = 5


class RuntimeUnavailable(RuntimeError):
    def __init__(self):
        super().__init__(_ERROR)


@dataclass(frozen=True)
class LocalOptions:
    """Bounded, explicit local starting values; not ARC or deployment policy."""
    calculation_body_bytes: int = DEFAULT_CALCULATION_BODY_BYTES
    artifact_bytes: int = DEFAULT_ARTIFACT_BYTES
    storage_quota_bytes: int = DEFAULT_STORAGE_QUOTA_BYTES
    worker_lease_seconds: int = DEFAULT_WORKER_LEASE_SECONDS
    worker_retry_seconds: int = DEFAULT_WORKER_RETRY_SECONDS
    worker_poll_seconds: float = DEFAULT_WORKER_POLL_SECONDS

    def __post_init__(self):
        if (any(type(value) is not int or value <= 0 for value in (
                self.calculation_body_bytes, self.artifact_bytes, self.storage_quota_bytes,
                self.worker_lease_seconds, self.worker_retry_seconds))
                or self.artifact_bytes < self.calculation_body_bytes
                or type(self.worker_poll_seconds) not in (int, float)
                or not math.isfinite(self.worker_poll_seconds) or self.worker_poll_seconds <= 0):
            raise ValueError("Invalid explicit local journey limits.")

    @property
    def payload_limit(self):
        # Encoded event-body limit admitting the whole wire limit after base64.
        return base64_body_bytes(self.calculation_body_bytes)

    @property
    def response_body_limit(self):
        # Leave room for the submission overlay around a maximum stored result.
        return self.artifact_bytes + BODY_LIMIT


def _settings(database, material, options):
    from mock_journey.settings import ApiSettings, StateSettings, StorageSettings, WorkerSettings
    state = StateSettings(database.table_name, _STATE_CONFLICT_RETRIES)
    storage = StorageSettings(STORAGE_STAGE, STORAGE_BUCKET, STORAGE_DIRECTORY,
                              options.calculation_body_bytes, options.artifact_bytes)
    return (ApiSettings(state, storage, material.environment, options.payload_limit),
            WorkerSettings(state, storage, options.worker_lease_seconds, options.worker_retry_seconds))


def _objects(material, options, host, port, *, object_material=None):
    from local_server.object_storage import prepare_object_material, LocalObjectClient, LocalLegacyBindings
    from local_server.charts import LocalChartService

    if object_material is None:
        object_material = prepare_object_material(material)
    if (object_material.installation_id != material.installation_id
            or object_material.data_dir != material.data_dir):
        raise RuntimeUnavailable()
    objects = LocalObjectClient(object_material, bucket=STORAGE_BUCKET,
                               directory=STORAGE_DIRECTORY, stage=STORAGE_STAGE,
                               artifact_limit=options.artifact_bytes, quota_bytes=options.storage_quota_bytes)
    try:
        charts = LocalChartService(objects, base_url=f"http://{host}:{port}")
        return objects, charts, LocalLegacyBindings(objects, charts)
    except BaseException:
        objects.close()
        raise


# Local implementation defaults for the /api/v2 course limits: the product
# default equals the course_settings FIXTURE_* values (one definition, D12);
# not ARC, AWS or operating quotas. No synthetic catalog module is imported.
LOCAL_COURSE_LIMITS = {
    "max_course_items": FIXTURE_MAX_COURSE_ITEMS,
    "max_assignments": FIXTURE_MAX_ASSIGNMENTS,
    "max_bundle_bytes": FIXTURE_MAX_BUNDLE_BYTES,
    "max_control_body_bytes": FIXTURE_MAX_CONTROL_BODY_BYTES,
    "max_intervals_per_report": FIXTURE_MAX_INTERVALS_PER_REPORT,
    "max_merged_intervals_per_start": FIXTURE_MAX_MERGED_INTERVALS_PER_START,
    "max_reports_per_start": FIXTURE_MAX_REPORTS_PER_START,
    "max_transaction_actions": FIXTURE_MAX_TRANSACTION_ACTIONS,
    "max_conflict_retries": FIXTURE_MAX_CONFLICT_RETRIES,
}


def local_course_settings():
    """Explicit local CourseSettings, defined in product code; no test data is read."""
    from mock_journey.course_settings import CourseSettings
    return CourseSettings(**LOCAL_COURSE_LIMITS)


def build_api(database, material, options, host, port):
    """The /api/v2 API role over the Dummy Dev catalog, assembled like AWS Dev.

    Dummy login receives the 15 temporary DummyDevCourseProvider courses. The
    catalog is validated against the local course limits and artifact quota
    before any private file handle is opened. No dummy learner binding.
    """
    from mock_journey.assembly import build_course_application
    from mock_journey.dev_course import validate_dummy_catalog

    api_settings, _ = _settings(database, material, options)
    execution = execution_catalog()
    course_settings = local_course_settings()
    provider = validate_dummy_catalog(course_settings, execution=execution,
                                      artifact_bytes=options.artifact_bytes)
    objects, charts, legacy = _objects(material, options, host, port)
    try:
        service = build_course_application(
            api_settings, dynamodb_client=database.client, s3_client=objects, legacy_bindings=legacy,
            resume_keys={material.key_version: material.resume_key},
            current_key_version=material.key_version, execution=execution,
            provider=provider, course_settings=course_settings,
            mapping_document=provider.mapping_document,
            operations=getattr(database, "operations", None),
        )
        return service, objects, charts
    except BaseException:
        objects.close()
        raise


def build_local_worker(database, material, options, host, port, *, object_material):
    from mock_journey.assembly import build_worker, worker_adapters, worker_required_bindings
    from mock_journey.contracts import CURRENT_ADAPTER_VERSION, RETAINED_ADAPTER_VERSIONS
    from local_server.execution import LocalJobRunner
    from local_server.lease import LocalLeaseGuardFactory

    _, worker_settings = _settings(database, material, options)
    objects, charts, legacy = _objects(material, options, host, port, object_material=object_material)
    try:
        # The whole code registry, as AWS requires of its setting (D127).
        retained = RETAINED_ADAPTER_VERSIONS
        adapters = worker_adapters(CURRENT_ADAPTER_VERSION, retained,
                                   projection=PROJECTION_VERSION, stage=STORAGE_STAGE)
        guard = LocalLeaseGuardFactory(
            lease_seconds=options.worker_lease_seconds,
            interval_seconds=options.worker_lease_seconds / _LEASE_RENEWAL_DIVISOR,
            renewal_timeout_seconds=min(_LEASE_RENEWAL_TIMEOUT_CAP_SECONDS,
                                        options.worker_lease_seconds / _LEASE_RENEWAL_DIVISOR),
        )
        worker = build_worker(worker_settings, dynamodb_client=database.client, s3_client=objects,
                              legacy_bindings=legacy, adapters=adapters,
                              # Same formula as AWS: every retained binding's
                              # adapter is registered above, so its startup
                              # resolve check always succeeds.
                              required_bindings=worker_required_bindings(execution_catalog(), retained,
                                                                         projection=PROJECTION_VERSION),
                              lease_guard_factory=guard, operations=getattr(database, "operations", None))
        runner = LocalJobRunner(worker.jobs, worker, lease_seconds=options.worker_lease_seconds,
                                retry_seconds=options.worker_retry_seconds, page_size=_RELAY_PAGE_SIZE,
                                max_pages=_RELAY_MAX_PAGES)
        return runner, objects
    except BaseException:
        objects.close()
        raise


@dataclass(frozen=True)
class WorkerConfiguration:
    """Validated installation keys use private spawn IPC, never argv/env/logs."""
    material: object = field(repr=False)
    object_material: object = field(repr=False)
    endpoint: str
    host: str
    port: int
    options: LocalOptions
    parent_pid: int


def _watch_parent(parent_pid, stopped):
    # A killed parent cannot run its finally blocks. Exit only this owned
    # worker when it becomes an orphan; never signal a discovered process.
    while not stopped.wait(0.25):
        if os.getppid() != parent_pid:
            os._exit(1)


class _StopReceiver:
    """Child-only pipe polling, with no shared semaphore or waiter ACK."""
    def __init__(self, connection):
        self.connection = connection
        self._stopped = False

    def is_set(self):
        return self.wait(0)

    def wait(self, timeout):
        if not self._stopped:
            try:
                if self.connection.poll(timeout):
                    # The parent writes once, with a fixed bounded payload.
                    # EOF also stops admission after parent disappearance.
                    self.connection.recv_bytes(maxlength=1)
                    self._stopped = True
            except (EOFError, OSError):
                self._stopped = True
        return self._stopped


def _worker_main(config, stop_connection, ready_connection):
    """Spawn entry: attach only to the initialized owned DB; no stdout secrets."""
    database = objects = None
    watcher_stop = threading.Event()
    watcher = None
    succeeded = False
    stop_event = _StopReceiver(stop_connection)
    try:
        from local_server.cli import isolated_environment, restrict_outbound
        os.umask(0o077)
        isolated_environment()
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        restrict_outbound(int(config.endpoint.rsplit(":", 1)[1]), config.host, config.port)
        watcher = threading.Thread(target=_watch_parent, args=(config.parent_pid, watcher_stop), daemon=True)
        watcher.start()
        from local_server.database import connect_application
        # Attach has no initialization authority. Missing files/material must
        # fail rather than being re-created by a restarted worker.
        material = config.material
        database = connect_application(config.endpoint, material, initialize=False, operations_role="worker")
        runner, objects = build_local_worker(database, material, config.options, config.host, config.port,
                                             object_material=config.object_material)
        if database.ready() is not True or objects.ready() is not True:
            raise RuntimeUnavailable()
        ready_connection.send_bytes(b"ready")
        ready_connection.close()
        runner.run(stop_event, poll_interval=config.options.worker_poll_seconds)
        succeeded = stop_event.is_set()
    except BaseException:
        # In particular, a lease guard's fatal SystemExit is never success.
        succeeded = False
        try:
            ready_connection.send_bytes(b"failed")
        except BaseException:
            pass
    finally:
        try:
            ready_connection.close()
        except BaseException:
            succeeded = False
        for resource in (objects, database):
            if resource is not None:
                try:
                    resource.close()
                except BaseException:
                    succeeded = False
        try:
            stop_connection.close()
        except BaseException:
            succeeded = False
        watcher_stop.set()
        if watcher is not None:
            watcher.join(timeout=1)
    raise SystemExit(0 if succeeded else 1)


class OwnedWorker:
    """One spawn child, explicit startup acknowledgement and bounded shutdown."""
    def __init__(self, config, *, context=None):
        context = multiprocessing.get_context("spawn") if context is None else context
        self._reader, writer = context.Pipe(duplex=False)
        self._writer = writer
        self._stop_reader, self._stop_writer = context.Pipe(duplex=False)
        self.process = context.Process(target=_worker_main, args=(config, self._stop_reader, writer),
                                       name="arc-local-calculator", daemon=False)
        self._started = self._ready = self._closed = False

    def start(self, *, dependency_ready, timeout=30):
        if self._started or self._closed or type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise RuntimeUnavailable()
        try:
            self.process.start()
            self._started = True
            self._writer.close()
            self._stop_reader.close()
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if not self.process.is_alive() or dependency_ready() is not True:
                    raise RuntimeUnavailable()
                if self._reader.poll(min(0.1, max(0, deadline - time.monotonic()))):
                    if self._reader.recv_bytes(maxlength=16) != b"ready" or not self.process.is_alive():
                        raise RuntimeUnavailable()
                    self._ready = True
                    return
            raise RuntimeUnavailable()
        except BaseException as error:
            # start() can fail after creating the OS child. Its recorded PID is
            # still ours and must not be abandoned during failed startup.
            self._started = self._started or self.process.pid is not None
            self.close()
            if isinstance(error, KeyboardInterrupt):
                raise
            raise RuntimeUnavailable() from None

    def alive(self):
        return self._started and self._ready and not self._closed and self.process.is_alive()

    def close(self):
        if self._closed:
            return
        self._closed = True
        unsafe = False
        try:
            if self._started:
                try:
                    # One five-byte framed write to an otherwise empty pipe
                    # cannot wait for a child acknowledgement/shared lock.
                    # No worker has a writer endpoint or can fill this pipe.
                    self._stop_writer.send_bytes(b"s")
                except (BrokenPipeError, EOFError, OSError):
                    pass
                try:
                    self.process.join(timeout=3)
                except BaseException:
                    pass
                # A failed graceful join must not skip the stronger, owned
                # child shutdown attempts. A process may also exit between
                # the liveness check and a signal; verify it afterwards.
                for stop in (self.process.terminate, self.process.kill):
                    if not self.process.is_alive():
                        break
                    try:
                        stop()
                        self.process.join(timeout=2)
                    except BaseException:
                        pass
                if self.process.is_alive():
                    raise RuntimeUnavailable()
        except BaseException:
            unsafe = True
        finally:
            for connection in (self._reader, self._writer, self._stop_reader, self._stop_writer):
                try:
                    connection.close()
                except BaseException:
                    pass
            if not unsafe:
                try:
                    self.process.close()
                except BaseException:
                    unsafe = True
        if unsafe:
            raise RuntimeUnavailable() from None


class LocalRuntime:
    """Own API files and supervise HTTP/worker health; the CLI still owns DB."""
    def __init__(self, database, material, options, host, port, db_child):
        self.database, self.db_child, self.options = database, db_child, options
        self.service, self.objects, self.charts = build_api(database, material, options, host, port)
        self.worker = None
        self._closing = threading.Event()
        self._http_done = threading.Event()
        self._http_thread = None
        self.requires_process_exit = False

    def start_worker(self, material, endpoint, host, port):
        if self.worker is not None:
            raise RuntimeUnavailable()
        config = WorkerConfiguration(material, self.objects.material, endpoint, host, port, self.options, os.getpid())
        self.worker = OwnedWorker(config)
        self.worker.start(dependency_ready=lambda: self.db_child.poll() is None)

    def available(self):
        return (not self._closing.is_set() and self.db_child.poll() is None
                and self.worker is not None and self.worker.alive()
                and not self._http_done.is_set())

    def ready(self):
        try:
            return self.available() and self.database.ready() is True and self.objects.ready() is True
        except BaseException:
            return False

    def serve(self, server):
        if self._http_thread is not None or not self.available():
            raise RuntimeUnavailable()
        def run():
            try:
                server.run()
            except BaseException:
                pass
            finally:
                self._http_done.set()
        self._http_thread = threading.Thread(target=run, name="arc-local-http", daemon=True)
        self._http_thread.start()
        try:
            while self.available():
                self._closing.wait(0.1)
        finally:
            self._closing.set()
        raise RuntimeUnavailable()

    def close(self, server=None):
        self._closing.set()
        try:
            if server is not None:
                self._stop_http(server)
        finally:
            try:
                if self.worker is not None:
                    self.worker.close()
            except BaseException:
                self.requires_process_exit = True
                raise
            finally:
                # An undrained HTTP thread may hold the object's lock forever.
                # The CLI terminates this process after owned child cleanup;
                # do not wait on the same lock on this fatal-only path.
                if not self.requires_process_exit:
                    self.objects.close()

    def _stop_http(self, server):
        from waitress import wasyncore
        thread = self._http_thread
        listener_closed = threading.Event()
        def stop_listener():
            try:
                # Retain the trigger while closing only admission first.
                wasyncore.dispatcher.close(server)
            finally:
                listener_closed.set()
        try:
            if thread is not None and thread.is_alive():
                server.trigger.pull_trigger(stop_listener)
                if not listener_closed.wait(1):
                    self.requires_process_exit = True
            else:
                stop_listener()
            server.task_dispatcher.shutdown(timeout=5)
            if server.task_dispatcher.threads:
                self.requires_process_exit = True
        except BaseException:
            self.requires_process_exit = True
        finally:
            if thread is not None and thread.is_alive():
                try:
                    server.trigger.pull_trigger(lambda: wasyncore.close_all(map=server._map))
                    thread.join(timeout=3)
                except BaseException:
                    self.requires_process_exit = True
                if thread.is_alive():
                    self.requires_process_exit = True
            else:
                try:
                    wasyncore.close_all(map=server._map)
                except BaseException:
                    self.requires_process_exit = True

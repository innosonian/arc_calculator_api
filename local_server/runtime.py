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


from mock_journey.execution_definitions import PROJECTION_VERSION, execution_catalog
STORAGE_BUCKET = "arc-local-private"
STORAGE_DIRECTORY = "calculator_result/interpreted_rtdata/arc"
STORAGE_STAGE = "local"
_ERROR = "The owned local journey runtime is unavailable."


class RuntimeUnavailable(RuntimeError):
    def __init__(self):
        super().__init__(_ERROR)


@dataclass(frozen=True)
class LocalOptions:
    """Bounded, explicit local starting values; not ARC or deployment policy."""
    calculation_body_bytes: int = 1_000_000
    artifact_bytes: int = 8_000_000
    storage_quota_bytes: int = 1_073_741_824
    worker_lease_seconds: int = 60
    worker_retry_seconds: int = 5
    worker_poll_seconds: float = 0.25

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
        return 4 * ((self.calculation_body_bytes + 2) // 3)

    @property
    def response_body_limit(self):
        # Leave room for the submission overlay around a maximum stored result.
        from local_server.http import BODY_LIMIT
        return self.artifact_bytes + BODY_LIMIT


def _settings(database, material, options):
    from mock_journey.settings import ApiSettings, StateSettings, StorageSettings, WorkerSettings
    state = StateSettings(database.table_name, 4)
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


def build_api(database, material, options, host, port):
    from mock_journey.assembly import build_application

    api_settings, _ = _settings(database, material, options)
    objects, charts, legacy = _objects(material, options, host, port)
    try:
        service = build_application(
            api_settings, dynamodb_client=database.client, s3_client=objects, legacy_bindings=legacy,
            resume_keys={material.key_version: material.resume_key}, current_key_version=material.key_version,
            execution=execution_catalog(), operations=getattr(database, "operations", None),
        )
        return service, objects, charts
    except BaseException:
        objects.close()
        raise


def build_local_worker(database, material, options, host, port, *, object_material):
    from mock_journey.assembly import build_worker
    from mock_journey.contracts import PENDING_GOAL_ADAPTER_VERSION
    from mock_journey.internal_calculator import InternalCalculator
    from local_server.execution import LocalJobRunner
    from local_server.lease import LocalLeaseGuardFactory

    _, worker_settings = _settings(database, material, options)
    objects, charts, legacy = _objects(material, options, host, port, object_material=object_material)
    try:
        adapter = InternalCalculator(version=PENDING_GOAL_ADAPTER_VERSION,
                                     projection_version=PROJECTION_VERSION, stage=STORAGE_STAGE,
                                     allow_pending_cycle_goal=True)
        guard = LocalLeaseGuardFactory(
            lease_seconds=options.worker_lease_seconds,
            interval_seconds=options.worker_lease_seconds / 4,
            renewal_timeout_seconds=min(15, options.worker_lease_seconds / 4),
        )
        worker = build_worker(worker_settings, dynamodb_client=database.client, s3_client=objects,
                              legacy_bindings=legacy, adapters=[adapter],
                              required_bindings=execution_catalog().required_bindings,
                              lease_guard_factory=guard, operations=getattr(database, "operations", None))
        runner = LocalJobRunner(worker.jobs, worker, lease_seconds=options.worker_lease_seconds,
                                retry_seconds=options.worker_retry_seconds, page_size=20, max_pages=5)
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
        database = connect_application(config.endpoint, material, journey=True, initialize=False, operations_role="worker")
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

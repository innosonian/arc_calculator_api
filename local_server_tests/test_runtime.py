"""Owned runtime failure boundaries; real CLI/database acceptance is separate."""

from dataclasses import replace
import io
import json
from types import SimpleNamespace
import threading

import pytest

from local_server import cli, runtime
from local_server.runtime import LocalOptions, OwnedWorker, RuntimeUnavailable


@pytest.mark.parametrize("field,value", [
    ("calculation_body_bytes", True), ("calculation_body_bytes", 0),
    ("artifact_bytes", 999_999), ("storage_quota_bytes", -1),
    ("worker_lease_seconds", False), ("worker_retry_seconds", 0),
    ("worker_poll_seconds", True), ("worker_poll_seconds", float("inf")),
    ("worker_poll_seconds", float("nan")), ("worker_poll_seconds", 0),
])
def test_limits_reject_unbounded_or_type_coerced_configuration(field, value):
    with pytest.raises(ValueError, match="Invalid explicit"):
        replace(LocalOptions(), **{field: value})


def test_cli_default_is_journey_and_explicit_control_remains_available():
    args = cli.argument_parser().parse_args([])
    assert args.control_only is False
    limits = LocalOptions(args.calculation_body_bytes, args.artifact_bytes, args.storage_quota_bytes,
                          args.worker_lease_seconds, args.worker_retry_seconds, args.worker_poll_seconds)
    assert limits.payload_limit == 1_333_336
    assert limits.response_body_limit == 8_016_384
    assert args.course_v2 is False
    assert cli.argument_parser().parse_args(["--course-v2"]).course_v2 is True
    assert cli.argument_parser().parse_args(["--control-only"]).control_only is True
    assert cli.argument_parser().parse_args(["--artifact-bytes", "9000000"]).artifact_bytes == 9_000_000
    assert cli.main(["--course-v2", "--control-only"]) == 1


def test_all_fifteen_definitions_use_real_enums_and_explicit_pending_policy():
    from mock_journey.catalog import PROGRAMS, TARGETS, Catalog
    from mock_journey.contracts import PENDING_GOAL_ADAPTER_VERSION, PENDING_GOAL_PROFILE_VERSION
    from mock_journey import typed
    execution = runtime.execution_catalog()
    assert len(Catalog(execution).slot_keys) == 15
    assert execution.required_bindings == ((PENDING_GOAL_ADAPTER_VERSION, runtime.PROJECTION_VERSION),)
    for program, _, kind, amount in PROGRAMS:
        for target in TARGETS:
            value = execution.get_definition(program, target)
            condition = value["condition"]
            assert condition == {
                "mode": "training", "target": target, "guideline": "ARC2025",
                "training_type": {"compressions": "compression_only", "ventilations": "ventilation_only"}.get(kind, "cpr"),
                "cpr_cycle_type": "152" if target == "infant" else "302",
                "is_2rescuers": program in ("mock-two-rescuer-cpr", "mock-two-rescuer-aed"),
            }
            assert value["calculation_profile"] == {}
            assert value["profile_version"] == PENDING_GOAL_PROFILE_VERSION
            definition = typed.parse_json(Catalog(execution).definition(program, target))
            assert definition["goal"] == {"kind": kind, "required": amount}
            condition["target"] = "mutated"
            assert execution.get_definition(program, target)["condition"]["target"] == target


class _Pipe:
    def __init__(self, message=b"ready"):
        self.message, self.closed, self.sent = message, False, []

    def poll(self, timeout):
        return self.message is not None

    def recv_bytes(self, maxlength):
        assert maxlength == 16
        return self.message

    def send_bytes(self, value):
        self.sent.append(value)

    def close(self):
        self.closed = True


class _Process:
    def __init__(self, *, stop_at="graceful", start_error=None, join_error=False):
        self.stop_at, self.start_error, self.join_error = stop_at, start_error, join_error
        self.pid, self.live, self.events = None, False, []

    def start(self):
        self.pid, self.live = 43210, True
        self.events.append("start")
        if self.start_error:
            raise self.start_error

    def is_alive(self):
        return self.live

    def join(self, timeout):
        self.events.append(("join", timeout))
        if self.join_error:
            raise RuntimeError("private-join-marker")
        if self.stop_at == "graceful":
            self.live = False

    def terminate(self):
        self.events.append("terminate")
        if self.stop_at == "terminate":
            self.live = False

    def kill(self):
        self.events.append("kill")
        if self.stop_at == "kill":
            self.live = False

    def close(self):
        self.events.append("close")


class _Context:
    def __init__(self, process=None, message=b"ready"):
        self.process = process or _Process()
        self.reader, self.writer = _Pipe(message), _Pipe()
        self.stop_reader, self.stop_writer = _Pipe(None), _Pipe()
        self.pipe_calls = 0
        self.kwargs = None

    def Pipe(self, *, duplex):
        assert duplex is False
        self.pipe_calls += 1
        return (self.reader, self.writer) if self.pipe_calls == 1 else (self.stop_reader, self.stop_writer)

    def Process(self, **kwargs):
        self.kwargs = kwargs
        return self.process


def _owned(process=None, message=b"ready"):
    context = _Context(process, message)
    worker = OwnedWorker("private-config", context=context)
    return worker, context


def test_worker_is_not_ready_before_ack_and_spawn_uses_private_ipc():
    worker, context = _owned()
    assert not worker.alive()
    worker.start(dependency_ready=lambda: True)
    assert worker.alive() and context.writer.closed
    assert context.kwargs == {"target": runtime._worker_main,
                              "args": ("private-config", context.stop_reader, context.writer),
                              "name": "arc-local-calculator", "daemon": False}
    worker.close()
    assert not worker.alive() and context.stop_writer.sent == [b"s"] and context.reader.closed
    assert context.stop_reader.closed and context.stop_writer.closed
    before = list(context.process.events)
    worker.close()
    assert context.process.events == before


@pytest.mark.parametrize("message", [b"failed", b"READY", b"", b"private-exception-marker"])
def test_bad_startup_ack_closes_only_owned_child_and_hides_payload(message):
    worker, context = _owned(message=message)
    with pytest.raises(RuntimeUnavailable) as error:
        worker.start(dependency_ready=lambda: True)
    assert "private" not in str(error.value)
    assert not context.process.live and context.reader.closed and context.writer.closed


def test_startup_requires_owned_db_liveness_even_with_ack():
    worker, context = _owned()
    with pytest.raises(RuntimeUnavailable):
        worker.start(dependency_ready=lambda: False)
    assert not context.process.live


def test_startup_without_ack_has_bounded_wait(monkeypatch):
    worker, context = _owned(message=None)
    ticks = iter([0, 0, 0, 2])
    monkeypatch.setattr(runtime.time, "monotonic", lambda: next(ticks))
    with pytest.raises(RuntimeUnavailable):
        worker.start(dependency_ready=lambda: True, timeout=1)
    assert not context.process.live


@pytest.mark.parametrize("start_error", [RuntimeError("private-marker"), KeyboardInterrupt()])
def test_partial_spawn_failure_still_collects_child_and_preserves_interrupt(start_error):
    worker, context = _owned(_Process(start_error=start_error))
    expected = KeyboardInterrupt if isinstance(start_error, KeyboardInterrupt) else RuntimeUnavailable
    with pytest.raises(expected):
        worker.start(dependency_ready=lambda: True)
    assert not context.process.live and context.stop_writer.sent == [b"s"]


@pytest.mark.parametrize("stop_at,expected", [
    ("graceful", []), ("terminate", ["terminate"]), ("kill", ["terminate", "kill"]),
])
def test_shutdown_escalates_only_as_needed_to_owned_process(stop_at, expected):
    worker, context = _owned(_Process(stop_at=stop_at))
    worker.start(dependency_ready=lambda: True)
    worker.close()
    assert [item for item in context.process.events if item in ("terminate", "kill")] == expected
    assert not context.process.live


def test_graceful_join_error_does_not_abandon_owned_child():
    worker, context = _owned(_Process(stop_at="kill", join_error=True))
    worker.start(dependency_ready=lambda: True)
    worker.close()
    assert "terminate" in context.process.events and "kill" in context.process.events
    assert not context.process.live


def test_unconfirmed_child_death_is_failure_not_success():
    worker, context = _owned(_Process(stop_at="never"))
    worker.start(dependency_ready=lambda: True)
    with pytest.raises(RuntimeUnavailable):
        worker.close()
    assert context.process.live and "close" not in context.process.events
    assert context.reader.closed and context.writer.closed


def _runtime_shell():
    value = object.__new__(runtime.LocalRuntime)
    value._closing, value._http_done = threading.Event(), threading.Event()
    value._http_thread = None
    value.requires_process_exit = False
    value.db_child = SimpleNamespace(poll=lambda: None)
    value.database = SimpleNamespace(ready=lambda: True)
    value.objects = SimpleNamespace(ready=lambda: True, close=lambda: None)
    value.worker = SimpleNamespace(alive=lambda: True, close=lambda: None)
    return value


@pytest.mark.parametrize("broken", ["closing", "http", "worker", "db_process", "db_schema", "files"])
def test_readiness_reports_dependency_failures_truthfully(broken):
    value = _runtime_shell()
    assert value.ready() is True
    if broken == "closing":
        value._closing.set()
    elif broken == "http":
        value._http_done.set()
    elif broken == "worker":
        value.worker.alive = lambda: False
    elif broken == "db_process":
        value.db_child.poll = lambda: 1
    elif broken == "db_schema":
        value.database.ready = lambda: False
    else:
        value.objects.ready = lambda: False
    assert value.ready() is False


@pytest.mark.parametrize("unsafe_reason", ["http", "worker"])
def test_unsafe_shutdown_never_waits_on_shared_file_or_sdk_locks(monkeypatch, unsafe_reason):
    value, events = _runtime_shell(), []
    value.objects.close = lambda: pytest.fail("Would block forever on the HTTP-held file lock")
    def stop_http(server):
        events.append("http")
        value.requires_process_exit = unsafe_reason == "http"
    value._stop_http = stop_http
    def stop_worker():
        events.append("worker")
        if unsafe_reason == "worker":
            raise RuntimeUnavailable()
    value.worker.close = stop_worker
    db = SimpleNamespace(close=lambda: pytest.fail("Would block forever on the shared SDK lock"))
    child = object()
    monkeypatch.setattr(cli, "stop_database", lambda found: events.append("db-child") if found is child else pytest.fail("Foreign child"))
    def exit_self(code):
        events.append(("exit-self", code))
        raise SystemExit(code)
    monkeypatch.setattr(cli.os, "_exit", exit_self)
    with pytest.raises(SystemExit) as error:
        cli.close_journey_resources(value, object(), db, child)
    assert error.value.code == 1
    assert events == ["http", "worker", "db-child", ("exit-self", 1)]


def test_normal_shutdown_preserves_order_and_does_not_use_fatal_exit(monkeypatch):
    value, events = _runtime_shell(), []
    value._stop_http = lambda server: events.append("http")
    value.worker.close = lambda: events.append("worker")
    value.objects.close = lambda: events.append("objects")
    db = SimpleNamespace(close=lambda: events.append("db-client"))
    monkeypatch.setattr(cli, "stop_database", lambda child: events.append("db-child"))
    monkeypatch.setattr(cli.os, "_exit", lambda code: pytest.fail("Normal shutdown must not use fatal exit"))
    cli.close_journey_resources(value, object(), db, object())
    assert events == ["http", "worker", "objects", "db-client", "db-child"]


def test_fatal_exit_still_occurs_if_owned_db_cleanup_fails(monkeypatch):
    value = _runtime_shell()
    value.requires_process_exit = True
    value.close = lambda server: None
    def stop_database(child):
        raise RuntimeError("private-marker")
    monkeypatch.setattr(cli, "stop_database", stop_database)
    monkeypatch.setattr(cli.os, "_exit", lambda code: (_ for _ in ()).throw(SystemExit(code)))
    with pytest.raises(SystemExit) as error:
        cli.close_journey_resources(value, object(), object(), object())
    assert error.value.code == 1


def test_parent_loss_exits_only_the_worker_itself(monkeypatch):
    stop = SimpleNamespace(wait=lambda seconds: False)
    monkeypatch.setattr(runtime.os, "getppid", lambda: 999)
    monkeypatch.setattr(runtime.os, "_exit", lambda code: (_ for _ in ()).throw(SystemExit(code)))
    with pytest.raises(SystemExit) as error:
        runtime._watch_parent(123, stop)
    assert error.value.code == 1


def test_stop_pipe_handles_message_and_eof_without_shared_locks():
    import multiprocessing
    for send in (True, False):
        reader, writer = multiprocessing.Pipe(duplex=False)
        receiver = runtime._StopReceiver(reader)
        try:
            assert receiver.is_set() is False
            if send:
                writer.send_bytes(b"s")
            writer.close()
            assert receiver.wait(0.1) is True
            # No second read or peer acknowledgement is needed.
            reader.close()
            assert receiver.is_set() is True
        finally:
            reader.close()
            writer.close()


def test_dead_child_broken_stop_pipe_does_not_skip_reaping():
    worker, context = _owned()
    worker.start(dependency_ready=lambda: True)
    context.process.live = False
    def dead_peer(value):
        raise BrokenPipeError()
    context.stop_writer.send_bytes = dead_peer
    worker.close()
    assert ("join", 3) in context.process.events
    assert "close" in context.process.events


@pytest.mark.parametrize("outcome", ["stopped", "unexpected_return", "lease_fatal", "db_unready", "files_unready"])
def test_child_has_only_readonly_attach_and_fixed_terminal_status(monkeypatch, outcome):
    from local_server import database, object_storage
    events, threads = [], []
    secret = "private-key-marker"
    config = runtime.WorkerConfiguration(secret, secret, "http://127.0.0.1:8001", "127.0.0.1", 8000,
                                         LocalOptions(), 123)
    assert secret not in repr(config)
    monkeypatch.setattr(runtime.os, "umask", lambda mask: None)
    monkeypatch.setattr(runtime.signal, "signal", lambda *args: None)
    monkeypatch.setattr(cli, "isolated_environment", lambda: events.append("isolate"))
    monkeypatch.setattr(cli, "restrict_outbound", lambda *args: events.append("restrict"))
    monkeypatch.setattr(database, "prepare_material", lambda *a, **k: pytest.fail("Child cannot initialize keys"))
    monkeypatch.setattr(object_storage, "prepare_object_material", lambda *a, **k: pytest.fail("Child cannot initialize chart keys"))
    class Watcher:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            threads.append(self)
        def start(self):
            events.append("watch-parent")
        def join(self, timeout):
            assert self.kwargs["args"][1].is_set()
            events.append("watcher-joined")
    monkeypatch.setattr(runtime.threading, "Thread", Watcher)
    def close(name):
        # Parent loss must still terminate this child during resource cleanup.
        assert not threads[0].kwargs["args"][1].is_set()
        events.append(name)
    db = SimpleNamespace(ready=lambda: outcome != "db_unready", close=lambda: close("db-close"))
    objects = SimpleNamespace(ready=lambda: outcome != "files_unready", close=lambda: close("objects-close"))
    def attach(endpoint, material, **kwargs):
        assert endpoint == config.endpoint and material is config.material
        assert kwargs == {"journey": True, "initialize": False, "operations_role": "worker"}
        events.append("readonly-attach")
        return db
    monkeypatch.setattr(database, "connect_application", attach)
    def run(stop, *, poll_interval):
        assert poll_interval == 0.25
        events.append("runner")
        if outcome == "lease_fatal":
            raise SystemExit(1)
        if outcome == "stopped":
            assert stop.wait(0) is True
    def build(found, material, options, host, port, *, object_material):
        assert found is db and object_material is config.object_material
        return SimpleNamespace(run=run), objects
    monkeypatch.setattr(runtime, "build_local_worker", build)
    ready = _Pipe()
    stop = SimpleNamespace(poll=lambda timeout: outcome == "stopped",
                           recv_bytes=lambda maxlength: b"s", close=lambda: events.append("stop-close"))
    with pytest.raises(SystemExit) as error:
        runtime._worker_main(config, stop, ready)
    assert error.value.code == (0 if outcome == "stopped" else 1)
    assert events[:3] == ["isolate", "restrict", "watch-parent"]
    assert events[-4:] == ["objects-close", "db-close", "stop-close", "watcher-joined"]
    assert b"ready" in ready.sent if outcome not in ("db_unready", "files_unready") else b"ready" not in ready.sent
    assert all(message in (b"ready", b"failed") for message in ready.sent)


@pytest.mark.parametrize("failure", [None, "requests", "loop"])
def test_http_shutdown_stops_admission_then_drains_before_closing_loop(monkeypatch, failure):
    from waitress import wasyncore
    events = []
    class HttpThread:
        live = True
        def is_alive(self):
            return self.live
        def join(self, timeout):
            events.append("join-loop")
            self.live = failure == "loop"
    thread = HttpThread()
    value = _runtime_shell()
    value._http_thread = thread
    dispatcher = SimpleNamespace(threads={1} if failure == "requests" else set(),
                                 shutdown=lambda timeout: events.append("drain-requests"))
    server = SimpleNamespace(task_dispatcher=dispatcher, _map={})
    server.trigger = SimpleNamespace(pull_trigger=lambda callback: callback())
    monkeypatch.setattr(wasyncore.dispatcher, "close", lambda found: events.append("close-admission"))
    monkeypatch.setattr(wasyncore, "close_all", lambda **kwargs: events.append("close-loop"))
    value._stop_http(server)
    assert events == ["close-admission", "drain-requests", "close-loop", "join-loop"]
    assert value.requires_process_exit is (failure is not None)


def test_supervised_http_reports_real_readiness_and_stops_acceptance(tmp_path, monkeypatch, capsys):
    from local_server import http
    from local_server.charts import LocalChartService
    from local_server.database import prepare_material
    from local_server.object_storage import LocalObjectClient, prepare_object_material
    with cli.installation_lock(tmp_path / "installation") as directory:
        material = prepare_material(directory)
        objects = LocalObjectClient(prepare_object_material(material), bucket=runtime.STORAGE_BUCKET,
                                    directory=runtime.STORAGE_DIRECTORY, stage="local",
                                    artifact_limit=100_000, quota_bytes=1_000_000)
        try:
            chart = LocalChartService(objects, base_url="http://127.0.0.1:8000")
            available, database_ready = [True], [True]
            def execution_ready():
                if isinstance(available[0], Exception):
                    raise available[0]
                return available[0]
            service = SimpleNamespace(calculation=SimpleNamespace(payload_limit=4 * ((1000 + 2) // 3)))
            app = http.make_application(service, lambda: database_ready[0], "127.0.0.1", 8000,
                                        ["127.0.0.1"], calculation_body_limit=1000, chart_service=chart,
                                        response_body_limit=116_384, execution_ready=execution_ready)
            def call(path="/healthz", method="GET", *, extra=None):
                environ = {"REMOTE_ADDR": "127.0.0.1", "HTTP_HOST": "127.0.0.1:8000",
                           "REQUEST_URI": path, "PATH_INFO": path, "REQUEST_METHOD": method,
                           "wsgi.input": io.BytesIO(), "CONTENT_TYPE": "application/json"}
                environ.update(extra or {})
                status = []
                body = b"".join(app(environ, lambda value, headers: status.append(value)))
                return status[0], json.loads(body)
            monkeypatch.setattr(http, "handle", lambda *args: pytest.fail("Unavailable runtime must never dispatch"))
            status, body = call()
            assert status == "200 OK" and body["calculator_available"] is True
            assert body["mode"] == "local_journey" and body["program_target_combinations"] == 15
            assert body["completion_policy"]["cycles"] == "pending_policy"
            database_ready[0] = False
            assert call()[0] == "503 Service Unavailable"
            database_ready[0] = True
            for failed in (False, None, "true", RuntimeError("private-marker")):
                available[0] = failed
                for path, method in (("/", "GET"), ("/cpr-analysis", "POST"), ("/mock/v1/sessions", "POST")):
                    status, body = call(path, method)
                    assert status == "503 Service Unavailable"
                    assert body["error"]["code"] == "TEMPORARILY_UNAVAILABLE"
                    assert "private-marker" not in json.dumps(body)
            # Common direct-peer/Host protections still run before the gate.
            assert call(extra={"REMOTE_ADDR": "203.0.113.5"})[0] == "404 Not Found"
            assert call(extra={"HTTP_HOST": "attacker.example"})[0] == "400 Bad Request"
            assert "private-marker" not in "".join(capsys.readouterr())
        finally:
            objects.close()

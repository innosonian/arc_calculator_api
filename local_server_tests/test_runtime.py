"""Owned runtime failure boundaries; real CLI/database acceptance is separate."""

from dataclasses import replace
import io
import json
from types import SimpleNamespace
import signal
import threading
import time

import pytest

from local_server import cli, runtime
from local_server.runtime import LocalOptions, OwnedWorker, RuntimeUnavailable
from local_server_tests.course_stub import StubCourseService, calculation_path


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


def test_cli_defaults_are_the_v2_journey_limits_and_removed_modes_are_unknown():
    args = cli.argument_parser().parse_args([])
    limits = LocalOptions(args.calculation_body_bytes, args.artifact_bytes, args.storage_quota_bytes,
                          args.worker_lease_seconds, args.worker_retry_seconds, args.worker_poll_seconds)
    assert limits.payload_limit == 1_333_336
    assert limits.response_body_limit == 8_016_384
    assert not hasattr(args, "course_v2") and not hasattr(args, "control_only")
    assert cli.argument_parser().parse_args(["--artifact-bytes", "9000000"]).artifact_bytes == 9_000_000
    usage = cli.argument_parser().format_help()
    assert "--course-v2" not in usage and "--control-only" not in usage
    assert "/api/v2" in usage and "/mock/v1" not in usage


@pytest.mark.parametrize("argv", [["--control-only"], ["--course-v2"], ["--control-only", "--course-v2"],
                                  ["--course-v2", "--port", "80"]])
def test_removed_flags_are_argparse_errors_before_isolation_or_any_file_or_port(monkeypatch, capsys, tmp_path, argv):
    # D123: the removed modes are unknown arguments; argparse exits with its
    # usage error (status 2) before main() isolates, locks or binds anything.
    for name in ("isolated_environment", "ensure_port_free", "verify_distribution", "installation_lock"):
        monkeypatch.setattr(cli, name, lambda *a, **k: pytest.fail("Removed flag reached startup."))
    monkeypatch.setattr(cli.signal, "signal", lambda *a: pytest.fail("Removed flag installed handlers."))
    with pytest.raises(SystemExit) as exit_info:
        cli.main(argv + ["--data-dir", str(tmp_path / "never")])
    assert exit_info.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "unrecognized arguments" in captured.err and argv[0] in captured.err
    assert not (tmp_path / "never").exists()
    # The accepted argument set still starts normally (port 80 is refused by validation, status 1).
    monkeypatch.setattr(cli, "isolated_environment", lambda: None)
    monkeypatch.setattr(cli.signal, "signal", lambda *a: None)
    assert cli.main(["--port", "80", "--data-dir", str(tmp_path / "never")]) == 1
    assert capsys.readouterr().err.strip() == (
        "Local server could not start: API and DB ports must be different numbers between 1024 and 65535.")


def test_ready_banner_names_v2_dummy_courses_cycle_rule_and_disabled_submission():
    text = "\n".join(cli.READY_BANNER)
    assert "/api/v2" in text and "15 temporary Dummy courses" in text
    assert "CPR completion follows the cycle rule (D136)" in text and "ARC submission remains disabled" in text
    assert "/mock/v1" not in text and "control" not in text.lower()


def test_local_course_settings_are_the_former_course_v2_values_defined_in_product_code():
    from mock_journey.course_settings import CourseSettings, fixture_course_settings
    from local_server_tests.test_local_module_boundaries import loaded_after
    settings = runtime.local_course_settings()
    assert type(settings) is CourseSettings
    # Same values the former --course-v2 option passed (fixture_course_settings()).
    assert settings == fixture_course_settings()
    assert runtime.LOCAL_COURSE_LIMITS == {
        "max_course_items": 64, "max_assignments": 100, "max_bundle_bytes": 262144,
        "max_control_body_bytes": 16384, "max_intervals_per_report": 128,
        "max_merged_intervals_per_start": 512, "max_reports_per_start": 4096,
        "max_transaction_actions": 20, "max_conflict_retries": 4,
    }
    # The former --course-v2 path read tests/fixtures/vcc_contract through the
    # synthetic catalog module; importing the runtime must not load it.
    loaded = loaded_after("local_server.runtime")
    assert "mock_journey.course_fixture" not in loaded
    assert "mock_journey.course_settings" in loaded and "mock_journey.dev_course" not in loaded


class _NoClientCalls:
    def __getattr__(self, name):
        raise AssertionError("Local assembly made an SDK call: " + name)


def _database(table="arc_local_assembly_test"):
    return SimpleNamespace(client=_NoClientCalls(), table_name=table, operations=None)


def _material(tmp_path):
    from local_server.database import prepare_material
    root = tmp_path / "installation"
    root.mkdir(mode=0o700)
    return prepare_material(root)


def test_local_api_is_the_dummy_dev_course_application_assembled_like_aws(tmp_path):
    from mock_journey.course_wiring import CourseApplication
    from mock_journey.dev_course import CATALOG_VERSION, DummyDevCourseProvider
    material, options = _material(tmp_path), LocalOptions()
    service, objects, charts = runtime.build_api(_database(), material, options, "127.0.0.1", 8000)
    try:
        assert type(service) is CourseApplication and service.course_mode == "course_v2"
        # AWS Dev assembly: the provider itself, no DummyLearnerBinding wrapper.
        assert type(service.provider) is DummyDevCourseProvider
        assignments = service.provider.list_assignments(service.provider.learner)
        assert [binding.public_ids.course_id for binding in assignments] == list(range(910001, 910016))
        assert service.provider.mapping_document["mapping_version"] == CATALOG_VERSION
        assert len(service.provider.mapping_document["mappings"]) == 30
        assert service.calculation.payload_limit == options.payload_limit
        assert service.state.table_name == "arc_local_assembly_test"
        assert charts.base_url == "http://127.0.0.1:8000" and objects.ready() is True
    finally:
        objects.close()


def test_local_api_validates_catalog_capacity_before_opening_private_files(tmp_path, monkeypatch):
    material = _material(tmp_path)
    monkeypatch.setattr(runtime, "_objects", lambda *a, **k: pytest.fail("Files opened before validation."))
    small = LocalOptions(calculation_body_bytes=100, artifact_bytes=100)
    with pytest.raises(ValueError, match="Dummy Dev catalog"):
        runtime.build_api(_database(), material, small, "127.0.0.1", 8000)
    monkeypatch.setattr(runtime, "LOCAL_COURSE_LIMITS", {**runtime.LOCAL_COURSE_LIMITS, "max_transaction_actions": 7})
    with pytest.raises(ValueError, match="Dummy Dev catalog"):
        runtime.build_api(_database(), material, LocalOptions(), "127.0.0.1", 8000)


def test_local_api_rejects_invalid_key_material_without_echo_and_closes_files(tmp_path, monkeypatch):
    from dataclasses import replace as changed
    material = _material(tmp_path)
    closed = []
    original = runtime._objects

    def tracked(*args, **kwargs):
        objects, charts, legacy = original(*args, **kwargs)
        close = objects.close
        objects.close = lambda: closed.append(True) or close()
        return objects, charts, legacy

    monkeypatch.setattr(runtime, "_objects", tracked)
    with pytest.raises(ValueError) as error:
        runtime.build_api(_database(), changed(material, resume_key=b"secret-marker"), LocalOptions(),
                          "127.0.0.1", 8000)
    assert "secret-marker" not in str(error.value) and closed == [True]


def test_local_api_and_worker_share_one_state_and_storage_scope_and_retain_adapters(tmp_path):
    from local_server.execution import LocalJobRunner
    from local_server.lease import LocalLeaseGuardFactory
    from mock_journey.contracts import (
        CURRENT_ADAPTER_VERSION, CYCLE_GOAL_ADAPTER_VERSION, PENDING_GOAL_ADAPTER_VERSION,
        RETAINED_PENDING_GOAL_ADAPTER_VERSION,
    )
    from mock_journey.cycle_goal import closed_cycle_count
    from mock_journey.internal_calculator import InternalCalculator
    material, options = _material(tmp_path), LocalOptions(worker_lease_seconds=40)
    database = _database()
    service, api_objects, _ = runtime.build_api(database, material, options, "127.0.0.1", 8000)
    runner, worker_objects = runtime.build_local_worker(database, material, options, "127.0.0.1", 8000,
                                                        object_material=api_objects.material)
    try:
        worker = runner._relay.sender.worker
        assert type(runner) is LocalJobRunner and worker_objects is not api_objects
        assert service.state.client is worker.jobs.state.client is database.client
        assert service.state.table_name == worker.jobs.state.table_name
        assert service.calculation.storage.bucket == worker.storage.bucket == runtime.STORAGE_BUCKET
        assert service.calculation.storage.prefix == worker.storage.prefix
        assert not hasattr(worker, "auth")
        # The removed explicit factory rejected state/storage mismatches. Both
        # roles now take their scope from one settings pair, never two inputs.
        api_settings, worker_settings = runtime._settings(database, material, options)
        assert api_settings.state is worker_settings.state and api_settings.storage is worker_settings.storage
        assert worker_settings.storage.stage == runtime.STORAGE_STAGE
        # ... and accepted only internal calculators of the storage stage: the
        # current adapter and the retained v4 (D138) bound to the D136
        # closed-cycle resolver, the retained pending adapters without one
        # (their cycles goal stays pending).
        registered = worker.adapters._registered
        assert set(registered) == {(CURRENT_ADAPTER_VERSION, runtime.PROJECTION_VERSION),
                                   (CYCLE_GOAL_ADAPTER_VERSION, runtime.PROJECTION_VERSION),
                                   (PENDING_GOAL_ADAPTER_VERSION, runtime.PROJECTION_VERSION),
                                   (RETAINED_PENDING_GOAL_ADAPTER_VERSION, runtime.PROJECTION_VERSION)}
        for (version, projection), adapter in registered.items():
            assert type(adapter) is InternalCalculator and adapter.version == version
            assert adapter.stage == worker.storage.stage == runtime.STORAGE_STAGE
            if version in (CURRENT_ADAPTER_VERSION, CYCLE_GOAL_ADAPTER_VERSION):
                assert adapter.cycle_goal_resolver is closed_cycle_count and adapter.allow_pending_cycle_goal is False
            else:
                assert adapter.cycle_goal_resolver is None and adapter.allow_pending_cycle_goal is True
            # D138/D139: only the current adapter runs with the current options.
            assert (adapter.calculation_options.eof_single_confirmation,
                    adapter.calculation_options.minimum_quantity_null) == (
                (True, False) if version == CURRENT_ADAPTER_VERSION else (False, True))
            assert worker.adapters.resolve(version, projection) is adapter
        for version, projection in runtime.execution_catalog().required_bindings:
            assert worker.adapters.resolve(version, projection) is registered[(version, projection)]
        # The explicit lease guard factory is passed, not started.
        assert type(worker.lease_guard_factory) is LocalLeaseGuardFactory
        assert worker.lease_guard_factory.lease_seconds == 40
        assert worker.lease_seconds == 40 and worker.retry_seconds == options.worker_retry_seconds
    finally:
        worker_objects.close()
        api_objects.close()


def test_all_fifteen_definitions_use_real_enums_and_the_cycle_goal_profile():
    from mock_journey.catalog import PROGRAMS, TARGETS, Catalog
    from mock_journey.contracts import CURRENT_ADAPTER_VERSION, CURRENT_PROFILE_VERSION
    assert (CURRENT_ADAPTER_VERSION, CURRENT_PROFILE_VERSION) == ("arc-internal-detection-v5", "tester-goal-cycles-v2")
    from mock_journey import typed
    execution = runtime.execution_catalog()
    assert len(PROGRAMS) * len(TARGETS) == 15
    assert execution.required_bindings == ((CURRENT_ADAPTER_VERSION, runtime.PROJECTION_VERSION),)
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
            assert value["profile_version"] == CURRENT_PROFILE_VERSION
            assert value["adapter_version"] == CURRENT_ADAPTER_VERSION
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
        assert kwargs == {"initialize": False, "operations_role": "worker"}
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
            service = StubCourseService(payload_limit=4 * ((1000 + 2) // 3))
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
            assert service.dispatched == []
            status, body = call()
            assert status == "200 OK" and body["calculator_available"] is True
            # Same keys and order as the former --course-v2 health body.
            assert list(body) == ["service", "mode", "calculator_available", "login_path", "programs_path",
                                  "calculation_transport_configured", "program_target_combinations",
                                  "completion_policy"]
            assert body["mode"] == "course_v2" and body["program_target_combinations"] == 15
            assert body["login_path"] == "/api/v2/sessions/" and body["programs_path"] == "/api/v2/courses/progress/"
            assert body["completion_policy"] == {"cycles": "evaluated", "compressions": "evaluated",
                                                 "ventilations": "evaluated"}
            database_ready[0] = False
            assert call()[0] == "503 Service Unavailable"
            database_ready[0] = True
            for failed in (False, None, "true", RuntimeError("private-marker")):
                available[0] = failed
                for path, method in (("/", "GET"), (calculation_path(), "POST"), ("/api/v2/sessions/", "POST")):
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


@pytest.mark.parametrize("failure", ["db_process", "db_schema"])
def test_dead_owned_database_refuses_health_progress_and_login_with_503(tmp_path, capsys, failure):
    """The definite 503 contract of a dead owned DB, at the real WSGI gate.

    The live CLI test can only observe it briefly: serve() then stops within
    one 0.1-second poll and the CLI exits with a visible failure (checked here
    too). The real LocalRuntime.ready/available functions are wired exactly as
    the CLI wires them.
    """
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
            owned = _runtime_shell()
            service = StubCourseService(payload_limit=4 * ((1000 + 2) // 3))
            app = http.make_application(service, owned.ready, "127.0.0.1", 8000, ["127.0.0.1"],
                                        calculation_body_limit=1000, chart_service=chart,
                                        response_body_limit=116_384, execution_ready=owned.available)

            def call(method, path, query="", body=b""):
                environ = {"REMOTE_ADDR": "127.0.0.1", "HTTP_HOST": "127.0.0.1:8000",
                           "REQUEST_URI": path + ("?" + query if query else ""), "PATH_INFO": path,
                           "QUERY_STRING": query, "REQUEST_METHOD": method,
                           "CONTENT_LENGTH": str(len(body)), "wsgi.input": io.BytesIO(body),
                           "CONTENT_TYPE": "application/json"}
                status = []
                text = b"".join(app(environ, lambda value, headers: status.append(value)))
                return status[0], json.loads(text)

            login = json.dumps({"loginId": "test@test.com", "password": "2222"}).encode()
            requests = (("GET", "/healthz", "", b""),
                        ("GET", "/api/v2/courses/progress/", "page=1&pageSize=100", b""),
                        ("POST", "/api/v2/sessions/", "", login),
                        ("POST", calculation_path(), "", b"x"))
            assert call("GET", "/healthz")[0] == "200 OK"
            assert call("POST", "/api/v2/sessions/", body=login)[0] == "201 Created"
            service.dispatched.clear()
            service.calls.clear()
            # The supervising serve loop is running when the DB dies.
            stopped, outcome = threading.Event(), []
            server = SimpleNamespace(run=lambda: stopped.wait(10))

            def serve():
                try:
                    owned.serve(server)
                except BaseException as raised:
                    outcome.append(raised)
            serving = threading.Thread(target=serve, daemon=True)
            serving.start()
            if failure == "db_process":
                died = time.monotonic()
                owned.db_child.poll = lambda: -signal.SIGTERM
            else:
                # The process is alive but the attached table is not ready.
                owned.database.ready = lambda: False
            for method, path, query, body in requests:
                status, value = call(method, path, query, body)
                if failure == "db_schema" and path != "/healthz":
                    # Only /healthz reports DB readiness; routes still run.
                    continue
                assert status == "503 Service Unavailable"
                assert value["error"]["code"] == "TEMPORARILY_UNAVAILABLE"
                assert "2222" not in json.dumps(value)
            if failure == "db_process":
                assert service.dispatched == [] and service.calls == []
                # serve() stops within its 0.1-second poll and reports the
                # failure, so the CLI closes and exits non-zero.
                serving.join(timeout=2)
                assert not serving.is_alive() and time.monotonic() - died < 2
                assert [type(value) for value in outcome] == [RuntimeUnavailable]
                assert owned._closing.is_set() and owned.available() is False
            else:
                owned._closing.set()
                serving.join(timeout=2)
                assert [type(value) for value in outcome] == [RuntimeUnavailable]
            stopped.set()
            assert "2222" not in "".join(capsys.readouterr())
        finally:
            objects.close()

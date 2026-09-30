"""Independent S5 lifecycle fault injection; no server or network is started."""

from contextlib import contextmanager
import os
from types import SimpleNamespace

import pytest

from local_server import cli, database, http, runtime


@pytest.mark.parametrize("failure", ["serve", "runtime", "database", "runtime+database"])
def test_cleanup_failure_still_stops_owned_child_before_unlock(monkeypatch, tmp_path, failure):
    """One failed closer cannot release the installation with its DB running."""
    events = []
    locked = False
    owned_child = object()

    @contextmanager
    def lock(path):
        nonlocal locked
        locked = True
        events.append("lock")
        try:
            yield path
        finally:
            locked = False
            events.append("unlock")

    def action(name):
        def run(*args, **kwargs):
            events.append(name)
            if name in failure.split("+"):
                raise RuntimeError("PRIVATE-CLEANUP-MARKER")
        return run

    def stop(child):
        assert child is owned_child
        assert locked, "Owned DB must be stopped before installation lock release."
        events.append("child")

    class Runtime:
        """The CLI's owned-runtime seam; real shutdown ordering is in test_runtime.py."""
        requires_process_exit = False

        def __init__(self, db, material, options, host, port, child):
            assert child is owned_child and db is connected
            self.service, self.charts = object(), object()
            events.append("runtime-built")

        def start_worker(self, material, endpoint, host, port):
            events.append("worker-started")

        def ready(self):
            return True

        def available(self):
            return True

        def serve(self, server):
            action("serve")(server)
            raise KeyboardInterrupt

        def close(self, server):
            action("runtime")(server)

    connected = SimpleNamespace(ready=lambda: True, close=action("database"))
    server = object()
    monkeypatch.setattr(cli, "isolated_environment", lambda: None)
    monkeypatch.setattr(cli, "restrict_outbound", lambda *args: None)
    monkeypatch.setattr(cli, "ensure_port_free", lambda *args: None)
    monkeypatch.setattr(cli, "verify_distribution", lambda home: home)
    monkeypatch.setattr(cli, "installation_lock", lock)
    monkeypatch.setattr(cli, "start_database", lambda *args: owned_child)
    monkeypatch.setattr(cli, "stop_database", stop)
    monkeypatch.setattr(cli.signal, "signal", lambda *args: None)
    monkeypatch.setattr(cli.os, "_exit", lambda code: pytest.fail("Normal cleanup must not use fatal exit."))
    monkeypatch.setattr(database, "prepare_material", lambda path, **kwargs: SimpleNamespace(db_dir=path / "dynamodb"))

    def connect(endpoint, material, *, operations_role):
        # The only local mode: the (journey-schema) installation and the API log role.
        assert operations_role == "api"
        events.append("connect")
        return connected

    monkeypatch.setattr(database, "connect_application", connect)
    monkeypatch.setattr(runtime, "LocalRuntime", Runtime)
    monkeypatch.setattr(http, "make_application", lambda *args, **kwargs: object())
    monkeypatch.setattr(http, "create_server", lambda *args: server)

    previous_umask = os.umask(0o077)
    try:
        try:
            result = cli.main(["--data-dir", str(tmp_path / "local")])
        except Exception:
            result = None
        assert events[:4] == ["lock", "connect", "runtime-built", "worker-started"]
        assert "child" in events, "A failed resource closer leaked the owned DB child."
        assert events.index("child") < events.index("unlock")
        assert all(name in events for name in ("serve", "runtime", "database"))
        assert events.index("runtime") < events.index("database") < events.index("child")
        assert result in (0, 1), "CLI cleanup must not expose its internal exception."
    finally:
        os.umask(previous_umask)


@pytest.mark.parametrize("failure", ["runtime_build", "runtime_build+database"])
def test_failure_before_the_runtime_stops_owned_child_before_unlock_without_a_listener(
        monkeypatch, tmp_path, capsys, failure):
    """Before LocalRuntime exists there is no HTTP listener to close.

    Listener shutdown failures (dispatcher, channels) are LocalRuntime._stop_http
    cases in test_runtime.py; here only the DB client and owned child remain.
    """
    events = []
    locked = False
    owned_child = object()

    @contextmanager
    def lock(path):
        nonlocal locked
        locked = True
        events.append("lock")
        try:
            yield path
        finally:
            locked = False
            events.append("unlock")

    def stop(child):
        assert child is owned_child and locked
        events.append("child")

    def close():
        events.append("database")
        if "database" in failure:
            raise RuntimeError("PRIVATE-CLEANUP-MARKER")

    class Runtime:
        def __init__(self, *args):
            events.append("runtime-build")
            raise RuntimeError("PRIVATE-CLEANUP-MARKER")

    monkeypatch.setattr(cli, "isolated_environment", lambda: None)
    monkeypatch.setattr(cli, "restrict_outbound", lambda *args: None)
    monkeypatch.setattr(cli, "ensure_port_free", lambda *args: None)
    monkeypatch.setattr(cli, "verify_distribution", lambda home: home)
    monkeypatch.setattr(cli, "installation_lock", lock)
    monkeypatch.setattr(cli, "start_database", lambda *args: owned_child)
    monkeypatch.setattr(cli, "stop_database", stop)
    monkeypatch.setattr(cli.signal, "signal", lambda *args: None)
    monkeypatch.setattr(cli.os, "_exit", lambda code: pytest.fail("Normal cleanup must not use fatal exit."))
    monkeypatch.setattr(database, "prepare_material", lambda path, **kwargs: SimpleNamespace(db_dir=path / "dynamodb"))
    monkeypatch.setattr(database, "connect_application",
                        lambda *args, **kwargs: SimpleNamespace(ready=lambda: True, close=close))
    monkeypatch.setattr(runtime, "LocalRuntime", Runtime)
    monkeypatch.setattr(http, "make_application", lambda *a, **k: pytest.fail("No listener before the runtime."))
    monkeypatch.setattr(http, "create_server", lambda *a, **k: pytest.fail("No listener before the runtime."))

    previous_umask = os.umask(0o077)
    try:
        assert cli.main(["--data-dir", str(tmp_path / "local")]) == 1
        assert events == ["lock", "runtime-build", "database", "child", "unlock"]
        captured = capsys.readouterr()
        assert "prepare the local calculation runtime" in captured.err
        assert "PRIVATE-CLEANUP-MARKER" not in captured.out + captured.err
    finally:
        os.umask(previous_umask)

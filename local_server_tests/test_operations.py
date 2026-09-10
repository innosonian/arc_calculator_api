"""Independent S5 lifecycle fault injection; no server or network is started."""

from contextlib import contextmanager
import os
from types import SimpleNamespace

import pytest

from local_server import cli, database, http


@pytest.mark.parametrize("failure", ["dispatcher", "channels", "database"])
def test_cleanup_failure_still_stops_owned_child_before_unlock(monkeypatch, tmp_path, failure):
    """One failed closer cannot release the installation with its DB running."""
    from waitress import wasyncore

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

    def close(name):
        def action(*args, **kwargs):
            events.append(name)
            if name == failure:
                raise RuntimeError("PRIVATE-CLEANUP-MARKER")
        return action

    def stop(child):
        assert child is owned_child
        assert locked, "Owned DB must be stopped before installation lock release."
        events.append("child")

    db = SimpleNamespace(application=object(), ready=lambda: True, close=close("database"))
    server = SimpleNamespace(
        task_dispatcher=SimpleNamespace(shutdown=close("dispatcher")), _map={}, run=lambda: None,
    )
    monkeypatch.setattr(cli, "isolated_environment", lambda: None)
    monkeypatch.setattr(cli, "restrict_outbound", lambda *args: None)
    monkeypatch.setattr(cli, "ensure_port_free", lambda *args: None)
    monkeypatch.setattr(cli, "verify_distribution", lambda home: home)
    monkeypatch.setattr(cli, "installation_lock", lock)
    monkeypatch.setattr(cli, "start_database", lambda *args: owned_child)
    monkeypatch.setattr(cli, "stop_database", stop)
    monkeypatch.setattr(cli.signal, "signal", lambda *args: None)
    monkeypatch.setattr(database, "prepare_material", lambda path: SimpleNamespace(db_dir=path / "dynamodb"))
    def connect(*args, operations_role):
        assert operations_role == "api"
        return db
    monkeypatch.setattr(database, "connect_application", connect)
    monkeypatch.setattr(http, "make_application", lambda *args: object())
    monkeypatch.setattr(http, "create_server", lambda *args: server)
    monkeypatch.setattr(wasyncore, "close_all", close("channels"))

    previous_umask = os.umask(0o077)
    try:
        try:
            result = cli.main(["--control-only", "--data-dir", str(tmp_path / "local")])
        except Exception:
            result = None
        assert "child" in events, "A failed resource closer leaked the owned DB child."
        assert events.index("child") < events.index("unlock")
        assert all(name in events for name in ("dispatcher", "channels", "database"))
        assert result in (0, 1), "CLI cleanup must not expose its internal exception."
    finally:
        os.umask(previous_umask)

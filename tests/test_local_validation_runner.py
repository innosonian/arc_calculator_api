"""The integration runner must select the same tools for real CLI children."""
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

from local_server import cli
from scripts import validate_local_integration as runner


def test_explicit_tools_reach_live_harness_without_default_installation(tmp_path, monkeypatch):
    # Import the actual consumer in a checkout with no var/ installation. This
    # catches mismatched variable names, not just assignments in the launcher.
    root = Path(__file__).resolve().parents[1]
    harness = tmp_path / 'checkout' / 'local_server_tests' / 'test_live_server.py'
    harness.parent.mkdir(parents=True)
    harness.write_bytes((root / 'local_server_tests/test_live_server.py').read_bytes())
    selected_home = tmp_path / 'verified-distribution'
    selected_home.mkdir()
    owned_child = object()
    stopped = []
    actual_run = subprocess.run
    monkeypatch.setattr(os, 'environ', dict(os.environ))
    monkeypatch.setenv('ARC_LOCAL_TEST_PYTHON', '/unused/ambient-python')
    monkeypatch.setenv('ARC_LOCAL_TEST_DYNAMODB_HOME', '/unused/ambient-database')
    monkeypatch.setattr(cli, 'verify_distribution', lambda path: selected_home)
    monkeypatch.setattr(cli, 'start_database', lambda *args: owned_child)
    monkeypatch.setattr(cli, 'stop_database', stopped.append)

    class PortReservation:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def bind(self, address):
            assert address == ('127.0.0.1', 0)
        def getsockname(self):
            return ('127.0.0.1', 32123)

    monkeypatch.setattr(runner.socket, 'socket', PortReservation)
    program = '''
import importlib.util, pathlib, sys
spec = importlib.util.spec_from_file_location('selected_harness', sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
assert module.PYTHON == pathlib.Path(sys.executable), 'Wrong live-test Python selected'
assert module.DYNAMODB_HOME == pathlib.Path(sys.argv[2]), 'Wrong live-test database selected'
assert not (module.ROOT / 'var').exists(), 'Default installation must be absent'
'''

    def run_consumer(command, *, cwd, env, timeout, check):
        assert command[:4] == [sys.executable, '-m', 'pytest', '-q']
        result = actual_run([sys.executable, '-c', program, str(harness), str(selected_home)],
                            env=env, capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, result.stderr
        return SimpleNamespace(returncode=17)

    monkeypatch.setattr(runner.subprocess, 'run', run_consumer)
    assert runner.main(['--dynamodb-home', str(selected_home), '--suite', 'all']) == 17
    assert stopped == [owned_child]

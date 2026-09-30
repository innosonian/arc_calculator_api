"""The integration runner must select the same tools for real CLI children."""
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from local_server import cli
from scripts import test_suites as suites
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


class _PortReservation:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def bind(self, address):
        assert address == ('127.0.0.1', 0)

    def getsockname(self):
        return ('127.0.0.1', 32123)


@pytest.fixture
def owned_runner(tmp_path, monkeypatch):
    """The runner with its DB child and port reservation stubbed; returns (home, child, stopped)."""
    home = tmp_path / 'verified-distribution'
    home.mkdir()
    owned_child = object()
    stopped = []
    # runner.main() isolates the process environment (drops ARC_*/AWS_*);
    # keep that on a copy so later suites of the same session keep theirs.
    monkeypatch.setattr(os, 'environ', dict(os.environ))
    monkeypatch.setattr(cli, 'verify_distribution', lambda path: home)
    monkeypatch.setattr(cli, 'start_database', lambda *args: owned_child)
    monkeypatch.setattr(cli, 'stop_database', stopped.append)
    monkeypatch.setattr(runner.socket, 'socket', _PortReservation)
    return home, owned_child, stopped


def test_suite_budget_and_explicit_override_reach_the_pytest_child(owned_runner, monkeypatch):
    home, _, _ = owned_runner
    budgets = []

    def record(command, *, cwd, env, timeout, check):
        budgets.append(timeout)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(runner.subprocess, 'run', record)
    assert runner.main(['--dynamodb-home', str(home), '--suite', 'boundary']) == 0
    assert runner.main(['--dynamodb-home', str(home), '--suite', 'integration']) == 0
    assert runner.main(['--dynamodb-home', str(home), '--suite', 'all', '--timeout-seconds', '7']) == 0
    assert budgets == [suites.SUITE_TIMEOUT_SECONDS['boundary'], suites.SUITE_TIMEOUT_SECONDS['integration'], 7]
    assert budgets[:2] == [600, 600]


def test_exceeded_budget_ends_with_a_fixed_message_and_stops_the_owned_database(owned_runner, monkeypatch, capsys):
    home, owned_child, stopped = owned_runner

    def hang(command, *, cwd, env, timeout, check):
        raise subprocess.TimeoutExpired(command, timeout)
    monkeypatch.setattr(runner.subprocess, 'run', hang)
    assert runner.main(['--dynamodb-home', str(home), '--suite', 'integration']) == runner.TIMEOUT_EXIT_CODE == 3
    captured = capsys.readouterr()
    assert captured.err.strip() == ("Local validation suite 'integration' exceeded its 600-second budget "
                                    "and was stopped.")
    assert captured.out == '' and stopped == [owned_child]


@pytest.mark.parametrize('value', ['0', '-5'])
def test_non_positive_budget_override_is_a_usage_error_before_any_child(owned_runner, monkeypatch, value):
    home, _, stopped = owned_runner
    monkeypatch.setattr(runner.subprocess, 'run', lambda *a, **k: pytest.fail('pytest child started'))
    with pytest.raises(SystemExit) as exit_info:
        runner.main(['--dynamodb-home', str(home), '--suite', 'boundary', '--timeout-seconds', value])
    assert exit_info.value.code == 2 and stopped == []

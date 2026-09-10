"""Local executable exposure and process ownership boundaries."""

import os
from pathlib import Path
import subprocess
import sys

import pytest

from local_server.cli import StartupError, installation_lock, validate_network


@pytest.mark.parametrize("host,clients,ack", [
    ("0.0.0.0", ["192.168.1.5"], True),
    ("8.8.8.8", ["192.168.1.5"], True),
    ("192.168.1.4", [], True),
    ("192.168.1.4", ["192.168.1.5"], False),
    ("192.168.1.4", ["192.168.1.0/24"], True),
    ("192.168.1.4", ["192.168.1.4"], True),
    ("127.0.0.1", ["192.168.1.5"], True),
    ("localhost", [], False),
])
def test_no_implicit_lan_or_wildcard_exposure(host, clients, ack):
    with pytest.raises(StartupError):
        validate_network(host, 8000, 8001, clients, ack)


def test_explicit_lan_allows_only_mac_and_requested_clients():
    assert validate_network("192.168.1.4", 8000, 8001, ["192.168.1.5"], True) == {
        "192.168.1.4", "192.168.1.5",
    }
    assert validate_network("127.0.0.1", 8000, 8001, [], False) == {"127.0.0.1"}


def test_live_lock_refuses_second_process_and_preserves_files(tmp_path):
    directory = tmp_path / "local"
    root = Path(__file__).resolve().parents[1]
    script = "from pathlib import Path; from local_server.cli import installation_lock; " \
             "ctx=installation_lock(Path(__import__('sys').argv[1])); ctx.__enter__()"
    with installation_lock(directory):
        result = subprocess.run([sys.executable, "-c", script, str(directory)], cwd=root,
                                capture_output=True, timeout=5)
        assert result.returncode != 0
        assert b"already owns" in result.stderr
    with installation_lock(directory):
        assert (directory / "server.lock").exists()


def test_symlink_and_broad_permissions_refused(tmp_path):
    destination = tmp_path / "real"
    destination.mkdir(mode=0o700)
    link = tmp_path / "alias"
    link.symlink_to(destination, target_is_directory=True)
    with pytest.raises(StartupError):
        with installation_lock(link):
            pytest.fail("Symlink accepted")
    destination.chmod(0o755)
    with pytest.raises(StartupError):
        with installation_lock(destination):
            pytest.fail("Public directory accepted")


def test_ambient_configuration_removed_and_outbound_blocked_in_child():
    # This audit hook is intentionally process-lifetime only, never installed in pytest.
    script = """
import os,socket
from local_server.cli import isolated_environment, restrict_outbound
os.environ.update(AWS_PROFILE='sentinel',AWS_ENDPOINT_URL='https://invalid.example',
                  HTTP_PROXY='http://invalid.example',SENTRY_DSN='sentinel')
isolated_environment()
assert all(key not in os.environ for key in ('AWS_PROFILE','AWS_ENDPOINT_URL','HTTP_PROXY','SENTRY_DSN'))
assert os.environ['AWS_CONFIG_FILE']==os.devnull
assert os.environ['AWS_SHARED_CREDENTIALS_FILE']==os.devnull
restrict_outbound(8001,'127.0.0.1',8000)
assert socket.getaddrinfo('127.0.0.1','8000')
for address in [('192.0.2.1',443),('127.0.0.1',8002)]:
    with socket.socket() as sock:
        try: sock.connect(address)
        except OSError as error: assert 'denied' in str(error)
        else: raise AssertionError('External socket allowed')
try: socket.getaddrinfo('invalid.example',443)
except OSError as error: assert 'denied' in str(error)
else: raise AssertionError('External DNS allowed')
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, timeout=5)
    assert result.returncode == 0, result.stderr.decode()

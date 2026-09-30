"""Building blocks of the per-directory test network and AWS guards.

Each directory keeps its own contract; these helpers only remove the copied
code. The callers choose what is allowed and which message a violation shows:

* tests/ (unit, conftest): no network at all while a test runs, with the same
  audit events as scripts/run_actions_regression.py, plus the AWS client stubs.
* integration_tests/ and http_pipeline_tests/: only the explicit DynamoDB Local
  endpoint (and, for HTTP, the addresses a test registers).
* transport_integration_tests/: only registered numeric loopback endpoints,
  including name resolution.
* local_server_tests/ unit files: no connection at all.

Standard library, pytest and the (standard-library only) CI runner module.
Messages never include the destination.
"""

from contextlib import contextmanager
import os
import socket
import sys

import pytest

# One audited event set for the CI runner and the per-directory test guards.
from scripts.run_actions_regression import NETWORK_AUDIT_EVENTS  # noqa: F401 (re-exported)


def loopback_validator(allowed, message, *, exact_type=False):
    """Fail unless ``address[:2]`` is in ``allowed`` (a set, or a tuple compared with ==)."""
    def validate(address):
        is_tuple = type(address) is tuple if exact_type else isinstance(address, tuple)
        if not is_tuple or address[:2] not in allowed:
            pytest.fail(message)
    return validate


def guard_socket_connect(monkeypatch, validate, *, resolve=False):
    """Validate every Python-level connect/connect_ex (and getaddrinfo when ``resolve``)."""
    original_connect, original_connect_ex = socket.socket.connect, socket.socket.connect_ex
    original_getaddrinfo = socket.getaddrinfo

    def connect(sock, address):
        validate(address)
        return original_connect(sock, address)

    def connect_ex(sock, address):
        validate(address)
        return original_connect_ex(sock, address)

    def getaddrinfo(host, port, *args, **kwargs):
        validate((host, port))
        return original_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    if resolve:
        monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


def forbid_socket_connect(monkeypatch, message):
    """Reject every connect/connect_ex; returns the rejecting callable for other seams."""
    def forbidden(*args, **kwargs):
        pytest.fail(message)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    return forbidden


def isolate_aws_environment(monkeypatch):
    """No shared AWS profile, instance metadata or Sentry DSN for a loopback DB test."""
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_CONFIG_FILE", os.devnull)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", os.devnull)
    for name in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE", "SENTRY_DSN"):
        monkeypatch.delenv(name, raising=False)


class _OfflineAudit:
    """One process-wide audit hook that enforces only while a guarded test runs.

    Audit hooks cannot be removed, so the hook is installed once and consults a
    counter. The counter is process-wide (not thread-local), so threads that a
    guarded test starts are covered too. Tests of other directories, which use
    loopback sockets on purpose, run while the counter is zero.
    """

    def __init__(self):
        self.active = 0
        self.installed = False

    def __call__(self, event, _args):
        if self.active and event in NETWORK_AUDIT_EVENTS:
            pytest.fail("Unit test attempted network access; tests/ runs without sockets.")


_OFFLINE_AUDIT = _OfflineAudit()


@contextmanager
def offline_network():
    """Forbid the audited network events for the enclosed block (same events as the CI runner)."""
    if not _OFFLINE_AUDIT.installed:
        sys.addaudithook(_OFFLINE_AUDIT)
        _OFFLINE_AUDIT.installed = True
    _OFFLINE_AUDIT.active += 1
    try:
        yield
    finally:
        _OFFLINE_AUDIT.active -= 1

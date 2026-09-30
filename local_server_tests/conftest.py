"""Directory-wide network guard for local_server_tests (E-05).

Unit files here run without any Python-level socket connection: the guard
below rejects every ``socket.connect``/``connect_ex`` (the same
``forbid_socket_connect`` seam the files used to copy one by one). Files that
deliberately drive a loopback listener or an owned DynamoDB Local child (the
real-CLI live files, the pinned Waitress listener tests) opt out with the
``loopback`` marker at module level::

    pytestmark = pytest.mark.loopback

The marker is declared here; tests/test_local_validation_runner.py loads
test_live_server.py standalone (no pytest collection), which the marker
assignment does not disturb. No AWS stubs are installed here: the local
server modules never create SDK clients outside their explicit loopback
endpoint, and the live files must reach their owned DB child.
"""

import pytest

from tests.network_guard_support import forbid_socket_connect

LOOPBACK_MARKER = "loopback"


def pytest_configure(config):
    config.addinivalue_line(
        "markers", f"{LOOPBACK_MARKER}: the file connects to loopback listeners or DB children it owns")


@pytest.fixture(autouse=True)
def network_guard(request, monkeypatch):
    """Reject socket connections unless the test (or its module) carries the loopback marker.

    Returns the rejecting callable for files that reuse it on other seams,
    or None for an opted-out test.
    """
    if request.node.get_closest_marker(LOOPBACK_MARKER) is not None:
        return None
    return forbid_socket_connect(monkeypatch, "local_server_tests unit test attempted a network connection.")

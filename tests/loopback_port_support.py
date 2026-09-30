"""Temporary loopback TCP port selection shared by the live HTTP tests.

The same pattern the tests copied before: the OS picks a free 127.0.0.1 port,
the probe socket closes, and the caller's server binds it afterwards. The
window between the two binds is unchanged (the server start fails visibly if
another process takes the port). local_server_tests/test_live_server.py keeps
its own copy because tests/test_local_validation_runner.py loads that file
standalone, with only the standard library and pytest importable.
"""

import socket


def unused_loopback_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]

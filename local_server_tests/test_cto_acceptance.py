"""Final CTO acceptance gaps: actual listener, occupied DB, and idle requests."""

from contextlib import ExitStack
import socket
import subprocess
import time

from local_server_tests.test_live_server import LiveServer, live, require


def test_owned_database_really_listens_only_on_loopback(live):
    children = subprocess.run(["pgrep", "-P", str(live.process.pid)],
                              capture_output=True, text=True, timeout=3)
    pids = children.stdout.split()
    require(children.returncode == 0 and len(pids) == 1 and pids[0].isdigit(),
            "Expected exactly one owned DB child.")
    identity = subprocess.run(["ps", "-p", pids[0], "-o", "args="],
                              capture_output=True, text=True, timeout=3)
    require("ArcLocalDynamo" in identity.stdout and str(live.data / "dynamodb") in identity.stdout,
            "Unexpected DB child identity.")
    listeners = subprocess.run(["lsof", "-nP", "-a", "-p", pids[0],
                                "-iTCP", "-sTCP:LISTEN", "-Fn"],
                               capture_output=True, text=True, timeout=3)
    names = [line[1:] for line in listeners.stdout.splitlines() if line.startswith("n")]
    require(listeners.returncode == 0 and names == [f"127.0.0.1:{live.db_port}"],
            "Actual DB listen address was not exclusively its loopback endpoint.")


def test_occupied_db_port_preserves_existing_listener(tmp_path):
    server = LiveServer(tmp_path / "occupied-db")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as existing:
        existing.bind(("127.0.0.1", server.db_port))
        existing.listen(1)
        existing.settimeout(2)
        try:
            server.launch()
            require(server.process.wait(timeout=15) != 0, "CLI accepted an occupied DB port.")
            with socket.create_connection(("127.0.0.1", server.db_port), timeout=2) as client:
                accepted, _ = existing.accept()
                with accepted:
                    accepted.sendall(b"still-owned")
                    require(client.recv(32) == b"still-owned", "Existing DB-port listener was disrupted.")
            require(not (server.data / "dynamodb" / "shared-local-instance.db").exists(),
                    "Occupied DB refusal initialized a database.")
        finally:
            server.stop()


def test_oversized_header_returns_431_and_service_recovers(live):
    reply = live.raw(f"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1:{live.port}\r\n".encode()
                     + b"X-Long: " + b"x" * (17 * 1024) + b"\r\n\r\n")
    require(reply.status == 431, "Oversized HTTP headers were not rejected with 431.")
    require(live.request("GET", "/healthz").status == 200, "Header limit left server unavailable.")


def test_incomplete_header_and_body_idle_connections_close_and_recover(live):
    with ExitStack() as stack:
        sockets = [stack.enter_context(socket.create_connection(("127.0.0.1", live.port), timeout=2))
                   for _ in range(2)]
        sockets[0].sendall(f"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1:{live.port}\r\n".encode())
        sockets[1].sendall(f"POST /mock/v1/sessions HTTP/1.1\r\nHost: 127.0.0.1:{live.port}\r\n"
                          "Content-Type: application/json\r\nContent-Length: 100\r\n\r\n{".encode())
        require(live.request("GET", "/healthz").status == 200,
                "Two incomplete requests blocked independent healthy requests.")
        deadline = time.monotonic() + 16
        for sock in sockets:
            sock.settimeout(max(0.1, deadline - time.monotonic()))
            require(sock.recv(1024) == b"", "Incomplete idle request did not close without a response.")
    require(live.request("GET", "/healthz").status == 200, "Idle cleanup left server unavailable.")

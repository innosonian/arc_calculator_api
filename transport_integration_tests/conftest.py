"""Explicit loopback TLS only; excluded from the default pytest testpaths.

The certificates and private keys generated here are disposable test material.
They are never read from a developer account or included in runtime artifacts.
"""

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import threading

import pytest


@pytest.fixture(scope="session")
def local_tls_address():
    host, raw_port = os.environ.get("ARC_TEST_HTTPS_HOST"), os.environ.get("ARC_TEST_HTTPS_PORT")
    if host != "127.0.0.1" or raw_port is None or not raw_port.isascii() or not raw_port.isdigit():
        raise pytest.UsageError("Set ARC_TEST_HTTPS_HOST=127.0.0.1 and ARC_TEST_HTTPS_PORT explicitly (0 selects a temporary port).")
    port = int(raw_port)
    if not 0 <= port < 65536:
        raise pytest.UsageError("The explicit local TLS port must be 0..65535.")
    return host, port


@dataclass(frozen=True)
class TLSFiles:
    certificate: Path
    private_key: Path


@pytest.fixture(scope="session")
def tls_certificates(tmp_path_factory, local_tls_address):
    executable = shutil.which("openssl")
    if executable is None:
        raise pytest.UsageError("The local TLS tests require an installed openssl executable.")
    root = tmp_path_factory.mktemp("arc-local-tls-test-certificates")
    material = {}
    for name, san in (("matched", "IP:127.0.0.1"), ("mismatched", "DNS:localhost")):
        files = TLSFiles(root / f"{name}.crt", root / f"{name}.key")
        completed = subprocess.run([
            executable, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-keyout", str(files.private_key), "-out", str(files.certificate),
            "-subj", "/CN=ARC-local-disposable-test", "-addext", f"subjectAltName={san}",
        ], capture_output=True, timeout=20)
        if completed.returncode != 0:
            raise pytest.UsageError("Disposable local test certificate generation failed.")
        files.private_key.chmod(0o600)
        material[name] = files
    return material


@pytest.fixture(autouse=True)
def loopback_only(monkeypatch, local_tls_address):
    allowed = set()
    original_connect, original_connect_ex = socket.socket.connect, socket.socket.connect_ex
    original_getaddrinfo = socket.getaddrinfo

    def validate(address):
        if type(address) is not tuple or address[:2] not in allowed:
            pytest.fail("TLS integration attempted a connection outside its registered numeric loopback endpoint.")

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
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    for name in ("SENTRY_DSN", "SSL_CERT_FILE", "SSL_CERT_DIR", "SSLKEYLOGFILE"):
        monkeypatch.delenv(name, raising=False)
    yield allowed


class LocalTLSServer:
    """One handler per accepted connection, with cleanup even on client abort."""

    def __init__(self, address, files, handler, allowed, *, handshake=True):
        self.stopped = threading.Event()
        self.requests = []
        self.connections = 0
        self.handshake_failures = 0
        self.errors = []
        self.handler = handler
        self.handshake = handshake
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(files.certificate, files.private_key)
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(address)
        self.address = self.listener.getsockname()
        self.listener.listen(4)
        self.listener.settimeout(0.05)
        allowed.add(self.address)
        self.url = f"https://127.0.0.1:{self.address[1]}/fixed/endpoint"
        self.thread = threading.Thread(target=self._run, name="arc-disposable-local-tls", daemon=True)
        self.thread.start()

    def _run(self):
        while not self.stopped.is_set():
            try:
                raw, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self.connections += 1
            raw.settimeout(2)
            if self.handshake:
                try:
                    connection = self.context.wrap_socket(raw, server_side=True)
                except (ssl.SSLError, OSError):
                    self.handshake_failures += 1
                    raw.close()
                    continue
            else:
                # Only a test peer withholding the TLS handshake; this path
                # cannot receive an authenticated HTTP request.
                connection = raw
            try:
                self.handler(connection, self)
            except (BrokenPipeError, ConnectionResetError, ssl.SSLError, socket.timeout):
                # Expected when the client rejects a response or its deadline
                # expires while this test server is deliberately trickling.
                pass
            except Exception as error:
                self.errors.append(type(error).__name__)
            finally:
                connection.close()

    def read_request(self, connection):
        data = bytearray()
        while b"\r\n\r\n" not in data:
            chunk = connection.recv(4096)
            if not chunk:
                raise AssertionError("The test client disconnected before sending headers.")
            data.extend(chunk)
            if len(data) > 32768:
                raise AssertionError("Unexpectedly large local test request headers.")
        header, body = bytes(data).split(b"\r\n\r\n", 1)
        lengths = [line.split(b":", 1)[1].strip() for line in header.split(b"\r\n")[1:]
                   if line.lower().startswith(b"content-length:")]
        if len(lengths) != 1:
            raise AssertionError("Expected exactly one request content length.")
        size = int(lengths[0])
        while len(body) < size:
            chunk = connection.recv(size - len(body))
            if not chunk:
                raise AssertionError("The test client disconnected before sending its body.")
            body += chunk
        self.requests.append((header, body))
        return header, body

    def stop(self):
        self.stopped.set()
        self.listener.close()
        self.thread.join(timeout=3)
        if self.thread.is_alive():
            pytest.fail("A local TLS test thread did not terminate.")
        assert self.errors == [], self.errors


@pytest.fixture
def tls_server(local_tls_address, tls_certificates, loopback_only):
    servers = []

    def create(handler, *, certificate="matched", handshake=True):
        server = LocalTLSServer(local_tls_address, tls_certificates[certificate], handler, loopback_only,
                                handshake=handshake)
        servers.append(server)
        return server

    yield create
    for server in reversed(servers):
        server.stop()


@pytest.fixture
def trust_test_certificate(monkeypatch, tls_certificates):
    from mock_journey import https_transport
    original = https_transport.ssl.create_default_context

    def trust(name="matched"):
        def context():
            result = original(cafile=str(tls_certificates[name].certificate))
            assert result.verify_mode == ssl.CERT_REQUIRED
            assert result.check_hostname is True
            return result
        monkeypatch.setattr(https_transport.ssl, "create_default_context", context)

    return trust

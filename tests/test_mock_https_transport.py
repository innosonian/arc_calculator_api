"""No-network transport boundary tests; real TLS tests live separately."""

import socket

import pytest

from mock_journey import https_transport as transport


def limits(**changes):
    return transport.TransportLimits(**({
        "connect_seconds": 1.0, "io_seconds": 0.5, "total_seconds": 2.0,
        "max_request_bytes": 32, "max_response_bytes": 16,
    } | changes))


class SecureBytes:
    def __init__(self, wire):
        self.wire = bytearray(wire)
        self.sent = bytearray()
        self.closed = False
        self.timeouts = []

    def settimeout(self, value):
        self.timeouts.append(value)

    def recv_into(self, buffer):
        assert not self.closed
        size = min(len(self.wire), len(buffer))
        buffer[:size] = self.wire[:size]
        del self.wire[:size]
        return size

    def send(self, data):
        assert not self.closed
        size = min(len(data), 7)
        self.sent.extend(data[:size])
        return size

    def close(self):
        self.closed = True


def exchange(monkeypatch, wire, *, method="POST", **changes):
    secure = SecureBytes(wire)
    calls = []
    def connect(*args):
        calls.append(args)
        return secure
    monkeypatch.setattr(transport, "_connect", connect)
    client = transport.HttpsTransport("https://example.invalid/fixed?version=1", method,
                                     (("Content-Type", "application/octet-stream"),), limits(**changes))
    return secure, calls, client


@pytest.mark.parametrize("wire,expected", [
    (b"HTTP/1.1 500 Oops\r\nContent-Length: 3\r\nX-A: b\r\nX-A: c\r\n\r\n\xff\x00!", b"\xff\x00!"),
    (b"HTTP/1.1 200 OK\r\n\r\nwhole EOF body", b"whole EOF body"),
    (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n2\r\nab\r\n1\r\nc\r\n0\r\nX-T: value\r\n\r\n", b"abc"),
    (b"HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\nContent-Length: 3\r\n\r\nraw", b"raw"),
])
def test_one_exchange_raw_bytes_eof_and_closure(monkeypatch, wire, expected):
    secure, calls, client = exchange(monkeypatch, wire)
    result = client.exchange(b"real\x00binary")
    assert result.body == expected
    assert type(result.status) is int and type(result.headers) is tuple
    assert secure.closed and len(calls) == 1
    assert bytes(secure.sent).startswith(b"POST /fixed?version=1 HTTP/1.1\r\n")
    assert bytes(secure.sent).endswith(b"real\x00binary")
    assert b"Content-Length: 11\r\n" in secure.sent
    assert all(0 < value <= 0.5 for value in secure.timeouts)
    assert expected.decode("latin-1") not in repr(result)


@pytest.mark.parametrize("wire,code", [
    (b"HTTP/1.1 200 OK\r\n\r\n" + b"x" * 17, "RESPONSE_TOO_LARGE"),
    (b"HTTP/1.1 200 OK\r\nContent-Length: -1\r\n\r\n", "INVALID_HTTP_RESPONSE"),
    (b"HTTP/1.1 200 OK\r\nContent-Length: +1\r\n\r\nx", "INVALID_HTTP_RESPONSE"),
    (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: gzip, chunked\r\n\r\n", "INVALID_HTTP_RESPONSE"),
    (b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\nx", "INVALID_HTTP_RESPONSE"),
    (b"REMOTE-PRIVATE-ERROR\r\n\r\n", "INVALID_HTTP_RESPONSE"),
])
def test_errors_are_bounded_sanitized_and_closed(monkeypatch, wire, code):
    secure, calls, client = exchange(monkeypatch, wire)
    with pytest.raises(transport.TransportError) as error:
        client.exchange(b"PRIVATE-REQUEST")
    assert error.value.code == code and str(error.value) == code
    assert error.value.__context__ is None and error.value.__cause__ is None
    assert secure.closed and len(calls) == 1


@pytest.mark.parametrize("method,status", [("HEAD", 200), ("POST", 204), ("POST", 304)])
def test_protocol_no_body_semantics(monkeypatch, method, status):
    secure, _, client = exchange(monkeypatch,
        f"HTTP/1.1 {status} OK\r\nContent-Length: 999\r\n\r\n".encode(), method=method)
    assert client.exchange(b"").body == b""
    assert secure.closed


def test_delayed_dns_returns_late_but_never_connects(monkeypatch):
    now = [0.0]
    def resolve(*args, **kwargs):
        now[0] = 5.0
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 443))]
    monkeypatch.setattr(transport.socket, "getaddrinfo", resolve)
    monkeypatch.setattr(transport.socket, "socket", lambda *args: pytest.fail("Expired DNS must not connect."))
    client = transport.HttpsTransport("https://example.invalid", "POST", (), limits(), clock=lambda: now[0])
    with pytest.raises(transport.TransportError, match="TRANSPORT_TIMEOUT"):
        client.exchange(b"test")
    assert now[0] > 2.0  # Deliberately does NOT assert a cancellable DNS timeout.


def test_budget_reader_defers_underlying_close_until_reader_finishes():
    secure = SecureBytes(b"one")
    wrapper = transport._BudgetSocket(secure, transport._Budget(limits(), lambda: 0.0))
    reader = wrapper.makefile("rb")
    wrapper.close()
    assert not secure.closed
    assert reader.read() == b"one"
    reader.close()
    reader.close()
    assert secure.closed and wrapper.readers == 0


def test_positive_exact_limits():
    for field in ("connect_seconds", "io_seconds", "total_seconds", "max_request_bytes", "max_response_bytes"):
        for invalid in (0, -1, True, None, "1", float("inf"), float("nan")):
            with pytest.raises(transport.TransportError, match="INVALID_TRANSPORT_CONFIGURATION"):
                limits(**{field: invalid})


def test_platform_timeout_overflow_is_sanitized_and_socket_closed(monkeypatch):
    secure, calls, client = exchange(monkeypatch, b"", io_seconds=1e308, total_seconds=1e308)
    def rejected_timeout(value):
        raise OverflowError("Platform timeout cannot hold supplied value.")
    secure.settimeout = rejected_timeout
    with pytest.raises(transport.TransportError) as error:
        client.exchange(b"test")
    assert error.value.code == "INVALID_TRANSPORT_CONFIGURATION"
    assert error.value.__context__ is None and error.value.__cause__ is None
    assert secure.closed and len(calls) == 1 and not secure.sent


def test_empty_query_delimiter_is_preserved_on_request_line(monkeypatch):
    secure, calls, _ = exchange(monkeypatch, b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
    client = transport.HttpsTransport("https://example.invalid/fixed?", "POST", (), limits())
    client.exchange(b"test")
    assert bytes(secure.sent).startswith(b"POST /fixed? HTTP/1.1\r\n")
    assert secure.closed and len(calls) == 1


@pytest.mark.parametrize("level", [1, 2])
def test_ambient_http_debug_never_prints_request_or_response(monkeypatch, capsys, level):
    monkeypatch.setattr(transport.http.client.HTTPConnection, "debuglevel", level)
    wire = b"HTTP/1.1 200 PRIVATE-REASON\r\nX-Private: PRIVATE-HEADER\r\nContent-Length: 13\r\n\r\nPRIVATE-REPLY"
    secure, _, client = exchange(monkeypatch, wire)
    assert client.exchange(b"PRIVATE-REQUEST").body == b"PRIVATE-REPLY"
    assert secure.closed
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("url", [
    "http://example.invalid", "https://user:password@example.invalid", "https://example.invalid/#fragment",
    "https://[::1]suffix/a", "https://example.invalid:", "https://example.invalid:0",
    "https://example.invalid:65536", "https://example.invalid\n/a", "https://example.invalid\\evil/a",
    "https://example.invalid/ space", "https://", "https://%65xample.invalid/", None,
])
def test_destination_rejected_without_lookup(monkeypatch, url):
    monkeypatch.setattr(transport.socket, "getaddrinfo", lambda *a, **kw: pytest.fail("No network during validation."))
    with pytest.raises(transport.TransportError, match="INVALID_TRANSPORT_CONFIGURATION"):
        transport.HttpsTransport(url, "POST", (), limits())

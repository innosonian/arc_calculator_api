"""S2 independent attacks using the real HTTP parser and scripted TLS I/O."""

from dataclasses import replace
import socket
import traceback

import pytest

from mock_journey import https_transport as transport


LIMITS = transport.TransportLimits(2.0, 0.2, 3.0, 32, 32)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class ScriptedTLS:
    """Each recv delivers one script fragment with a controlled elapsed time."""

    def __init__(self, fragments, clock):
        self.fragments = [(delay, bytearray(data)) for delay, data in fragments]
        self.clock = clock
        self.timeout = None
        self.timeouts = []
        self.sent = bytearray()
        self.received = 0
        self.closed = False
        self.send_error = None

    def settimeout(self, value):
        assert value > 0
        self.timeout = value
        self.timeouts.append(value)

    def recv_into(self, target):
        assert not self.closed
        if not self.fragments:
            return 0
        delay, data = self.fragments[0]
        if delay >= self.timeout:
            self.clock.now += self.timeout
            raise TimeoutError("PRIVATE-I/O-DETAIL")
        self.clock.now += delay
        count = min(len(target), len(data))
        self.received += count
        target[:count] = data[:count]
        del data[:count]
        if data:
            self.fragments[0] = (0, data)
        else:
            self.fragments.pop(0)
        return count

    def send(self, data):
        assert not self.closed
        if self.send_error is not None:
            self.sent.extend(data[:1])
            raise self.send_error
        self.sent.extend(data)
        return len(data)

    def close(self):
        self.closed = True


def connected(monkeypatch, fragments, *, selected=LIMITS):
    clock = Clock()
    secure = ScriptedTLS(fragments, clock)
    calls = []
    def connect(host, port, budget, limits):
        calls.append((host, port))
        return secure
    monkeypatch.setattr(transport, "_connect", connect)
    client = transport.HttpsTransport("https://fixed.example.invalid/immutable", "POST", (), selected, clock=clock)
    return client, secure, calls, clock


def assert_private_error(error, expected):
    assert error.code == expected
    assert str(error) == expected
    assert error.__context__ is None and error.__cause__ is None
    assert "PRIVATE" not in "".join(traceback.format_exception(error))


@pytest.mark.parametrize("url", [
    "https://[PRIVATE-MARKER]/", "https://[fe80::PRIVATE-MARKER]/",
    "https://fixed.example.invalid:65536/PRIVATE-MARKER",
])
def test_invalid_destination_retains_no_private_parser_exception(monkeypatch, url, capsys):
    monkeypatch.setattr(transport.socket, "getaddrinfo", lambda *a, **kw: pytest.fail("Configuration made a DNS request."))
    with pytest.raises(transport.TransportError) as raised:
        transport.HttpsTransport(url, "POST", (), LIMITS)
    assert_private_error(raised.value, "INVALID_TRANSPORT_CONFIGURATION")
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("headers", [
    (("X-Test", "PRIVATE\r\nHost: attacker"),), (("X-Test\nPRIVATE", "value"),),
    (("Content-Length", "1"),), (("Transfer-Encoding", "chunked"),),
    (("Host", "PRIVATE.example.invalid"),), (("Proxy-Authorization", "PRIVATE"),),
    (("Connection", "keep-alive"),), (("X-Test", "a"), ("x-test", "b")),
])
def test_headers_cannot_inject_or_replace_owned_request_framing(headers):
    with pytest.raises(transport.TransportError) as raised:
        transport.HttpsTransport("https://fixed.example.invalid/", "POST", headers, LIMITS)
    assert_private_error(raised.value, "INVALID_TRANSPORT_CONFIGURATION")


@pytest.mark.parametrize("method", ["POST\r\nPRIVATE", "CONNECT", "TRACE", "post /PRIVATE", b"POST"])
def test_method_cannot_tunnel_or_inject_request_line(method):
    with pytest.raises(transport.TransportError) as raised:
        transport.HttpsTransport("https://fixed.example.invalid/", method, (), LIMITS)
    assert_private_error(raised.value, "INVALID_TRANSPORT_CONFIGURATION")


@pytest.mark.parametrize("body", [None, "PRIVATE", bytearray(b"PRIVATE"), memoryview(b"PRIVATE"), b"x" * 33])
def test_invalid_request_body_never_connects(monkeypatch, body):
    monkeypatch.setattr(transport, "_connect", lambda *args: pytest.fail("Invalid body reached connection."))
    client = transport.HttpsTransport("https://fixed.example.invalid", "POST", (), LIMITS)
    with pytest.raises(transport.TransportError) as raised:
        client.exchange(body)
    assert_private_error(raised.value, "INVALID_TRANSPORT_CONFIGURATION")


def test_request_exact_cap_is_preserved_and_return_repr_is_private(monkeypatch):
    wire = b"HTTP/1.1 200 OK\r\nContent-Length: 7\r\nX-Private: PRIVATE\r\n\r\nPRIVATE"
    client, secure, calls, _ = connected(monkeypatch, [(0, wire)])
    body = b"x" * LIMITS.max_request_bytes
    result = client.exchange(body)
    assert bytes(secure.sent).endswith(body)
    assert result.body == b"PRIVATE"
    assert "PRIVATE" not in repr(result)
    assert secure.closed and calls == [("fixed.example.invalid", 443)]


@pytest.mark.parametrize("failure", [OSError("PRIVATE socket address"), TimeoutError("PRIVATE socket timeout")])
def test_partial_send_failure_closes_without_reconnect_or_error_context(monkeypatch, failure, capsys):
    client, secure, calls, _ = connected(monkeypatch, [])
    secure.send_error = failure
    body = b"PRIVATE-REQUEST"
    with pytest.raises(transport.TransportError) as raised:
        client.exchange(body)
    expected = "TRANSPORT_TIMEOUT" if type(failure) is TimeoutError else "TRANSPORT_FAILED"
    assert_private_error(raised.value, expected)
    assert secure.closed and len(calls) == 1 and secure.sent
    assert capsys.readouterr() == ("", "")


def test_oversized_content_length_cannot_be_downgraded_to_eof_by_python_int_limit(monkeypatch):
    wire = b"HTTP/1.1 200 OK\r\nContent-Length: " + b"9" * 5000 + b"\r\n\r\n{}"
    client, secure, calls, _ = connected(monkeypatch, [(0, wire)])
    with pytest.raises(transport.TransportError) as raised:
        client.exchange(b"test")
    assert_private_error(raised.value, "RESPONSE_TOO_LARGE")
    assert secure.closed and len(calls) == 1


@pytest.mark.parametrize("declared,body", [(b"0" * 5000 + b"2", b"{}"), (b"0" * 5000, b"")])
def test_long_zero_prefixed_bounded_content_length_preserves_bytes(monkeypatch, declared, body):
    wire = b"HTTP/1.1 200 OK\r\nContent-Length: " + declared + b"\r\n\r\n" + body
    client, secure, calls, _ = connected(monkeypatch, [(0, wire)])
    assert client.exchange(b"test").body == body
    assert secure.closed and len(calls) == 1


def test_head_giant_hypothetical_content_length_is_not_a_received_body(monkeypatch):
    wire = b"HTTP/1.1 200 OK\r\nContent-Length: " + b"9" * 5000 + b"\r\n\r\n"
    _, secure, calls, clock = connected(monkeypatch, [(0, wire)])
    client = transport.HttpsTransport("https://fixed.example.invalid", "HEAD", (), LIMITS, clock=clock)
    assert client.exchange(b"").body == b""
    assert secure.closed and len(calls) == 1


@pytest.mark.parametrize("chunk_body", [b"1\r\nxZZ0\r\n\r\n", b"1\r\nx\n\n0\r\n\r\n"])
def test_invalid_chunk_terminator_is_not_silently_discarded(monkeypatch, chunk_body):
    wire = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n" + chunk_body
    client, secure, calls, _ = connected(monkeypatch, [(0, wire)])
    with pytest.raises(transport.TransportError) as raised:
        client.exchange(b"test")
    assert_private_error(raised.value, "INVALID_HTTP_RESPONSE")
    assert secure.closed and len(calls) == 1


def test_negative_chunk_size_cannot_trigger_unbounded_read_before_cap_check(monkeypatch):
    header = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n-1\r\n"
    body = b"x" * 65536
    client, secure, calls, _ = connected(monkeypatch, [(0, header), (0, body)])
    with pytest.raises(transport.TransportError) as raised:
        client.exchange(b"test")
    assert_private_error(raised.value, "INVALID_HTTP_RESPONSE")
    # The malformed size must be rejected before a parser read(-1) consumes
    # the oversized payload, even when that parser eventually raises anyway.
    assert secure.received == len(header)
    assert secure.closed and len(calls) == 1


@pytest.mark.parametrize("chunk_body", [
    b"+1\r\nx\r\n0\r\n\r\n", b"0x1\r\nx\r\n0\r\n\r\n",
    b"1_0\r\n" + b"x" * 16 + b"\r\n0\r\n\r\n",
    b"1\nx\r\n0\r\n\r\n", b"0\r\n", b"0\r\nX-Test: value\r\n",
])
def test_non_hex_size_and_missing_chunk_trailer_terminator_are_not_success(monkeypatch, chunk_body):
    wire = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n" + chunk_body
    client, secure, calls, _ = connected(monkeypatch, [(0, wire)])
    with pytest.raises(transport.TransportError) as raised:
        client.exchange(b"test")
    assert_private_error(raised.value, "INVALID_HTTP_RESPONSE")
    assert secure.closed and len(calls) == 1


@pytest.mark.parametrize("prefix,suffix", [
    (b"HTTP/1.1 ", b" 200 OK\r\nContent-Length: 0\r\n\r\n"),
    (b"HTTP/1.1 200 OK\r\nX-Slow: ", b"\r\nContent-Length: 0\r\n\r\n"),
    (b"HTTP/1.1 200 OK\r\nContent-Length: 80\r\n\r\n", b""),
    (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n1;x=", b"\r\na\r\n0\r\n\r\n"),
    (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n0\r\nX-Slow: ", b"\r\n\r\n"),
])
def test_deadline_covers_hidden_http_parser_reads(monkeypatch, prefix, suffix):
    selected = replace(LIMITS, total_seconds=0.3, max_response_bytes=128)
    fragments = [(0, prefix)] + [(0.04, b"a")] * 80 + [(0, suffix)]
    client, secure, calls, clock = connected(monkeypatch, fragments, selected=selected)
    with pytest.raises(transport.TransportError) as raised:
        client.exchange(b"test")
    assert_private_error(raised.value, "TRANSPORT_TIMEOUT")
    assert secure.closed and len(calls) == 1
    assert clock.now <= 0.3
    assert secure.timeouts[-1] < selected.io_seconds


def test_all_tcp_addresses_share_one_connect_budget(monkeypatch):
    clock = Clock()
    observed = []
    class RefusingSocket:
        def __init__(self, *args):
            self.timeout = None
            self.closed = False
            observed.append(self)
        def settimeout(self, value):
            self.timeout = value
        def connect(self, address):
            clock.now += min(1.2, self.timeout)
            raise TimeoutError("PRIVATE connection failure")
        def close(self):
            self.closed = True
    addresses = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (f"127.0.0.{i}", 443)) for i in (1, 2, 3)]
    monkeypatch.setattr(transport.socket, "getaddrinfo", lambda *a, **kw: addresses)
    monkeypatch.setattr(transport.socket, "socket", RefusingSocket)
    client = transport.HttpsTransport("https://fixed.example.invalid", "POST", (), LIMITS, clock=clock)
    with pytest.raises(transport.TransportError) as raised:
        client.exchange(b"test")
    assert_private_error(raised.value, "TRANSPORT_TIMEOUT")
    assert len(observed) == 2 and all(sock.closed for sock in observed)
    assert observed[0].timeout == 2.0 and observed[1].timeout == pytest.approx(0.8)
    assert clock.now == 2.0

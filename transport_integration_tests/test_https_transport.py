"""Real local TLS and stdlib HTTP parsing with test-owned loopback servers."""

import time

import pytest

from mock_journey.https_transport import HttpsTransport, TransportError, TransportLimits


def limits(**changes):
    # These are small fixture values, not deployment operating limits.
    values = {"connect_seconds": 1.0, "io_seconds": 1.0, "total_seconds": 3.0,
              "max_request_bytes": 1024, "max_response_bytes": 1024}
    return TransportLimits(**(values | changes))


def transport(server, **changes):
    return HttpsTransport(server.url, "POST", (("Content-Type", "application/octet-stream"),), limits(**changes))


def respond(payload):
    def handler(connection, server):
        server.read_request(connection)
        connection.sendall(payload)
    return handler


def test_real_trusted_tls_preserves_request_and_response_bytes(tls_server, trust_test_certificate):
    trust_test_certificate()
    expected = b"\0\xff+&=%\r\nraw-response"
    server = tls_server(respond(b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(expected)).encode()
                                + b"\r\nConnection: close\r\n\r\n" + expected))
    body = b"\xff\x00measured+&=%bytes"
    result = transport(server).exchange(body)
    assert result.status == 200 and result.body == expected
    assert type(result.headers) is tuple
    assert server.requests[0][0].startswith(b"POST /fixed/endpoint HTTP/1.1\r\n")
    assert server.requests[0][1] == body
    assert server.connections == 1 and len(server.requests) == 1


def test_real_request_preserves_explicit_empty_query_delimiter(tls_server, trust_test_certificate):
    trust_test_certificate()
    server = tls_server(respond(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"))
    client = HttpsTransport(server.url + "?", "POST", (), limits())
    assert client.exchange(b"test").status == 200
    assert server.requests[0][0].split(b"\r\n", 1)[0] == b"POST /fixed/endpoint? HTTP/1.1"
    assert server.connections == 1 and len(server.requests) == 1


def test_real_tls_does_not_write_secrets_to_environment_keylog_file(
        tls_server, trust_test_certificate, monkeypatch, tmp_path):
    destination = tmp_path / "disposable-test-keylog.txt"
    monkeypatch.setenv("SSLKEYLOGFILE", str(destination))
    trust_test_certificate()
    server = tls_server(respond(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"))
    assert transport(server).exchange(b"test").status == 200
    # The default TLS context factory may create the file and a comment
    # header before the transport disables key logging. No secret row may
    # remain after the real handshake, which is the property under test.
    if destination.exists():
        assert all(not line.strip() or line.lstrip().startswith("#")
                   for line in destination.read_text(encoding="ascii").splitlines())
    assert server.connections == 1 and len(server.requests) == 1


def test_untrusted_real_certificate_is_rejected_before_http_request(tls_server):
    server = tls_server(respond(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"))
    with pytest.raises(TransportError) as error:
        transport(server).exchange(b"test")
    assert error.value.code == "TRANSPORT_FAILED"
    assert server.requests == [] and server.connections == 1


def test_trusted_certificate_for_different_host_is_rejected(tls_server, trust_test_certificate):
    trust_test_certificate("mismatched")
    server = tls_server(respond(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"), certificate="mismatched")
    with pytest.raises(TransportError) as error:
        transport(server).exchange(b"test")
    assert error.value.code == "TRANSPORT_FAILED"
    assert server.requests == [] and server.connections == 1


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_redirect_location_cannot_trigger_second_connection(tls_server, trust_test_certificate, status):
    trust_test_certificate()
    server = tls_server(respond(b"HTTP/1.1 " + str(status).encode() + b" Redirect\r\nLocation: https://forbidden.example.invalid/PRIVATE\r\n"
                                b"Content-Length: 0\r\n\r\n"))
    result = transport(server).exchange(b"test")
    assert result.status == status and result.body == b""
    assert server.connections == 1 and len(server.requests) == 1


@pytest.mark.parametrize("chunked", [False, True])
@pytest.mark.parametrize("size", [16, 17])
def test_real_response_limit_boundary_and_chunked_body(tls_server, trust_test_certificate, size, chunked):
    trust_test_certificate()
    body = b"x" * size
    if chunked:
        response = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n" + hex(size)[2:].encode() + b"\r\n" + body + b"\r\n0\r\n\r\n"
    else:
        response = b"HTTP/1.1 200 OK\r\nContent-Length: " + str(size).encode() + b"\r\n\r\n" + body
    server = tls_server(respond(response))
    client = transport(server, max_response_bytes=16)
    if size == 16:
        assert client.exchange(b"test").body == body
    else:
        with pytest.raises(TransportError) as error:
            client.exchange(b"test")
        assert error.value.code == "RESPONSE_TOO_LARGE"
    assert server.connections == 1


@pytest.mark.parametrize("size", [16, 17])
def test_real_eof_delimited_body_is_preserved_and_capped(tls_server, trust_test_certificate, size):
    trust_test_certificate()
    body = b"x" * size
    server = tls_server(respond(b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n" + body))
    client = transport(server, max_response_bytes=16)
    if size == 16:
        assert client.exchange(b"test").body == body
    else:
        with pytest.raises(TransportError) as error:
            client.exchange(b"test")
        assert error.value.code == "RESPONSE_TOO_LARGE"
    assert server.connections == 1 and len(server.requests) == 1


@pytest.mark.parametrize("response", [
    b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nabc",
    b"HTTP/1.1 200 OK\r\nContent-Length: 1\r\nContent-Length: 1\r\n\r\nx",
    b"HTTP/1.1 200 OK\r\nContent-Length: 1\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n",
    b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n3\r\nab",
])
def test_real_malformed_or_truncated_framing_is_not_success(tls_server, trust_test_certificate, response):
    trust_test_certificate()
    server = tls_server(respond(response))
    with pytest.raises(TransportError) as error:
        transport(server).exchange(b"test")
    assert error.value.code == "INVALID_HTTP_RESPONSE"
    assert server.connections == 1 and len(server.requests) == 1


def test_server_disconnect_after_request_cannot_replay(tls_server, trust_test_certificate):
    trust_test_certificate()
    def disconnect(connection, server):
        server.read_request(connection)
    server = tls_server(disconnect)
    with pytest.raises(TransportError) as error:
        transport(server).exchange(b"TEST-PRIVATE-MARKER")
    assert error.value.code in {"TRANSPORT_FAILED", "INVALID_HTTP_RESPONSE"}
    assert "TEST-PRIVATE-MARKER" not in str(error.value)
    assert server.connections == 1 and len(server.requests) == 1


@pytest.mark.parametrize("phase,prefix,suffix", [
    ("status-line", b"HTTP/1.1 200 ", b"\r\nContent-Length: 1\r\n\r\nx"),
    ("headers", b"HTTP/1.1 200 OK\r\nX-Slow: ", b"\r\nContent-Length: 1\r\n\r\nx"),
    ("body", b"HTTP/1.1 200 OK\r\nContent-Length: 80\r\n\r\n", b""),
    ("chunk-size", b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n1;slow=", b"\r\nx\r\n0\r\n\r\n"),
    ("trailers", b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n0\r\nX-Slow: ", b"\r\n\r\n"),
])
def test_real_trickle_traffic_cannot_extend_total_budget(tls_server, trust_test_certificate, phase, prefix, suffix):
    trust_test_certificate()
    def trickle(connection, server):
        server.read_request(connection)
        connection.sendall(prefix)
        for _ in range(80):
            if server.stopped.wait(0.03):
                return
            connection.sendall(b"a")
        connection.sendall(suffix)
    server = tls_server(trickle)
    client = transport(server, connect_seconds=0.8, io_seconds=0.2, total_seconds=0.35)
    start = time.monotonic()
    with pytest.raises(TransportError) as error:
        client.exchange(b"test")
    elapsed = time.monotonic() - start
    assert error.value.code == "TRANSPORT_TIMEOUT", phase
    # Slack permits local scheduling/TLS overhead while distinguishing the
    # 2.4-second endless-per-read-timeout behavior this counterexample attacks.
    assert elapsed < 1.5, (phase, elapsed)
    assert server.connections == 1 and len(server.requests) == 1


def test_real_post_send_no_response_times_out_without_replay(tls_server, trust_test_certificate):
    trust_test_certificate()
    def silent(connection, server):
        server.read_request(connection)
        server.stopped.wait(2)
    server = tls_server(silent)
    client = transport(server, connect_seconds=0.8, io_seconds=0.2, total_seconds=0.8)
    start = time.monotonic()
    with pytest.raises(TransportError) as error:
        client.exchange(b"TEST-PRIVATE-REQUEST")
    assert error.value.code == "TRANSPORT_TIMEOUT"
    assert "PRIVATE" not in str(error.value)
    assert time.monotonic() - start < 1.5
    assert server.connections == 1 and len(server.requests) == 1


def test_real_tls_handshake_stall_times_out_before_http_request(tls_server, trust_test_certificate):
    trust_test_certificate()
    def withhold_handshake(connection, server):
        server.stopped.wait(2)
    server = tls_server(withhold_handshake, handshake=False)
    client = transport(server, connect_seconds=0.2, io_seconds=0.8, total_seconds=0.8)
    start = time.monotonic()
    with pytest.raises(TransportError) as error:
        client.exchange(b"TEST-PRIVATE-REQUEST")
    assert error.value.code == "TRANSPORT_TIMEOUT"
    assert time.monotonic() - start < 1.5
    assert server.connections == 1 and server.requests == []

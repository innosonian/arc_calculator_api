"""Independent parser/WSGI and durable relay counterexamples; never open sockets."""

import base64
from copy import deepcopy
from types import SimpleNamespace

import pytest

import local_server.http as http
from local_server.execution import LocalJobRunner
from mock_journey.jobs import DynamoJobRepository
from mock_journey.state import DynamoStateRepository
from tests.test_mock_state import ScriptedClient, item


ATTEMPT = "a1234567-1234-4234-9234-123456789abc"
WIRE = 32770  # Explicit test fixture, not a runtime recommendation.
SECRET = "TRANSPORT_PRIVATE_MARKER"


def harness(monkeypatch, *, wire=WIRE, payload=None, peer="127.0.0.1"):
    from waitress.adjustments import Adjustments
    import waitress.server
    from waitress.task import WSGITask

    observed = []
    options = {}

    def handle(event, context, service):
        observed.append(deepcopy(event))
        return {"statusCode": 202, "body": '{"state":"queued"}'}

    def no_socket(application, **kwargs):
        options.update(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr(http, "handle", handle)
    monkeypatch.setattr(waitress.server, "create_server", no_socket)
    payload = 4 * ((wire + 2) // 3) if payload is None else payload
    service = SimpleNamespace(calculation=SimpleNamespace(payload_limit=payload))
    app = http.make_application(service, lambda: True, "127.0.0.1", 8000,
                                ("127.0.0.1",), calculation_body_limit=wire)
    server = http.create_server(app, "127.0.0.1", 8000)
    shared = Adjustments(**options)
    channel = SimpleNamespace(
        addr=(peer, 12345), check_client_disconnected=lambda: False,
        server=SimpleNamespace(adj=shared, effective_port=8000, server_name="localhost"),
    )

    def invoke(parser):
        assert parser.completed and parser.error is None
        environ = WSGITask(channel, parser).get_environment()
        response = {}

        def start(status, headers):
            response.update(status=int(status.split()[0]), headers=dict(headers))

        response["body"] = b"".join(app(environ, start))
        return response

    return SimpleNamespace(parser=server.channel_class.parser_class, shared=shared,
                           invoke=invoke, observed=observed, options=options)


def header(path="/cpr-analysis", *, size=0, extra=b"", method="POST"):
    return (f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:8000\r\n"
            f"Content-Length: {size}\r\nAuthorization: Bearer synthetic-test\r\n"
            f"X-Attempt-ID: {ATTEMPT}\r\n".encode()
            + extra + b"\r\n")


@pytest.mark.parametrize("wire", [32769, 32770, 32771])
def test_exact_base64_expansion_is_admitted_and_one_byte_smaller_configuration_is_rejected(monkeypatch, wire):
    expected = 4 * ((wire + 2) // 3)
    with pytest.raises(ValueError, match="encoded wire-body"):
        harness(monkeypatch, wire=wire, payload=expected - 1)
    testing = harness(monkeypatch, wire=wire, payload=expected)
    parser = testing.parser(testing.shared)
    try:
        parser.received(header(size=wire, extra=b"Content-Type: multipart/form-data; boundary=test\r\n"))
        body = bytes(range(256)) * (wire // 256) + bytes(range(wire % 256))
        assert parser.received(body) == wire
        assert testing.invoke(parser)["status"] == 202
        value = testing.observed[0]
        assert len(value["body"]) == expected and value["isBase64Encoded"] is True
        assert base64.b64decode(value["body"], validate=True) == body
        assert parser.body_rcv.buf.overflowed is False
    finally:
        parser.close()


@pytest.mark.parametrize("first", ["calculation", "control"])
def test_interleaved_parser_objects_cannot_transfer_the_calculation_limit(monkeypatch, first):
    testing = harness(monkeypatch)
    parsers = {name: testing.parser(testing.shared) for name in ("calculation", "control")}
    try:
        order = (first, "control" if first == "calculation" else "calculation")
        for name in order:
            parsers[name].received(header("/cpr-analysis" if name == "calculation" else "/mock/v1/sessions",
                                          size=WIRE if name == "calculation" else http.BODY_LIMIT))
        assert parsers["calculation"].adj is not parsers["control"].adj
        assert parsers["calculation"].adj is not testing.shared
        assert parsers["control"].adj is not testing.shared
        assert parsers["control"].adj.max_request_body_size == http.BODY_LIMIT + 1
        assert testing.shared.max_request_body_size == WIRE + 1
        parsers["calculation"].received(b"A" * 100)
        parsers["control"].received(b"B" * http.BODY_LIMIT)
        parsers["calculation"].received(b"A" * (WIRE - 100))
        assert all(parser.completed and parser.error is None for parser in parsers.values())
        assert all(parser.connection_close is True for parser in parsers.values())
    finally:
        for parser in parsers.values():
            parser.close()


@pytest.mark.parametrize("extra,status", [
    (b"Host: attacker.invalid\r\n", 400),
    (b"Origin: \r\n", 400),
    (b"Origin: null\r\n", 400),
    (b"Sec-Fetch-Site: cross-site\r\n", 400),
    (b"Authorization: Bearer different\r\n", 401),
    (f"X-Attempt-ID: {ATTEMPT}\r\n".encode(), 400),
    (b"Content-Type: application/json\r\nContent-Type: application/json\r\n", 400),
    (b"Content-Encoding: gzip\r\n", 400),
])
def test_actual_waitress_header_collapse_does_not_bypass_wsgi_guards(monkeypatch, extra, status):
    testing = harness(monkeypatch)
    parser = testing.parser(testing.shared)
    try:
        parser.received(header(extra=extra))
        response = testing.invoke(parser)
        assert response["status"] == status
        assert testing.observed == []
    finally:
        parser.close()


@pytest.mark.parametrize("path", ["//cpr-analysis", "/cpr%2danalysis", "/cpr-analysis?",
                                   "/cpr-analysis?token=" + SECRET, "/cpr-analysis#fragment",
                                   "http://127.0.0.1:8000/cpr-analysis"])
def test_waitress_uri_normalization_cannot_turn_a_different_original_uri_into_a_route(monkeypatch, path):
    testing = harness(monkeypatch)
    parser = testing.parser(testing.shared)
    try:
        parser.received(header(path))
        response = testing.invoke(parser)
        assert response["status"] == 400
        assert SECRET.encode() not in response["body"]
        assert testing.observed == []
    finally:
        parser.close()


def test_socket_peer_has_authority_even_when_forwarded_headers_name_the_allowed_peer(monkeypatch):
    testing = harness(monkeypatch, peer="192.168.1.224")
    parser = testing.parser(testing.shared)
    try:
        parser.received(header(extra=b"Forwarded: for=127.0.0.1\r\nX-Forwarded-For: 127.0.0.1\r\n"))
        assert testing.invoke(parser)["status"] == 404
        assert testing.observed == []
    finally:
        parser.close()


@pytest.mark.parametrize("framing", [b"Transfer-Encoding:\r\n", b"Transfer-Encoding: chunked\r\n",
                                     b"Content-Length: 0\r\n", b"Content-Length: 1\r\n"])
def test_ambiguous_framing_is_rejected_before_body_or_wsgi(monkeypatch, framing):
    testing = harness(monkeypatch)
    parser = testing.parser(testing.shared)
    try:
        parser.received(header(extra=framing))
        assert parser.completed and parser.error.code == 400
        assert parser.error.body == "Invalid request headers."
        assert parser.body_bytes_received == 0 and testing.observed == []
    finally:
        parser.close()


def test_declared_large_control_body_cannot_allocate_the_calculation_buffer(monkeypatch):
    testing = harness(monkeypatch)
    parser = testing.parser(testing.shared)
    try:
        data = header("/mock/v1/sessions", size=http.BODY_LIMIT + 1) + b"A" * 100
        consumed = parser.received(data)
        assert consumed < len(data)
        assert parser.completed and parser.error.code == 413
        assert parser.body_bytes_received == 0 and len(parser.body_rcv.getbuf()) == 0
        assert testing.observed == []
    finally:
        parser.close()


class QueryClient(ScriptedClient):
    """Finite expected SDK calls, not an implementation of DynamoDB conditions."""

    def query(self, **kwargs):
        return self._call("query", kwargs)


def test_expired_outbox_ack_is_not_committed_and_next_runner_can_ack_a_terminal_job():
    old = {"PK": "OUTBOX#job", "SK": "DISPATCH", "job_id": "job", "state": "pending",
           "owner": None, "lease_until": 0, "fence": 0, "revision": 0, "delivery_attempts": 0,
           "GSI1PK": "DUE#OUTBOX", "GSI1SK": 100, "next_due_at": 100}
    leased = {**old, "owner": "unused-record-owner", "lease_until": 105,
              "fence": 1, "revision": 1, "delivery_attempts": 1, "GSI1SK": 105, "next_due_at": 105}
    now, executed = [100], []
    # Fixed owner makes the repository ownership predicates observable. This
    # changes only relay owner generation inside this test, not runtime code.
    from unittest.mock import patch

    calls = [
        ("query", {"Items": [item(old)]}), ("get", {"Item": item(old)}),
        ("get", {"Item": item(old)}), ("write", {}),
        ("get", {"Item": item(leased)}), ("get", {"Item": item(leased)}),
        ("query", {"Items": []}),
        ("query", {"Items": [item(leased)]}), ("get", {"Item": item(leased)}),
        ("get", {"Item": item(leased)}), ("write", {}),
        ("get", {"Item": item({**leased, "lease_until": 111, "fence": 2, "revision": 2,
                               "delivery_attempts": 2, "GSI1SK": 111, "next_due_at": 111})}),
        ("write", {}), ("query", {"Items": []}),
    ]
    client = QueryClient(calls)
    jobs = DynamoJobRepository(DynamoStateRepository(client, "test-only-state", clock=lambda: now[0]))

    def terminal_process(job_id):
        executed.append(job_id)
        now[0] = 106  # First execution outlives the five-second relay lease.
        return True

    runner = LocalJobRunner(jobs, SimpleNamespace(process=terminal_process), lease_seconds=5,
                            retry_seconds=2, page_size=1, max_pages=1, clock=lambda: now[0])
    with patch("mock_journey.dispatch.uuid.uuid4", return_value="unused-record-owner"):
        assert runner.run_once() == {"outbox_wakes": 0, "job_wakes": 0, "failures": 1}
        assert len([call for call in client.calls if call[0] == "write"]) == 1
        assert runner.run_once() == {"outbox_wakes": 1, "job_wakes": 0, "failures": 0}
    assert executed == ["job", "job"] and client.expected == []
    ack = [call[1] for call in client.calls if call[0] == "write"][-1]["TransactItems"][0]["Put"]
    assert ack["Item"]["state"] == {"S": "sent"}
    assert "#o = :owner AND #f = :fence AND #l > :now" in ack["ConditionExpression"]


def test_due_job_query_rechecks_authoritative_rows_and_runs_without_any_outbox():
    index = {"PK": "JOB#job", "SK": "STATE", "job_id": "job",
             "GSI1PK": "DUE#JOB", "GSI1SK": 100, "next_due_at": 100, "lease_until": 0}
    calls = [
        ("query", {"Items": []}),
        ("query", {"Items": [item(index)]}), ("get", {"Item": item({**index, "lease_until": 101})}),
        ("query", {"Items": []}),
        ("query", {"Items": [item(index)]}), ("get", {"Item": item(index)}),
    ]
    client, executed = QueryClient(calls), []
    jobs = DynamoJobRepository(DynamoStateRepository(client, "test-only-state", clock=lambda: 100))
    worker = SimpleNamespace(process=lambda job: executed.append(job) or True)
    runner = LocalJobRunner(jobs, worker, lease_seconds=5, retry_seconds=2,
                            page_size=1, max_pages=1, clock=lambda: 100)
    assert runner.run_once() == {"outbox_wakes": 0, "job_wakes": 0, "failures": 0}
    assert executed == []
    assert runner.run_once() == {"outbox_wakes": 0, "job_wakes": 1, "failures": 0}
    assert executed == ["job"] and client.expected == []
    assert all(call[1]["ConsistentRead"] is True for call in client.calls if call[0] == "get")

"""Direct WSGI characterization of the local guard order (no sockets, no DB).

Each case combines two violations whose public codes differ, so the returned
code proves which guard runs first. Expected codes/statuses are written out
literally from the transport contract, not read back from the adapter.
"""

import io
import json
from types import SimpleNamespace
import uuid

import pytest

from local_server import http as local_http
from local_server import object_storage as objects
from local_server.charts import LocalChartService
from local_server.database import prepare_material


HOST, PORT = "127.0.0.1", 8123
AUTHORITY = f"{HOST}:{PORT}"
CALC_LIMIT = 1000
CALC_PATH = "/api/v2/attempts/a5917021-0db0-45ed-9c8c-97d31e43a652/calculation/"
DROP = object()
STATUS = {"NOT_FOUND": "404 Not Found", "INVALID_REQUEST": "400 Bad Request",
          "PAYLOAD_TOO_LARGE": "413 Request Entity Too Large", "SESSION_REQUIRED": "401 Unauthorized",
          "TEMPORARILY_UNAVAILABLE": "503 Service Unavailable"}
MESSAGE = {"NOT_FOUND": "Not found.", "INVALID_REQUEST": "Invalid request.",
           "PAYLOAD_TOO_LARGE": "The request exceeds the verified payload limit.",
           "SESSION_REQUIRED": "A valid session is required.",
           "TEMPORARILY_UNAVAILABLE": "The service is temporarily unavailable."}


class Service:
    course_mode = "course_v2"

    def __init__(self, payload_limit=None, operations=None):
        self.course_http = object()
        self.calculation = SimpleNamespace(payload_limit=payload_limit)
        self.operations = operations


class FailingInput:
    def read(self, *args):
        pytest.fail("A rejected request body was read.")


class RecordingEnviron(dict):
    """Records every environ key lookup, pinning the guard read sequence."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.seen = []

    def get(self, key, default=None):
        self.seen.append(key)
        return super().get(key, default)

    def __getitem__(self, key):
        self.seen.append(key)
        return super().__getitem__(key)

    def __contains__(self, key):
        self.seen.append(key)
        return super().__contains__(key)


@pytest.fixture
def dispatched(monkeypatch):
    events = []

    def handle(event, context, service):
        events.append((event, context, service))
        return {"statusCode": 200, "body": '{"ok": true}'}
    monkeypatch.setattr(local_http, "handle", handle)
    return events


@pytest.fixture
def chart_service(tmp_path):
    root = tmp_path.resolve() / "installation"
    root.mkdir(mode=0o700)
    material = objects.prepare_object_material(prepare_material(root))
    client = objects.LocalObjectClient(material, bucket="local-objects",
                                       directory="calculator_result/interpreted_rtdata/arc", stage="local-test",
                                       artifact_limit=50_000, quota_bytes=2_000_000)
    yield LocalChartService(client, base_url=f"http://{AUTHORITY}", clock=lambda: 1_700_000_000)
    client.close()


def environ(body=b"{}", **overrides):
    value = {"REMOTE_ADDR": HOST, "HTTP_HOST": AUTHORITY, "PATH_INFO": "/api/v2/sessions/",
             "REQUEST_URI": "/api/v2/sessions/", "QUERY_STRING": "", "REQUEST_METHOD": "POST",
             "CONTENT_LENGTH": str(len(body)), "CONTENT_TYPE": "application/json",
             "wsgi.input": io.BytesIO(body)}
    for key, item in overrides.items():
        if item is DROP:
            value.pop(key, None)
        else:
            value[key] = item
    return value


def invoke(application, env):
    calls = []

    def start_response(status, headers):
        calls.append((status, headers))
    body = b"".join(application(env, start_response))
    assert len(calls) == 1
    return calls[0][0], calls[0][1], body


def assert_error(result, code):
    status, headers, body = result
    assert status == STATUS[code]
    assert headers == [("Content-Type", "application/json"), ("Cache-Control", "no-store"),
                       ("X-Content-Type-Options", "nosniff"), ("Content-Length", str(len(body)))]
    value = json.loads(body)
    assert list(value) == ["error"] and list(value["error"]) == ["code", "message", "request_id"]
    assert value["error"]["code"] == code and value["error"]["message"] == MESSAGE[code]
    assert str(uuid.UUID(value["error"]["request_id"])) == value["error"]["request_id"]
    return value


def control_app(ready=lambda: True, **kwargs):
    return local_http.make_application(Service(**kwargs), ready, HOST, PORT, [HOST])


def journey_app(chart_service, *, execution_ready=lambda: True, ready=lambda: True, response_body_limit=None,
                operations=None):
    return local_http.make_application(
        Service(payload_limit=4 * ((CALC_LIMIT + 2) // 3), operations=operations), ready, HOST, PORT, [HOST],
        calculation_body_limit=CALC_LIMIT, chart_service=chart_service,
        response_body_limit=response_body_limit or 50_000 + local_http.BODY_LIMIT,
        execution_ready=execution_ready)


# (environ overrides, expected code) for the control (non-journey) adapter.
CONTROL_ORDER = [
    # Peer first: its rejection wins over every later guard and reads no body.
    ({"REMOTE_ADDR": "10.0.0.9", "HTTP_HOST": "evil", "REQUEST_METHOD": "PATCH", "wsgi.input": FailingInput()},
     "NOT_FOUND"),
    ({"REMOTE_ADDR": DROP, "wsgi.input": FailingInput()}, "NOT_FOUND"),
    # Host before method/length/auth.
    ({"HTTP_HOST": "evil:1", "REQUEST_METHOD": "PATCH"}, "INVALID_REQUEST"),
    ({"HTTP_HOST": DROP, "CONTENT_LENGTH": "99999"}, "INVALID_REQUEST"),
    ({"HTTP_HOST": f"{AUTHORITY},{AUTHORITY}", "HTTP_AUTHORIZATION": "a,b"}, "INVALID_REQUEST"),
    # Origin / fetch metadata before path, method and framing.
    ({"HTTP_ORIGIN": "http://x", "REQUEST_METHOD": "PATCH"}, "INVALID_REQUEST"),
    ({"HTTP_SEC_FETCH_SITE": "cross-site", "CONTENT_LENGTH": "99999"}, "INVALID_REQUEST"),
    # Raw target before method.
    ({"PATH_INFO": "//api/v2/sessions/", "REQUEST_URI": "//api/v2/sessions/", "REQUEST_METHOD": "PATCH"},
     "INVALID_REQUEST"),
    ({"PATH_INFO": "/api/v2/%73essions/", "REQUEST_METHOD": "PATCH"}, "INVALID_REQUEST"),
    ({"PATH_INFO": "/api/v2/sessions/\x7f", "REQUEST_METHOD": "PATCH"}, "INVALID_REQUEST"),
    ({"PATH_INFO": 7, "REQUEST_METHOD": "PATCH"}, "INVALID_REQUEST"),
    ({"QUERY_STRING": "a=1", "PATH_INFO": "/healthz", "REQUEST_URI": "/healthz?a=1", "REQUEST_METHOD": "PATCH"},
     "INVALID_REQUEST"),
    ({"QUERY_STRING": "a=%31", "REQUEST_URI": "/api/v2/sessions/?a=%31", "REQUEST_METHOD": "PATCH"},
     "INVALID_REQUEST"),
    ({"QUERY_STRING": "a=1", "REQUEST_URI": "/api/v2/other/?a=1", "REQUEST_METHOD": "PATCH"}, "INVALID_REQUEST"),
    ({"REQUEST_URI": "/api/v2/sessions/x", "REQUEST_METHOD": "PATCH"}, "INVALID_REQUEST"),
    ({"REQUEST_URI": DROP, "REQUEST_METHOD": "PATCH"}, "INVALID_REQUEST"),
    # Method before framing.
    ({"REQUEST_METHOD": "PATCH", "HTTP_TRANSFER_ENCODING": "chunked"}, "NOT_FOUND"),
    ({"REQUEST_METHOD": DROP, "CONTENT_LENGTH": "x"}, "NOT_FOUND"),
    # Transfer/content encoding before length.
    ({"HTTP_TRANSFER_ENCODING": "chunked", "CONTENT_LENGTH": "99999"}, "INVALID_REQUEST"),
    ({"HTTP_CONTENT_ENCODING": "gzip", "CONTENT_LENGTH": "99999"}, "INVALID_REQUEST"),
    # Length syntax, then size, then GET/DELETE body, then content type.
    ({"CONTENT_LENGTH": "12a"}, "INVALID_REQUEST"),
    ({"CONTENT_LENGTH": "0000002"}, "INVALID_REQUEST"),
    ({"CONTENT_LENGTH": "２"}, "INVALID_REQUEST"),
    ({"CONTENT_LENGTH": "16385", "CONTENT_TYPE": "a,b"}, "PAYLOAD_TOO_LARGE"),
    ({"CONTENT_LENGTH": "16385", "REQUEST_METHOD": "GET"}, "PAYLOAD_TOO_LARGE"),
    ({"REQUEST_METHOD": "GET", "CONTENT_TYPE": "a,b"}, "INVALID_REQUEST"),
    ({"REQUEST_METHOD": "DELETE", "HTTP_AUTHORIZATION": "a,b"}, "INVALID_REQUEST"),
    # Content type before authorization.
    ({"CONTENT_TYPE": "a,b", "HTTP_AUTHORIZATION": "a,b"}, "INVALID_REQUEST"),
    ({"CONTENT_TYPE": "text/plain", "HTTP_AUTHORIZATION": "a,b"}, "INVALID_REQUEST"),
    ({"CONTENT_TYPE": "application/json; charset=latin-1"}, "INVALID_REQUEST"),
    # Authorization before body read.
    ({"HTTP_AUTHORIZATION": "Bearer a,b", "wsgi.input": FailingInput()}, "SESSION_REQUIRED"),
    # Short body, then UTF-8 decoding.
    ({"CONTENT_LENGTH": "5"}, "INVALID_REQUEST"),
    ({"wsgi.input": io.BytesIO(b"\xff\xfe"), "CONTENT_LENGTH": "2"}, "INVALID_REQUEST"),
    # Query parsing runs only after the body and status routes.
    ({"QUERY_STRING": "a", "REQUEST_URI": "/api/v2/sessions/?a"}, "INVALID_REQUEST"),
    ({"QUERY_STRING": "a=1&a=2", "REQUEST_URI": "/api/v2/sessions/?a=1&a=2"}, "INVALID_REQUEST"),
]


@pytest.mark.parametrize("overrides,code", CONTROL_ORDER)
def test_control_guard_order_is_fixed(dispatched, overrides, code):
    assert_error(invoke(control_app(), environ(**overrides)), code)
    assert dispatched == []


def test_peer_rejection_is_a_direct_response_without_other_reads(dispatched):
    env = RecordingEnviron(environ(REMOTE_ADDR="192.168.0.9"))
    env["wsgi.input"] = FailingInput()
    ready_calls = []
    application = local_http.make_application(Service(), lambda: ready_calls.append(1), HOST, PORT, [HOST])
    value = assert_error(invoke(application, env), "NOT_FOUND")
    assert env.seen == ["REMOTE_ADDR"] and ready_calls == [] and dispatched == []
    assert value["error"]["code"] == "NOT_FOUND"


def test_successful_request_reads_environ_in_guard_order(dispatched):
    env = RecordingEnviron(environ(HTTP_AUTHORIZATION="Bearer token", QUERY_STRING="a=1",
                                   REQUEST_URI="/api/v2/sessions/?a=1"))
    status, headers, body = invoke(control_app(), env)
    assert status == "200 OK" and body == b'{"ok": true}'
    assert env.seen == [
        "REMOTE_ADDR", "HTTP_HOST", "HTTP_ORIGIN", "HTTP_SEC_FETCH_SITE", "PATH_INFO", "REQUEST_URI",
        "QUERY_STRING", "REQUEST_METHOD", "HTTP_CONTENT_ENCODING", "HTTP_TRANSFER_ENCODING", "CONTENT_LENGTH",
        "CONTENT_TYPE", "HTTP_AUTHORIZATION", "wsgi.input",
    ]
    ((event, context, service),) = dispatched
    assert list(event) == ["httpMethod", "path", "headers", "multiValueHeaders", "queryStringParameters",
                           "multiValueQueryStringParameters", "body", "isBase64Encoded"]
    assert event == {
        "httpMethod": "POST", "path": "/api/v2/sessions/",
        "headers": {"Host": AUTHORITY, "Content-Type": "application/json", "Authorization": "Bearer token"},
        "multiValueHeaders": {"Host": [AUTHORITY], "Content-Type": ["application/json"],
                              "Authorization": ["Bearer token"]},
        "queryStringParameters": {"a": "1"}, "multiValueQueryStringParameters": {"a": ["1"]},
        "body": "{}", "isBase64Encoded": False,
    }
    assert list(event["headers"]) == ["Host", "Content-Type", "Authorization"]
    assert str(uuid.UUID(context.aws_request_id)) == context.aws_request_id
    assert type(service) is Service


def test_host_is_case_insensitive_and_localhost_alias_is_loopback_only(dispatched):
    for host in (AUTHORITY.upper(), f"localhost:{PORT}", f"LOCALHOST:{PORT}"):
        status, _, _ = invoke(control_app(), environ(HTTP_HOST=host))
        assert status == "200 OK"
    assert [event["headers"]["Host"] for event, _, _ in dispatched] == [
        AUTHORITY.upper(), f"localhost:{PORT}", f"LOCALHOST:{PORT}"]
    lan = local_http.make_application(Service(), lambda: True, "192.168.0.2", PORT, ["192.168.0.3"])
    assert_error(invoke(lan, environ(REMOTE_ADDR="192.168.0.3", HTTP_HOST=f"localhost:{PORT}")),
                 "INVALID_REQUEST")
    status, _, _ = invoke(lan, environ(REMOTE_ADDR="192.168.0.3", HTTP_HOST=f"192.168.0.2:{PORT}"))
    assert status == "200 OK"


def test_same_origin_fetch_and_bare_request_uri_with_query_are_admitted(dispatched):
    for overrides in ({"HTTP_SEC_FETCH_SITE": "same-origin"}, {"HTTP_SEC_FETCH_SITE": "none"},
                      {"QUERY_STRING": "a=1", "REQUEST_URI": "/api/v2/sessions/"},
                      {"CONTENT_TYPE": " Application/JSON ; Charset=UTF-8 "}):
        status, _, _ = invoke(control_app(), environ(**overrides))
        assert status == "200 OK"
    assert len(dispatched) == 4


def test_handler_failures_are_generic(monkeypatch):
    for result in ({"statusCode": "200", "body": "{}"}, {"statusCode": 200, "body": b"{}"}):
        monkeypatch.setattr(local_http, "handle", lambda *args, value=result: value)
        assert_error(invoke(control_app(), environ()), "TEMPORARILY_UNAVAILABLE")

    def explode(*args):
        raise RuntimeError("PRIVATE-DETAIL")
    monkeypatch.setattr(local_http, "handle", explode)
    status, _, body = invoke(control_app(), environ())
    assert status == STATUS["TEMPORARILY_UNAVAILABLE"] and b"PRIVATE" not in body


def test_status_routes_keep_their_ready_and_key_order_rules(dispatched):
    not_ready = control_app(ready=lambda: False)
    get = dict(REQUEST_METHOD="GET", CONTENT_LENGTH=DROP, CONTENT_TYPE=DROP, body=b"")
    assert_error(invoke(not_ready, environ(PATH_INFO="/healthz", REQUEST_URI="/healthz", **get)),
                 "TEMPORARILY_UNAVAILABLE")
    # "/" never consults DB readiness.
    status, _, body = invoke(not_ready, environ(PATH_INFO="/", REQUEST_URI="/", **get))
    assert status == "200 OK"
    assert body == (b'{"service": "arc-local-api", "mode": "course_v2", "calculator_available": false, '
                    b'"login_path": "/api/v2/sessions/", "programs_path": "/api/v2/courses/progress/"}')
    # POST to a status path is an ordinary routed request.
    invoke(control_app(), environ(PATH_INFO="/healthz", REQUEST_URI="/healthz"))
    assert [event["path"] for event, _, _ in dispatched] == ["/healthz"]


class Recorder:
    def __init__(self, value):
        self.value = value

    def status(self):
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


@pytest.mark.parametrize("operations,logs", [
    (None, None),
    (Recorder({"scope": "api_process", "running": True}), {"scope": "api_process", "running": True}),
    (Recorder(RuntimeError("PRIVATE")), {"scope": "api_process", "running": False}),
])
def test_supervised_health_body_is_exact(chart_service, dispatched, operations, logs):
    application = journey_app(chart_service, operations=operations)
    get = dict(REQUEST_METHOD="GET", CONTENT_LENGTH="", CONTENT_TYPE="", body=b"")
    status, _, body = invoke(application, environ(PATH_INFO="/healthz", REQUEST_URI="/healthz", **get))
    assert status == "200 OK"
    expected = ('{"service": "arc-local-api", "mode": "course_v2", "calculator_available": true, '
                '"login_path": "/api/v2/sessions/", "programs_path": "/api/v2/courses/progress/", '
                '"calculation_transport_configured": true, "program_target_combinations": 15, '
                '"completion_policy": {"cycles": "evaluated", "compressions": "evaluated", '
                '"ventilations": "evaluated"}')
    if logs is not None:
        expected += ', "operational_logs": ' + json.dumps(logs)
    assert body == (expected + "}").encode()


def test_completion_policy_is_derived_from_catalog_goal_kinds_and_adapter_status():
    from mock_journey.catalog import PROGRAMS
    from mock_journey.contracts import CURRENT_ADAPTER_VERSION, expected_goal_status
    kinds = [program[2] for program in PROGRAMS]
    assert set(kinds) == {"cycles", "compressions", "ventilations"} and len(kinds) == 5
    # First-seen catalog order and the current adapter's status per kind (D13, D136).
    assert [kind for kind, _ in local_http._COMPLETION_POLICY] == ["cycles", "compressions", "ventilations"]
    assert dict(local_http._COMPLETION_POLICY) == {kind: expected_goal_status(kind, CURRENT_ADAPTER_VERSION)
                                                  for kind in kinds}
    assert dict(local_http._COMPLETION_POLICY) == {"cycles": "evaluated", "compressions": "evaluated",
                                                  "ventilations": "evaluated"}


def test_calculation_only_transport_health_body(dispatched):
    application = local_http.make_application(
        Service(payload_limit=4 * ((CALC_LIMIT + 2) // 3)), lambda: True, HOST, PORT, [HOST],
        calculation_body_limit=CALC_LIMIT)
    get = dict(REQUEST_METHOD="GET", CONTENT_LENGTH=DROP, CONTENT_TYPE=DROP, body=b"")
    _, _, body = invoke(application, environ(PATH_INFO="/", REQUEST_URI="/", **get))
    assert list(json.loads(body)) == ["service", "mode", "calculator_available", "login_path", "programs_path",
                                      "calculation_transport_configured"]
    assert json.loads(body)["calculator_available"] is False


JOURNEY_ORDER = [
    # Short body is detected before the supervised execution check.
    (dict(CONTENT_LENGTH="5"), lambda: False, "INVALID_REQUEST"),
    # Execution readiness before chart and status routes and before decoding.
    (dict(PATH_INFO="/local/v1/charts/x", REQUEST_URI="/local/v1/charts/x", REQUEST_METHOD="POST"),
     lambda: False, "TEMPORARILY_UNAVAILABLE"),
    (dict(wsgi_bytes=b"\xff\xfe"), lambda: False, "TEMPORARILY_UNAVAILABLE"),
    (dict(wsgi_bytes=b"\xff\xfe"), lambda: 1, "TEMPORARILY_UNAVAILABLE"),
    # Chart route method/range checks run before body decoding.
    (dict(PATH_INFO="/local/v1/charts/x", REQUEST_URI="/local/v1/charts/x", wsgi_bytes=b"\xff\xfe"),
     lambda: True, "NOT_FOUND"),
    (dict(PATH_INFO="/local/v1/charts/x", REQUEST_URI="/local/v1/charts/x", REQUEST_METHOD="GET",
          HTTP_RANGE="bytes=0-1", CONTENT_LENGTH="0", wsgi_bytes=b""), lambda: True, "NOT_FOUND"),
    (dict(PATH_INFO="/local/v1/charts/x", REQUEST_URI="/local/v1/charts/x", REQUEST_METHOD="GET",
          CONTENT_LENGTH="0", wsgi_bytes=b""), lambda: True, "NOT_FOUND"),
    # The calculation route uses its own limit; control routes keep 16 KiB.
    (dict(PATH_INFO=CALC_PATH, REQUEST_URI=CALC_PATH, CONTENT_LENGTH="1001", CONTENT_TYPE="a,b"),
     lambda: True, "PAYLOAD_TOO_LARGE"),
    (dict(PATH_INFO=CALC_PATH, REQUEST_URI=CALC_PATH, CONTENT_LENGTH="0000001"), lambda: True, "INVALID_REQUEST"),
    (dict(PATH_INFO=CALC_PATH, REQUEST_URI=CALC_PATH, REQUEST_METHOD="PUT", CONTENT_LENGTH="1001"),
     lambda: True, "INVALID_REQUEST"),
    (dict(PATH_INFO=CALC_PATH, REQUEST_URI=CALC_PATH, REQUEST_METHOD="PUT", CONTENT_LENGTH="16385"),
     lambda: True, "PAYLOAD_TOO_LARGE"),
]


@pytest.mark.parametrize("overrides,execution,code", JOURNEY_ORDER)
def test_journey_guard_order_is_fixed(chart_service, dispatched, overrides, execution, code):
    overrides = dict(overrides)
    raw = overrides.pop("wsgi_bytes", None)
    env = environ(**overrides)
    if raw is not None:
        env["wsgi.input"] = io.BytesIO(raw)
        env.setdefault("CONTENT_LENGTH", str(len(raw)))
        if "CONTENT_LENGTH" not in overrides:
            env["CONTENT_LENGTH"] = str(len(raw))
    assert_error(invoke(journey_app(chart_service, execution_ready=execution), env), code)
    assert dispatched == []


@pytest.mark.parametrize("content_type,encoded", [
    ("multipart/form-data; boundary=x", True), ("Multipart/Form-Data; boundary=x", True),
    ("application/octet-stream", False), ("", False),
])
def test_calculation_body_encoding_rule(chart_service, dispatched, content_type, encoded):
    raw = b"--x\r\nA\r\n--x--\r\n"
    invoke(journey_app(chart_service), environ(body=raw, PATH_INFO=CALC_PATH, REQUEST_URI=CALC_PATH,
                                               CONTENT_TYPE=content_type))
    ((event, _, _),) = dispatched
    assert event["isBase64Encoded"] is encoded
    assert event["body"] == ("LS14DQpBDQotLXgtLQ0K" if encoded else raw.decode())
    assert ("Content-Type" in event["headers"]) is bool(content_type)


def test_calculation_non_utf8_without_multipart_is_invalid(chart_service, dispatched):
    env = environ(body=b"\xff", PATH_INFO=CALC_PATH, REQUEST_URI=CALC_PATH, CONTENT_TYPE="application/json")
    assert_error(invoke(journey_app(chart_service), env), "INVALID_REQUEST")


def test_oversized_response_is_replaced_with_a_new_request_id(chart_service, monkeypatch):
    seen = []
    size = [local_http.BODY_LIMIT + 50_001]

    def handle(event, context, service):
        seen.append(context.aws_request_id)
        return {"statusCode": 200, "body": "x" * size[0]}
    monkeypatch.setattr(local_http, "handle", handle)
    application = journey_app(chart_service, response_body_limit=local_http.BODY_LIMIT + 50_000)
    value = assert_error(invoke(application, environ()), "TEMPORARILY_UNAVAILABLE")
    assert len(seen) == 1 and value["error"]["request_id"] != seen[0]
    size[0] = local_http.BODY_LIMIT + 50_000
    status, _, body = invoke(application, environ())
    assert status == "200 OK" and len(body) == local_http.BODY_LIMIT + 50_000


def test_peer_rejection_is_also_bounded_by_send(chart_service, dispatched):
    application = journey_app(chart_service, execution_ready=lambda: pytest.fail("peer checked after execution"))
    env = environ(REMOTE_ADDR="127.0.0.2")
    env["wsgi.input"] = FailingInput()
    assert_error(invoke(application, env), "NOT_FOUND")


@pytest.mark.parametrize("payload_delta,accepted", [(0, True), (-1, False), (1, True)])
def test_encoded_wire_limit_relation(payload_delta, accepted):
    # 4 * ceil(1000 / 3) = 1336, written independently of the adapter.
    service = Service(payload_limit=1336 + payload_delta)
    build = lambda: local_http.make_application(service, lambda: True, HOST, PORT, [HOST],
                                                calculation_body_limit=CALC_LIMIT)
    if accepted:
        assert callable(build())
    else:
        with pytest.raises(ValueError, match="^The calculation service must admit the encoded wire-body limit.$"):
            build()


@pytest.mark.parametrize("kwargs,message", [
    (dict(calculation_body_limit=0), "An explicit positive calculation wire-body limit is required."),
    (dict(calculation_body_limit=True), "An explicit positive calculation wire-body limit is required."),
    (dict(response_body_limit=local_http.BODY_LIMIT - 1), "An explicit local response-body limit is required."),
    (dict(response_body_limit=float(local_http.BODY_LIMIT)), "An explicit local response-body limit is required."),
    (dict(execution_ready=lambda: True), "A supervised local journey requires calculation and chart transport."),
    (dict(calculation_body_limit=CALC_LIMIT), "The calculation service must admit the encoded wire-body limit."),
])
def test_factory_configuration_messages(kwargs, message):
    with pytest.raises(ValueError) as error:
        local_http.make_application(Service(), lambda: True, HOST, PORT, [HOST], **kwargs)
    assert str(error.value) == message


@pytest.mark.parametrize("args,message", [
    ((Service(), lambda: True, "0.0.0.0", PORT, [HOST]), "A literal local IPv4 address is required."),
    ((Service(), lambda: True, HOST, 0, [HOST]), "A valid local TCP port is required."),
    ((Service(), None, HOST, PORT, [HOST]), "Invalid local application configuration."),
    ((Service(), lambda: True, HOST, PORT, HOST), "Invalid local application configuration."),
    ((Service(), lambda: True, HOST, PORT, []), "At least one explicit local client is required."),
    ((Service(), lambda: True, HOST, PORT, [HOST, "10.0.0.2"]), "Loopback mode accepts only the loopback client."),
    ((SimpleNamespace(course_mode="course_v2", course_http=None), lambda: True, HOST, PORT, [HOST]),
     "The local server requires the assembled /api/v2 course application."),
])
def test_factory_argument_messages(args, message):
    with pytest.raises(ValueError) as error:
        local_http.make_application(*args)
    assert str(error.value) == message


def test_chart_transport_configuration_message(chart_service):
    for kwargs in (dict(response_body_limit=None), dict(response_body_limit=50_000 + local_http.BODY_LIMIT - 1)):
        with pytest.raises(ValueError, match="^Invalid local chart transport configuration.$"):
            local_http.make_application(Service(), lambda: True, HOST, PORT, [HOST],
                                        chart_service=chart_service, **kwargs)
    with pytest.raises(ValueError, match="^Invalid local chart transport configuration.$"):
        local_http.make_application(Service(), lambda: True, HOST, PORT + 1, [HOST], chart_service=chart_service,
                                    response_body_limit=50_000 + local_http.BODY_LIMIT)


def test_create_server_rechecks_attached_response_limit():
    def application(environ, start_response):
        return []
    application._local_response_body_limit = local_http.BODY_LIMIT - 1
    with pytest.raises(ValueError, match="^An explicit local response-body limit is required.$"):
        local_http.create_server(application, HOST, PORT)
    application._local_response_body_limit = None
    application._local_calculation_body_limit = -1
    with pytest.raises(ValueError, match="^An explicit positive calculation wire-body limit is required.$"):
        local_http.create_server(application, HOST, PORT)

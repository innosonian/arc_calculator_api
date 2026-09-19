"""Restricted local HTTP transport over the authenticated journey handler.

Calculation transport is enabled only by an explicit wire-body limit and an
assembled calculation service; the default remains a local control API.
The listener must be created through ``create_server`` so the pinned parser
guard is active before any request is accepted by the event loop.
"""

from http import HTTPStatus
from importlib.metadata import version
import base64
from copy import copy
import ipaddress
import json
import logging
import re
from types import SimpleNamespace
import uuid

from mock_journey.errors import JourneyError
from mock_journey.handler import handle


BODY_LIMIT = 16 * 1024
HEADER_LIMIT = 16 * 1024
WAITRESS_VERSION = "3.0.2"
_PRIVATE_NETWORKS = tuple(ipaddress.IPv4Network(value) for value in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
))
_STATUS_BODY = {
    "service": "arc-local-api", "mode": "control_only",
    "calculator_available": False,
    "login_path": "/mock/v1/sessions", "programs_path": "/mock/v1/programs",
}
_CALCULATION_PATH = re.compile(
    r"/mock/v1/attempts/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/calculation\Z"
)
_V2_CALCULATION_PATH = re.compile(
    r"/api/v2/attempts/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/calculation/\Z"
)


def _path_only(path):
    if type(path) is not str:
        return path
    return path.split("?", 1)[0]


def _calculation_route(method, path, *, course_v2=False):
    path = _path_only(path)
    if method != "POST" or type(path) is not str:
        return False
    if course_v2:
        return _V2_CALCULATION_PATH.fullmatch(path) is not None
    return path == "/cpr-analysis" or _CALCULATION_PATH.fullmatch(path) is not None


def _parse_query(query_string):
    if query_string == "":
        return {}, {}
    params = {}
    multi = {}
    for part in query_string.split("&"):
        if "=" not in part:
            raise JourneyError("INVALID_REQUEST")
        key, _, value = part.partition("=")
        if not key or not value or key in params:
            raise JourneyError("INVALID_REQUEST")
        if any(char in key or char in value for char in "%+#\\"):
            raise JourneyError("INVALID_REQUEST")
        params[key] = value
        multi[key] = [value]
    return params, multi


def _body_limit(value):
    if value is not None and (type(value) is not int or value <= 0):
        raise ValueError("An explicit positive calculation wire-body limit is required.")
    return value


def _address(value):
    if type(value) is not str:
        raise ValueError("A literal local IPv4 address is required.")
    try:
        address = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError:
        raise ValueError("A literal local IPv4 address is required.") from None
    if str(address) != value or not (
        address == ipaddress.IPv4Address("127.0.0.1")
        or any(address in network for network in _PRIVATE_NETWORKS)
    ):
        raise ValueError("A literal local IPv4 address is required.")
    return value


def _port(value):
    if type(value) is not int or not 1 <= value <= 65535:
        raise ValueError("A valid local TCP port is required.")
    return value


def _public_error(code, request_id):
    error = JourneyError(code)
    return error.status, json.dumps({"error": {
        "code": error.code, "message": error.message,
        "request_id": request_id,
    }}, allow_nan=False).encode("utf-8")


def _send(start_response, status, body):
    headers = [
        ("Content-Type", "application/json"),
        ("Cache-Control", "no-store"),
        ("X-Content-Type-Options", "nosniff"),
    ]
    if status != 204:
        headers.append(("Content-Length", str(len(body))))
    start_response(f"{status} {HTTPStatus(status).phrase}", headers)
    return [body] if body else []


def make_application(service, ready, host, port, allowed_clients, *, calculation_body_limit=None,
                     chart_service=None, response_body_limit=None, execution_ready=None):
    """Build a WSGI adapter; ``ready`` returns True only for a healthy DB.

    Network exposure acknowledgment belongs to the CLI. This constructor also
    rejects nonliteral/nonlocal addresses, empty allowlists and wildcard hosts.
    ``calculation_body_limit`` counts received HTTP bytes, including multipart
    boundaries. The separate API payload limit counts the encoded event body;
    it must admit this entire wire limit after base64 encoding. This option
    starts no worker and does not discover a catalog, storage, or runtime.
    """
    host, port = _address(host), _port(port)
    calculation_body_limit = _body_limit(calculation_body_limit)
    if response_body_limit is not None and (
            type(response_body_limit) is not int or response_body_limit < BODY_LIMIT):
        raise ValueError("An explicit local response-body limit is required.")
    if chart_service is not None:
        from local_server.charts import LocalChartService

        if (type(chart_service) is not LocalChartService
                or chart_service.base_url != f"http://{host}:{port}"
                or response_body_limit is None
                or response_body_limit < chart_service.artifact_limit + BODY_LIMIT):
            raise ValueError("Invalid local chart transport configuration.")
    if execution_ready is not None and (
            not callable(execution_ready) or chart_service is None or calculation_body_limit is None):
        raise ValueError("A supervised local journey requires calculation and chart transport.")
    if calculation_body_limit is not None:
        payload_limit = getattr(getattr(service, "calculation", None), "payload_limit", None)
        if (type(payload_limit) is not int
                or payload_limit < 4 * ((calculation_body_limit + 2) // 3)):
            raise ValueError("The calculation service must admit the encoded wire-body limit.")
    if not callable(ready) or isinstance(allowed_clients, (str, bytes)):
        raise ValueError("Invalid local application configuration.")
    clients = frozenset(_address(value) for value in allowed_clients)
    if not clients:
        raise ValueError("At least one explicit local client is required.")
    if host == "127.0.0.1" and clients != {"127.0.0.1"}:
        raise ValueError("Loopback mode accepts only the loopback client.")
    authorities = {f"{host}:{port}"}
    if host == "127.0.0.1":
        authorities.add(f"localhost:{port}")

    def send(start_response, status, body):
        # Bound each complete application body before handing it to Waitress.
        # The listener admits this bound plus headers without /tmp spooling.
        if response_body_limit is not None and len(body) > response_body_limit:
            status, body = _public_error("TEMPORARILY_UNAVAILABLE", str(uuid.uuid4()))
        return _send(start_response, status, body)

    def application(environ, start_response):
        request_id = str(uuid.uuid4())
        try:
            # REMOTE_ADDR is supplied by the direct socket. Forwarded headers
            # have no authority here, even if an upstream caller includes them.
            if environ.get("REMOTE_ADDR") not in clients:
                return send(start_response, *_public_error("NOT_FOUND", request_id))
            authority = environ.get("HTTP_HOST")
            if (type(authority) is not str or "," in authority
                    or authority.lower() not in authorities):
                raise JourneyError("INVALID_REQUEST")
            if ("HTTP_ORIGIN" in environ
                    or environ.get("HTTP_SEC_FETCH_SITE", "none") not in ("none", "same-origin")):
                raise JourneyError("INVALID_REQUEST")
            course_v2 = getattr(service, "course_mode", None) == "course_v2"
            path_info = environ.get("PATH_INFO")
            request_uri = environ.get("REQUEST_URI")
            query_string = environ.get("QUERY_STRING") or ""
            if type(path_info) is not str or not path_info.startswith("/") or "//" in path_info:
                raise JourneyError("INVALID_REQUEST")
            if (any(char in path_info for char in "%?#\\")
                    or any(ord(char) < 33 or ord(char) > 126 for char in path_info)):
                raise JourneyError("INVALID_REQUEST")
            if query_string:
                if not course_v2:
                    raise JourneyError("INVALID_REQUEST")
                if (any(char in query_string for char in "%?#\\")
                        or any(ord(char) < 33 or ord(char) > 126 for char in query_string)):
                    raise JourneyError("INVALID_REQUEST")
                expected = path_info + "?" + query_string
                if type(request_uri) is not str or request_uri not in (expected, path_info):
                    raise JourneyError("INVALID_REQUEST")
            elif type(request_uri) is not str or request_uri != path_info:
                raise JourneyError("INVALID_REQUEST")
            raw_path = path_info
            method = environ.get("REQUEST_METHOD")
            allowed_methods = ("GET", "POST", "DELETE", "PUT") if course_v2 else ("GET", "POST", "DELETE")
            if method not in allowed_methods:
                raise JourneyError("NOT_FOUND")
            if "HTTP_CONTENT_ENCODING" in environ or "HTTP_TRANSFER_ENCODING" in environ:
                raise JourneyError("INVALID_REQUEST")
            calculation = (calculation_body_limit is not None
                           and _calculation_route(method, raw_path, course_v2=course_v2))
            limit = calculation_body_limit if calculation else BODY_LIMIT
            length_text = environ.get("CONTENT_LENGTH", "0") or "0"
            if (type(length_text) is not str or not length_text.isascii()
                    or not length_text.isdecimal() or len(length_text) > max(6, len(str(limit)))):
                raise JourneyError("INVALID_REQUEST")
            length = int(length_text)
            if length > limit:
                raise JourneyError("PAYLOAD_TOO_LARGE")
            if method in ("GET", "DELETE") and length:
                raise JourneyError("INVALID_REQUEST")
            content_type = environ.get("CONTENT_TYPE", "")
            if type(content_type) is not str or "," in content_type:
                raise JourneyError("INVALID_REQUEST")
            if method in ("POST", "PUT") and not calculation:
                content_parts = tuple(part.strip().lower() for part in content_type.split(";"))
                if content_parts not in (("application/json",), ("application/json", "charset=utf-8")):
                    raise JourneyError("INVALID_REQUEST")
            auth = environ.get("HTTP_AUTHORIZATION")
            if auth is not None and (type(auth) is not str or "," in auth):
                raise JourneyError("SESSION_REQUIRED")
            attempt_id = environ.get("HTTP_X_ATTEMPT_ID")
            if attempt_id is not None and (type(attempt_id) is not str or "," in attempt_id):
                raise JourneyError("INVALID_REQUEST")
            # Waitress exposes a finite, fully buffered stream. Do not use this
            # adapter behind another WSGI server that does not enforce limits.
            body_bytes = environ["wsgi.input"].read(length)
            if type(body_bytes) is not bytes or len(body_bytes) != length:
                raise JourneyError("INVALID_REQUEST")
            if execution_ready is not None and execution_ready() is not True:
                # A dead worker/DB is not a reason to continue accepting 202s.
                # Existing raw-path, peer and framing guards still run first.
                raise JourneyError("TEMPORARILY_UNAVAILABLE")
            if chart_service is not None and raw_path.startswith(chart_service.path_prefix):
                if method != "GET" or "HTTP_RANGE" in environ:
                    raise JourneyError("NOT_FOUND")
                # A short-lived bearer capability is the only authority for
                # this exact route. Common network/framing checks still apply.
                return send(start_response, 200, chart_service.read_path(raw_path))
            encoded = calculation and "multipart/form-data" in content_type.lower()
            if encoded:
                body = base64.b64encode(body_bytes).decode("ascii")
            else:
                try:
                    # The legacy form is already base64 text. Its missing or
                    # application/json Content-Type must not re-encode it.
                    body = body_bytes.decode("utf-8")
                except UnicodeError:
                    raise JourneyError("INVALID_REQUEST") from None
            if raw_path in ("/", "/healthz") and method == "GET":
                if raw_path == "/healthz" and ready() is not True:
                    raise JourneyError("TEMPORARILY_UNAVAILABLE")
                status_body = dict(_STATUS_BODY)
                if calculation_body_limit is not None:
                    # Configuration does not prove that a separately owned
                    # runner is alive. Do not claim calculator availability.
                    status_body["mode"] = "explicit_calculation_transport"
                    status_body["calculation_transport_configured"] = True
                if execution_ready is not None:
                    status_body.update(mode="local_journey", calculator_available=True,
                                       program_target_combinations=15,
                                       completion_policy={"cycles": "pending_policy",
                                                          "compressions": "evaluated", "ventilations": "evaluated"})
                # Local diagnostics only. API logging readiness never changes
                # business readiness or the health HTTP status. Worker counters
                # are not falsely presented as this process's counters.
                if getattr(service, "course_mode", None) == "course_v2":
                    status_body.update(mode="course_v2", login_path="/api/v2/sessions/",
                                       programs_path="/api/v2/courses/progress/")
                recorder = getattr(service, "operations", None)
                if recorder is not None:
                    try:
                        status_body["operational_logs"] = recorder.status()
                    except Exception:
                        status_body["operational_logs"] = {"scope": "api_process", "running": False}
                return send(start_response, 200, json.dumps(status_body).encode("utf-8"))
            headers = {"Host": authority}
            if content_type:
                headers["Content-Type"] = content_type
            if auth is not None:
                headers["Authorization"] = auth
            if attempt_id is not None:
                headers["X-Attempt-ID"] = attempt_id
            query_params, multi_query = _parse_query(query_string)
            event = {
                "httpMethod": method, "path": raw_path,
                "headers": headers,
                "multiValueHeaders": {name: [value] for name, value in headers.items()},
                "queryStringParameters": query_params,
                "multiValueQueryStringParameters": multi_query,
                "body": body, "isBase64Encoded": encoded,
            }
            result = handle(event, SimpleNamespace(aws_request_id=request_id), service)
            status, text = result["statusCode"], result["body"]
            if type(status) is not int or type(text) is not str:
                raise JourneyError("TEMPORARILY_UNAVAILABLE")
            return send(start_response, status, text.encode("utf-8"))
        except JourneyError as error:
            return send(start_response, *_public_error(error.code, request_id))
        except Exception:
            # No request values, exception messages, environment or traceback
            # may be echoed to the HTTP client or the server's console.
            return send(start_response, *_public_error("TEMPORARILY_UNAVAILABLE", request_id))

    # The pinned listener derives its parser cap from the same validated
    # configuration. Wrapping this callable drops back to the safe default.
    application._local_calculation_body_limit = calculation_body_limit
    application._local_response_body_limit = response_body_limit
    application._local_course_v2 = getattr(service, "course_mode", None) == "course_v2"
    return application


def create_server(application, host, port):
    """Create the pinned bounded server, with dedicated framing/privacy guards."""
    host, port = _address(host), _port(port)
    calculation_body_limit = _body_limit(getattr(application, "_local_calculation_body_limit", None))
    response_body_limit = getattr(application, "_local_response_body_limit", None)
    course_v2 = getattr(application, "_local_course_v2", False) is True
    if response_body_limit is not None and (
            type(response_body_limit) is not int or response_body_limit < BODY_LIMIT):
        raise ValueError("An explicit local response-body limit is required.")
    maximum_body_limit = max(BODY_LIMIT, calculation_body_limit or 0)
    if version("waitress") != WAITRESS_VERSION:
        raise RuntimeError("Install the pinned local HTTP server dependency.")
    from waitress.channel import HTTPChannel
    from waitress.parser import (
        HEADER_FIELD_RE, HTTPRequestParser, ParsingError,
        TransferEncodingNotImplemented, get_header_lines,
    )
    from waitress.server import create_server as waitress_create_server

    class StrictParser(HTTPRequestParser):
        def parse_header(self, header_plus):
            try:
                line_end = header_plus.find(b"\r\n")
                if line_end >= 0:
                    for line in get_header_lines(header_plus[line_end + 2:]):
                        header = HEADER_FIELD_RE.match(line)
                        if header and header.group("name").upper() == b"TRANSFER-ENCODING":
                            raise ParsingError("Invalid request headers.")
                        if (header and header.group("name").upper() == b"CONTENT-LENGTH"
                                and len(header.group("value").strip(b" \t")) > max(6, len(str(maximum_body_limit)))):
                            raise ParsingError("Invalid request headers.")
                super().parse_header(header_plus)
                # Waitress checks this per-request cap before receiving the
                # body. Never enlarge the shared Adjustments object: a large
                # calculation must not enlarge a concurrent login request.
                limit = (calculation_body_limit if calculation_body_limit is not None
                         and _calculation_route(self.command, self.request_uri, course_v2=course_v2)
                         else BODY_LIMIT)
                self.adj = copy(self.adj)
                self.adj.max_request_body_size = limit + 1
                # Each request closes its channel; queued pipelined requests
                # are discarded by Waitress once this response is complete.
                self.headers["CONNECTION"] = "close"
                self.connection_close = True
            except (ParsingError, TransferEncodingNotImplemented):
                raise ParsingError("Invalid request headers.") from None

    class StrictChannel(HTTPChannel):
        parser_class = StrictParser
        # Waitress may log raw request paths on connection errors. A local
        # private logger avoids those messages without changing global logs.
        logger = logging.Logger("local_server.private_http", level=logging.CRITICAL + 1)

    server = waitress_create_server(
        application, host=host, port=port, ipv6=False,
        threads=4, connection_limit=32, backlog=32,
        max_request_header_size=HEADER_LIMIT,
        # Waitress rejects >= its cap. The request parser narrows this to the
        # route's exact limit + 1 before any body bytes can be buffered. Keep
        # admitted bodies in memory instead of spilling measurements to /tmp.
        max_request_body_size=maximum_body_limit + 1,
        inbuf_overflow=maximum_body_limit + 1,
        outbuf_overflow=(response_body_limit + HEADER_LIMIT + 1
                         if response_body_limit is not None else 64 * 1024),
        outbuf_high_watermark=64 * 1024,
        channel_timeout=10, cleanup_interval=1,
        channel_request_lookahead=0, expose_tracebacks=False, ident="",
        trusted_proxy=None, clear_untrusted_proxy_headers=True,
        log_untrusted_proxy_headers=False,
    )
    # The single IPv4 listener does not enter its accept/event loop until the
    # caller invokes run(). No Waitress process-wide class is changed.
    server.channel_class = StrictChannel
    return server

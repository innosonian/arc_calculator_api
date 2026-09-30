"""Restricted local HTTP transport over the authenticated /api/v2 course handler.

Only an assembled course application (``course_mode == "course_v2"`` with its
``course_http``) is served. The measurement upload route is enabled only by an
explicit wire-body limit and a calculation service that admits it; every other
route keeps the small control-body limit. The listener must be created through
``create_server`` so the pinned parser guard is active before any request is
accepted by the event loop.
"""

from http import HTTPStatus
from importlib.metadata import version
import base64
from copy import copy
import json
import logging
import re
from types import SimpleNamespace
import uuid

# _address/_port keep their former names here (same objects).
from local_server.addresses import local_address as _address, local_port as _port
from local_server.constants import BODY_LIMIT, HEADER_LIMIT
from mock_journey.catalog import PROGRAMS, definition_keys
from mock_journey.contracts import CURRENT_ADAPTER_VERSION, expected_goal_status
from mock_journey.course_mode import COURSE_MODE as _COURSE_MODE
from mock_journey.errors import JourneyError
from mock_journey.handler import handle
from mock_journey.settings import base64_body_bytes


# Waitress private parser/shutdown APIs are used here and in
# runtime.LocalRuntime._stop_http (see the pinned-surface test). Every listener
# is created by create_server, which refuses any other installed version.
WAITRESS_VERSION = "3.0.2"
# Key order is part of the local status contract (same as the former --course-v2).
_STATUS_BODY = {
    "service": "arc-local-api", "mode": "course_v2",
    "calculator_available": False,
    "login_path": "/api/v2/sessions/", "programs_path": "/api/v2/courses/progress/",
}
# Diagnostic facts of a supervised local journey, derived from the catalog
# (5 programs x 3 targets = 15) and the current adapter's goal status per
# goal kind (every kind evaluated under the D136 cycle rule). The goal kinds
# are listed in the order the catalog first names them: cycles, compressions,
# ventilations.
_PROGRAM_TARGET_COMBINATIONS = len(definition_keys())
_COMPLETION_POLICY = tuple((kind, expected_goal_status(kind, CURRENT_ADAPTER_VERSION))
                           for kind in dict.fromkeys(program[2] for program in PROGRAMS))
# Local listener sizing only; not a deployment or AWS setting.
_WAITRESS_THREADS = 4
_CONNECTION_LIMIT = 32
_LISTEN_BACKLOG = 32
_CHANNEL_TIMEOUT_SECONDS = 10
_OUTBUF_HIGH_WATERMARK = 64 * 1024
_DEFAULT_OUTBUF_OVERFLOW = 64 * 1024
_API_PREFIX = "/api/v2/"
_METHODS = ("GET", "POST", "DELETE", "PUT")
_V2_CALCULATION_PATH = re.compile(
    r"/api/v2/attempts/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/calculation/\Z"
)


def _path_only(path):
    if type(path) is not str:
        return path
    return path.split("?", 1)[0]


def _calculation_route(method, path):
    path = _path_only(path)
    if method != "POST" or type(path) is not str:
        return False
    return _V2_CALCULATION_PATH.fullmatch(path) is not None


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


def _response_limit(value):
    # make_application and create_server both enforce this: create_server
    # also accepts arbitrary callables and must not trust their attributes.
    if value is not None and (type(value) is not int or value < BODY_LIMIT):
        raise ValueError("An explicit local response-body limit is required.")
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


def _unsafe_characters(text):
    """Percent/query/fragment/backslash or any non-visible-ASCII character."""
    return (any(char in text for char in "%?#\\")
            or any(ord(char) < 33 or ord(char) > 126 for char in text))


# Request guards, in the order application() calls them. The order decides
# the public code when a request violates several rules; each guard raises
# JourneyError, and any other exception becomes TEMPORARILY_UNAVAILABLE in
# application(). The peer check stays inline there (a direct NOT_FOUND send).

def _check_authority_and_fetch_metadata(environ, authorities):
    authority = environ.get("HTTP_HOST")
    if (type(authority) is not str or "," in authority
            or authority.lower() not in authorities):
        raise JourneyError("INVALID_REQUEST")
    if ("HTTP_ORIGIN" in environ
            or environ.get("HTTP_SEC_FETCH_SITE", "none") not in ("none", "same-origin")):
        raise JourneyError("INVALID_REQUEST")
    return authority


def _check_raw_target(environ):
    """Return (raw path, query string) of an unnormalized, exactly echoed target."""
    path_info = environ.get("PATH_INFO")
    request_uri = environ.get("REQUEST_URI")
    query_string = environ.get("QUERY_STRING") or ""
    if type(path_info) is not str or not path_info.startswith("/") or "//" in path_info:
        raise JourneyError("INVALID_REQUEST")
    if _unsafe_characters(path_info):
        raise JourneyError("INVALID_REQUEST")
    if query_string:
        # Only /api/v2 routes have a query contract (their router
        # enforces each route's allowlist). Status and signed chart
        # routes keep rejecting any query instead of ignoring it.
        if not path_info.startswith(_API_PREFIX):
            raise JourneyError("INVALID_REQUEST")
        if _unsafe_characters(query_string):
            raise JourneyError("INVALID_REQUEST")
        expected = path_info + "?" + query_string
        if type(request_uri) is not str or request_uri not in (expected, path_info):
            raise JourneyError("INVALID_REQUEST")
    elif type(request_uri) is not str or request_uri != path_info:
        raise JourneyError("INVALID_REQUEST")
    return path_info, query_string


def _check_method(environ):
    method = environ.get("REQUEST_METHOD")
    if method not in _METHODS:
        raise JourneyError("NOT_FOUND")
    return method


def _check_framing(environ, method, raw_path, calculation_body_limit):
    """Encoding, Content-Length and Content-Type; returns (calculation, length, content_type)."""
    if "HTTP_CONTENT_ENCODING" in environ or "HTTP_TRANSFER_ENCODING" in environ:
        raise JourneyError("INVALID_REQUEST")
    calculation = calculation_body_limit is not None and _calculation_route(method, raw_path)
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
    return calculation, length, content_type


def _check_authorization(environ):
    auth = environ.get("HTTP_AUTHORIZATION")
    if auth is not None and (type(auth) is not str or "," in auth):
        raise JourneyError("SESSION_REQUIRED")
    return auth


def _read_body(environ, length):
    # Waitress exposes a finite, fully buffered stream. Do not use this
    # adapter behind another WSGI server that does not enforce limits.
    body_bytes = environ["wsgi.input"].read(length)
    if type(body_bytes) is not bytes or len(body_bytes) != length:
        raise JourneyError("INVALID_REQUEST")
    return body_bytes


def _decode_body(body_bytes, calculation, content_type):
    """Return (event body text, isBase64Encoded)."""
    encoded = calculation and "multipart/form-data" in content_type.lower()
    if encoded:
        return base64.b64encode(body_bytes).decode("ascii"), encoded
    try:
        # The encoded measurement form is already base64 text. Its
        # missing or non-multipart Content-Type must not re-encode it.
        return body_bytes.decode("utf-8"), encoded
    except UnicodeError:
        raise JourneyError("INVALID_REQUEST") from None


def _status_body(service, calculation_body_limit, execution_ready):
    status_body = dict(_STATUS_BODY)
    if calculation_body_limit is not None:
        # Configuration does not prove that a separately owned
        # runner is alive. Do not claim calculator availability.
        status_body["calculation_transport_configured"] = True
    if execution_ready is not None:
        status_body.update(calculator_available=True, program_target_combinations=_PROGRAM_TARGET_COMBINATIONS,
                           completion_policy=dict(_COMPLETION_POLICY))
    # Local diagnostics only. API logging readiness never changes
    # business readiness or the health HTTP status. Worker counters
    # are not falsely presented as this process's counters.
    recorder = getattr(service, "operations", None)
    if recorder is not None:
        try:
            status_body["operational_logs"] = recorder.status()
        except Exception:
            status_body["operational_logs"] = {"scope": "api_process", "running": False}
    return status_body


def _build_event(method, raw_path, authority, content_type, auth, query_string, body, encoded):
    headers = {"Host": authority}
    if content_type:
        headers["Content-Type"] = content_type
    if auth is not None:
        headers["Authorization"] = auth
    query_params, multi_query = _parse_query(query_string)
    return {
        "httpMethod": method, "path": raw_path,
        "headers": headers,
        "multiValueHeaders": {name: [value] for name, value in headers.items()},
        "queryStringParameters": query_params,
        "multiValueQueryStringParameters": multi_query,
        "body": body, "isBase64Encoded": encoded,
    }


def make_application(service, ready, host, port, allowed_clients, *, calculation_body_limit=None,
                     chart_service=None, response_body_limit=None, execution_ready=None):
    """Build a WSGI adapter; ``ready`` returns True only for a healthy DB.

    ``service`` must be the assembled /api/v2 course application; there is no
    other local mode. Network exposure acknowledgment belongs to the CLI. This
    constructor also
    rejects nonliteral/nonlocal addresses, empty allowlists and wildcard hosts.
    ``calculation_body_limit`` counts received HTTP bytes, including multipart
    boundaries. The separate API payload limit counts the encoded event body;
    it must admit this entire wire limit after base64 encoding. This option
    starts no worker and does not discover a catalog, storage, or runtime.
    """
    host, port = _address(host), _port(port)
    if (getattr(service, "course_mode", None) != _COURSE_MODE
            or getattr(service, "course_http", None) is None):
        raise ValueError("The local server requires the assembled /api/v2 course application.")
    calculation_body_limit = _body_limit(calculation_body_limit)
    _response_limit(response_body_limit)
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
                or payload_limit < base64_body_bytes(calculation_body_limit)):
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
            authority = _check_authority_and_fetch_metadata(environ, authorities)
            raw_path, query_string = _check_raw_target(environ)
            method = _check_method(environ)
            calculation, length, content_type = _check_framing(environ, method, raw_path, calculation_body_limit)
            auth = _check_authorization(environ)
            body_bytes = _read_body(environ, length)
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
            body, encoded = _decode_body(body_bytes, calculation, content_type)
            if raw_path in ("/", "/healthz") and method == "GET":
                if raw_path == "/healthz" and ready() is not True:
                    raise JourneyError("TEMPORARILY_UNAVAILABLE")
                status_body = _status_body(service, calculation_body_limit, execution_ready)
                return send(start_response, 200, json.dumps(status_body).encode("utf-8"))
            event = _build_event(method, raw_path, authority, content_type, auth, query_string, body, encoded)
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
    return application


def create_server(application, host, port):
    """Create the pinned bounded server, with dedicated framing/privacy guards."""
    host, port = _address(host), _port(port)
    calculation_body_limit = _body_limit(getattr(application, "_local_calculation_body_limit", None))
    response_body_limit = _response_limit(getattr(application, "_local_response_body_limit", None))
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
                         and _calculation_route(self.command, self.request_uri)
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
        threads=_WAITRESS_THREADS, connection_limit=_CONNECTION_LIMIT, backlog=_LISTEN_BACKLOG,
        max_request_header_size=HEADER_LIMIT,
        # Waitress rejects >= its cap. The request parser narrows this to the
        # route's exact limit + 1 before any body bytes can be buffered. Keep
        # admitted bodies in memory instead of spilling measurements to /tmp.
        max_request_body_size=maximum_body_limit + 1,
        inbuf_overflow=maximum_body_limit + 1,
        outbuf_overflow=(response_body_limit + HEADER_LIMIT + 1
                         if response_body_limit is not None else _DEFAULT_OUTBUF_OVERFLOW),
        outbuf_high_watermark=_OUTBUF_HIGH_WATERMARK,
        channel_timeout=_CHANNEL_TIMEOUT_SECONDS, cleanup_interval=1,
        channel_request_lookahead=0, expose_tracebacks=False, ident="",
        trusted_proxy=None, clear_untrusted_proxy_headers=True,
        log_untrusted_proxy_headers=False,
    )
    # The single IPv4 listener does not enter its accept/event loop until the
    # caller invokes run(). No Waitress process-wide class is changed.
    server.channel_class = StrictChannel
    return server

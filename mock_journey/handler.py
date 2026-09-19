"""Separate REST proxy entrypoint for Mock control APIs."""

from mock_journey.bootstrap import configure_imports

configure_imports()

import base64
import json
import re

from mock_journey.auth import extract_bearer
from mock_journey.errors import JourneyError
from services.operational_logs import log_context, record_event, bind_identifiers, write_diagnostic
from services.submission_response import compose_calculation_snapshot


_ATTEMPT = re.compile(r"/mock/v1/attempts/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?:/(reauthorize|cancel|calculation|chart-link))?\Z")
_ATTEMPT_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
_CONTROL_BODY_LIMIT = 16 * 1024  # Only short control JSON; never applied to binary calculation uploads.


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def _json_body(event):
    try:
        body = event.get("body") or ""
        if type(body) is not str or len(body.encode()) > _CONTROL_BODY_LIMIT:
            raise JourneyError("INVALID_REQUEST")
        if event.get("isBase64Encoded"):
            body = base64.b64decode(body, validate=True).decode("utf-8")
        return json.loads(body, object_pairs_hook=_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite JSON")))
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise JourneyError("INVALID_REQUEST") from None


def _response(status, body=None):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", "Cache-Control": "no-store"},
        "body": "" if status == 204 else json.dumps(body, allow_nan=False),
    }


def _calculation_response(status, body):
    if status != 200:
        return _response(status, body)
    # Storage integrity and ownership were checked by CalculationService.
    # The response overlay never mutates or re-saves that committed snapshot.
    try:
        response_body = compose_calculation_snapshot(body)
    except ValueError:
        raise JourneyError("STORED_INPUT_INVALID")
    return {"statusCode": 200, "headers": {"Content-Type": "application/json", "Cache-Control": "no-store"},
            "body": response_body.decode("utf-8")}


def _single_header(event, name, *, required=False):
    """Resolve one REST proxy header without choosing between conflicting copies."""
    headers, multi = event.get("headers"), event.get("multiValueHeaders")
    headers = {} if headers is None else headers
    multi = {} if multi is None else multi
    if type(headers) is not dict or type(multi) is not dict:
        raise JourneyError("INVALID_REQUEST")
    single = [value for key, value in headers.items() if type(key) is str and key.lower() == name]
    multiple = [value for key, value in multi.items() if type(key) is str and key.lower() == name]
    if len(single) > 1 or len(multiple) > 1:
        raise JourneyError("INVALID_REQUEST")
    values = single
    if multiple:
        if type(multiple[0]) is not list or len(multiple[0]) != 1:
            raise JourneyError("INVALID_REQUEST")
        if single and single[0] != multiple[0][0]:
            raise JourneyError("INVALID_REQUEST")
        values = multiple[0]
    if not values:
        if required:
            raise JourneyError("INVALID_REQUEST")
        return None
    value = values[0]
    if type(value) is not str or not value or "," in value or "\r" in value or "\n" in value:
        raise JourneyError("INVALID_REQUEST")
    return value


def _attempt_header(event, *, required=False):
    value = _single_header(event, "x-attempt-id", required=required)
    if value is not None and not _ATTEMPT_ID.fullmatch(value):
        raise JourneyError("INVALID_REQUEST")
    return value


def _submit_calculation(calculation, auth, ident, event, *, legacy_errors=False):
    # Validate duplicate metadata without decoding or changing the body. A
    # multi-value-only Content-Type is normalized once for the legacy parser.
    content_type = _single_header(event, "content-type")
    headers = event.get("headers") or {}
    if content_type is not None and not any(
        type(key) is str and key.lower() == "content-type" for key in headers
    ):
        event = {**event, "headers": {**headers, "Content-Type": content_type}}
    from mock_journey.legacy_bridge import MeasurementInputError

    try:
        result = calculation.submit(auth, ident, event)
    except MeasurementInputError as error:
        if not legacy_errors:
            raise
        return _response(400, {"type": "client_error", "message": error.legacy_message})
    return _calculation_response(*result)


def handle(event, context, service):
    with log_context(getattr(service, "operations", None), request_id=getattr(context, "aws_request_id", "local")):
        return _handle(event, context, service)


def _handle_course_v2(event, context, service):
    http = getattr(service, "course_http", None)
    if http is None:
        raise JourneyError("TEMPORARILY_UNAVAILABLE")
    return http.dispatch(event)


def _handle(event, context, service):
    request_id = getattr(context, "aws_request_id", "local")
    try:
        if getattr(service, "course_mode", None) == "course_v2":
            return _handle_course_v2(event, context, service)
        if type(event) is not dict:
            raise JourneyError("INVALID_REQUEST")
        query = event.get("queryStringParameters")
        multi_query = event.get("multiValueQueryStringParameters")
        query = {} if query is None else query
        multi_query = {} if multi_query is None else multi_query
        if type(query) is not dict or type(multi_query) is not dict:
            raise JourneyError("INVALID_REQUEST")
        if query or multi_query:
            raise JourneyError("INVALID_REQUEST")
        method, path = event.get("httpMethod"), event.get("path")
        if (method, path) == ("POST", "/mock/v1/sessions"):
            return _response(201, service.login(_json_body(event)))
        match = _ATTEMPT.fullmatch(path) if type(path) is str else None
        alias = (method, path) == ("POST", "/cpr-analysis")
        if path not in ("/mock/v1/session", "/mock/v1/programs", "/mock/v1/attempts") and not match and not alias:
            raise JourneyError("NOT_FOUND")
        auth = service.auth.authenticate(extract_bearer(event), allow_logout_receipt=(
            method == "DELETE" and path == "/mock/v1/session"
        ))
        bind_identifiers(session_id=getattr(auth, "session_id", None))
        if alias:
            ident = _attempt_header(event, required=True)
            return _submit_calculation(service.require_calculation(), auth, ident, event, legacy_errors=True)
        if path == "/mock/v1/session" and method == "GET":
            return _response(200, service.session(auth))
        if path == "/mock/v1/session" and method == "DELETE":
            service.state.logout(auth)
            record_event("logout_succeeded")
            return _response(204)
        if path == "/mock/v1/programs" and method == "GET":
            return _response(200, service.programs(auth))
        if path == "/mock/v1/attempts" and method == "POST":
            status, result = service.create_attempt(auth, _json_body(event))
            return _response(status, result)
        if match:
            ident, operation = match.groups()
            supplied_id = _attempt_header(event)
            if supplied_id is not None and supplied_id != ident:
                raise JourneyError("INVALID_REQUEST")
            if operation == "calculation" and method in ("POST", "GET"):
                calculation = service.require_calculation()
                if method == "POST":
                    return _submit_calculation(calculation, auth, ident, event)
                return _calculation_response(*calculation.result(auth, ident))
            if operation == "chart-link" and method == "GET":
                return _response(200, service.require_calculation().chart_link(auth, ident))
            if method == "GET" and operation is None:
                return _response(200, service.get_attempt(auth, ident))
            if method == "POST" and operation == "reauthorize":
                return _response(200, service.reauthorize(auth, ident, _json_body(event)))
            if method == "POST" and operation == "cancel":
                service.cancel(auth, ident, _json_body(event))
                return _response(204)
        raise JourneyError("NOT_FOUND")
    except JourneyError as error:
        login = type(event) is dict and event.get("httpMethod") == "POST" and event.get("path") == "/mock/v1/sessions"
        record_event("login_failed" if login else "request_rejected", error_code=error.code, http_status=error.status)
        return _response(error.status, {"error": {
            "code": error.code, "message": error.message, "request_id": request_id,
        }})
    except Exception as error:
        login = type(event) is dict and event.get("httpMethod") == "POST" and event.get("path") == "/mock/v1/sessions"
        record_event("login_failed" if login else "request_rejected",
                     error_code="TEMPORARILY_UNAVAILABLE", http_status=503)
        write_diagnostic("error", "request_failed", {"request_id": request_id, "exception": error})
        return _response(503, {"error": {
            "code": "TEMPORARILY_UNAVAILABLE", "message": "The service is temporarily unavailable.",
            "request_id": request_id,
        }})


def run(event, context):
    from mock_journey.runtime import get_application
    from mock_journey.aws_runtime import invocation
    try:
        service = get_application()
        with invocation(service, context):
            return handle(event, context, service)
    except Exception as error:
        write_diagnostic("error", "request_failed", {"exception": error})
        return _response(503, {"error": {
            "code": "TEMPORARILY_UNAVAILABLE", "message": "The service is temporarily unavailable.",
            "request_id": getattr(context, "aws_request_id", "local"),
        }})

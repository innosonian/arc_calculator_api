"""REST proxy entrypoint for the /api/v2 course API.

Every request is handed to the assembled application's ``course_http``
(``mock_journey.course_http.CourseHttp``), which owns routing, validation and
the v2 envelopes. No other route family remains (D103). A service that is not
an assembled course application, or whose boot failed, answers with the fixed
503 error envelope below.
"""

from mock_journey.bootstrap import configure_imports

configure_imports()

import json

from mock_journey.course_mode import COURSE_MODE as _COURSE_MODE
from mock_journey.errors import JourneyError
from services.operational_logs import log_context, record_event, write_diagnostic


def _response(status, body=None):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", "Cache-Control": "no-store"},
        "body": "" if status == 204 else json.dumps(body, allow_nan=False),
    }


def _unavailable(request_id):
    return _response(503, {"error": {
        "code": "TEMPORARILY_UNAVAILABLE", "message": "The service is temporarily unavailable.",
        "request_id": request_id,
    }})


def handle(event, context, service):
    with log_context(getattr(service, "operations", None), request_id=getattr(context, "aws_request_id", "local")):
        return _handle(event, context, service)


def _handle(event, context, service):
    request_id = getattr(context, "aws_request_id", "local")
    try:
        http = getattr(service, "course_http", None)
        if getattr(service, "course_mode", None) != _COURSE_MODE or http is None:
            raise JourneyError("TEMPORARILY_UNAVAILABLE")
        return http.dispatch(event)
    except JourneyError as error:
        record_event("request_rejected", error_code=error.code, http_status=error.status)
        return _response(error.status, {"error": {
            "code": error.code, "message": error.message, "request_id": request_id,
        }})
    except Exception as error:
        record_event("request_rejected", error_code="TEMPORARILY_UNAVAILABLE", http_status=503)
        write_diagnostic("error", "request_failed", {"request_id": request_id, "exception": error})
        return _unavailable(request_id)


def run(event, context):
    from mock_journey.runtime import get_application
    from mock_journey.aws_runtime import invocation
    try:
        service = get_application()
        with invocation(service, context):
            return handle(event, context, service)
    except Exception as error:
        write_diagnostic("error", "request_failed", {"exception": error})
        return _unavailable(getattr(context, "aws_request_id", "local"))

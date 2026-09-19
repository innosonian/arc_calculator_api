"""VCC course HTTP boundary. Route/method/query/body validation and envelopes.

W5 hooks
--------
CourseHttp is not wired into handler.py or local_server (W5). Dispatch accepts an
API Gateway REST event and returns {statusCode, headers, body}.

1. Route wiring: call CourseHttp.dispatch(event) for every APP_ROUTES path after
   existing /mock/v1 handling is disabled in course_v2 mode. Preserve health and
   static chart routes. Trailing slash is required; do not add redirects.

2. measurement_submit(auth, attempt_id, event) -> dict
   Invoked for POST /api/v2/attempts/{attemptId}/calculation/ with the raw event.
   This module does not parse CPR binary/multipart. W5 must reuse the existing
   CalculationService.submit / legacy_bridge parser. Return
   {state, calculation, evaluation, progress_application, submit_arc?} or raise
   CourseError. HTTP maps created/cancelled→409 INVALID_STATE, queued/processing
   →202 pending, evaluated→200 succeeded, failed→503 CALCULATION_FAILED,
   outcome_unknown→503 CALCULATION_OUTCOME_UNKNOWN. GET calculation_result must
   not execute calculation or submission.

3. issue_resume(auth, attempt_id) -> str
   Called only after ownership checks for attempt create replay and reauthorize.
   W5 should use AuthManager.prepare_resume / resume_credential. The credential
   is added to the HTTP response only and must never be written into
   StartReceipt.response_json.

4. authenticate / login / logout / session_reader / load_attempt / reauthorize /
   cancel / chart_link are the existing AuthManager and JourneyService surfaces
   adapted to AuthContext and CourseError.
"""

import base64
import json
import re

from mock_journey.course_contracts import (
    APP_ROUTES, CANCEL_REASON_WIRE_TO_INTERNAL, CONTENT_EVENT_TYPES, PAGE_DEFAULT,
    PAGE_SIZE_DEFAULT, PAGE_SIZE_MAX, PUBLIC_ID_MAX, START_REQUEST_FIELDS, ContentReport,
    StartCommand, require_hash, require_public_id, require_uuid,
)
from mock_journey.course_errors import CourseError
from mock_journey.course_response import (
    aggregate_availability, attempt_view_data, calculation_view_data, chart_link_data,
    course_detail_data, error_envelope, parse_receipt_data, session_data, success_envelope,
    utc_timestamp,
)
from mock_journey.course_settings import CourseSettings
from mock_journey.errors import JourneyError
from mock_journey.typed import parse_json


_DUMMY_LOGIN = "test@test.com"
_AUTH_CODES = frozenset({
    "LOGIN_FAILED", "SESSION_REQUIRED", "SESSION_EXPIRED", "SESSION_REVOKED",
})
_POSITIVE = re.compile(r"[1-9][0-9]*\Z")


def map_cancel_reason(wire_reason):
    if wire_reason not in CANCEL_REASON_WIRE_TO_INTERNAL:
        raise CourseError("INVALID_REQUEST")
    return CANCEL_REASON_WIRE_TO_INTERNAL[wire_reason]


def _compile_path(spec):
    parts = []
    names = []
    rest = spec.path
    while True:
        start = rest.find("{")
        if start < 0:
            parts.append(re.escape(rest))
            break
        end = rest.find("}", start)
        parts.append(re.escape(rest[:start]))
        name = rest[start + 1:end]
        names.append(name)
        if name == "attemptId":
            parts.append(r"(?P<attemptId>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")
        else:
            parts.append(rf"(?P<{name}>[1-9][0-9]{{0,15}})")
        rest = rest[end + 1:]
    return re.compile(r"\A" + "".join(parts) + r"\Z"), names, spec


_COMPILED = tuple(_compile_path(spec) for spec in APP_ROUTES)


class CourseHttp:
    def __init__(
        self, service, settings, *, clock, uuid_factory, authenticate=None, login=None,
        logout=None, session_reader=None, issue_resume=None, load_attempt=None,
        reauthorize=None, cancel=None, measurement_submit=None, calculation_result=None,
        chart_link=None,
    ):
        if type(settings) is not CourseSettings:
            raise CourseError("TEMPORARILY_UNAVAILABLE")
        if not callable(clock) or not callable(uuid_factory):
            raise CourseError("TEMPORARILY_UNAVAILABLE")
        self._service = service
        self._settings = settings
        self._clock = clock
        self._uuid_factory = uuid_factory
        self._authenticate = authenticate
        self._login = login
        self._logout = logout
        self._session_reader = session_reader
        self._issue_resume = issue_resume
        self._load_attempt = load_attempt
        self._reauthorize = reauthorize
        self._cancel = cancel
        self._measurement_submit = measurement_submit
        self._calculation_result = calculation_result
        self._chart_link = chart_link

    def dispatch(self, event):
        request_id = require_uuid(self._uuid_factory())
        try:
            return self._dispatch(event, request_id)
        except CourseError as error:
            return self._error(error, request_id)
        except JourneyError as error:
            return self._error(CourseError(error.code), request_id)
        except Exception:
            return self._error(CourseError("TEMPORARILY_UNAVAILABLE"), request_id)

    def _dispatch(self, event, request_id):
        if type(event) is not dict:
            raise CourseError("INVALID_REQUEST")
        method, path = event.get("httpMethod"), event.get("path")
        if type(method) is not str or type(path) is not str:
            raise CourseError("INVALID_REQUEST")
        allowed, params, spec = self._match(method, path)
        if spec is None and not allowed:
            raise CourseError("NOT_FOUND")
        if spec is None:
            raise CourseError("METHOD_NOT_ALLOWED")
        query = self._query(event, spec)
        auth = None
        if spec.auth_required:
            auth = self._require_auth(event, allow_logout=(spec.route_id == "logout"))
        body = self._body(event, spec)
        data, status = self._handle(spec, auth, params, query, body, event)
        if status == 204:
            return self._raw(204, "", request_id)
        timestamp = utc_timestamp(self._clock())
        return self._raw(status, json.dumps(success_envelope(data, timestamp=timestamp), allow_nan=False), request_id)

    def _match(self, method, path):
        allowed = []
        matched = None
        params = {}
        for regex, names, spec in _COMPILED:
            found = regex.fullmatch(path)
            if found is None:
                continue
            allowed.append(spec.method)
            if spec.method == method:
                matched = spec
                params = {name: found.group(name) for name in names}
        return tuple(allowed), params, matched

    def _query(self, event, spec):
        query = event.get("queryStringParameters")
        multi = event.get("multiValueQueryStringParameters")
        query = {} if query is None else query
        multi = {} if multi is None else multi
        if type(query) is not dict or type(multi) is not dict:
            raise CourseError("INVALID_REQUEST")
        allowed = set(spec.query_allowed)
        keys = set()
        for source in (query, multi):
            for key in source:
                if type(key) is not str:
                    raise CourseError("INVALID_REQUEST")
                keys.add(key)
        if keys - allowed:
            raise CourseError("INVALID_REQUEST")
        values = {}
        for key in allowed:
            raw = None
            if key in query:
                if type(query[key]) is not str:
                    raise CourseError("INVALID_REQUEST")
                raw = query[key]
            if key in multi:
                items = multi[key]
                if type(items) is not list or not items or any(type(item) is not str for item in items):
                    raise CourseError("INVALID_REQUEST")
                if len(items) != 1:
                    raise CourseError("INVALID_REQUEST")
                if raw is not None and raw != items[0]:
                    raise CourseError("INVALID_REQUEST")
                raw = items[0]
            if raw is None:
                continue
            if raw == "":
                raise CourseError("INVALID_REQUEST")
            values[key] = raw
        parsed = {}
        for key in spec.query_allowed:
            if key not in values:
                continue
            parsed[key] = self._query_int(values[key], key)
        if spec.route_id in ("course_detail", "item_detail") and "enrollmentId" not in parsed:
            raise CourseError("INVALID_REQUEST")
        if spec.route_id == "course_list":
            parsed.setdefault("page", PAGE_DEFAULT)
            parsed.setdefault("pageSize", PAGE_SIZE_DEFAULT)
            if parsed["pageSize"] > PAGE_SIZE_MAX:
                raise CourseError("INVALID_REQUEST")
        return parsed

    def _query_int(self, value, key):
        if _POSITIVE.fullmatch(value) is None:
            raise CourseError("INVALID_REQUEST")
        number = int(value)
        if key == "pageSize" and number > PAGE_SIZE_MAX:
            raise CourseError("INVALID_REQUEST")
        if number > PUBLIC_ID_MAX:
            raise CourseError("INVALID_REQUEST")
        return number

    def _body(self, event, spec):
        raw = self._raw_body(event)
        if spec.body_kind == "none":
            if raw:
                raise CourseError("INVALID_REQUEST")
            return None
        if spec.body_kind == "measurement":
            return None
        if len(raw) > self._settings.max_control_body_bytes:
            raise CourseError("PAYLOAD_TOO_LARGE")
        self._require_json_type(event)
        try:
            parsed = parse_json(raw)
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise CourseError("INVALID_REQUEST") from None
        if spec.body_kind == "empty_object":
            if parsed != {}:
                raise CourseError("INVALID_REQUEST")
            return parsed
        if spec.body_kind == "login":
            return self._login_body(parsed)
        if spec.body_kind == "start_request":
            return self._start_body(parsed)
        if spec.body_kind == "content_report":
            return self._report_body(parsed)
        if spec.body_kind == "reauthorize":
            return self._exact_strings(parsed, ("resumeCredential",))
        if spec.body_kind == "cancel":
            body = self._exact_strings(parsed, ("reason",))
            map_cancel_reason(body["reason"])
            return body
        if type(parsed) is not dict:
            raise CourseError("INVALID_REQUEST")
        return parsed

    def _raw_body(self, event):
        body = event.get("body")
        if body is None or body == "":
            return b""
        try:
            if event.get("isBase64Encoded"):
                if type(body) is not str:
                    raise CourseError("INVALID_REQUEST")
                return base64.b64decode(body, validate=True)
            if type(body) is bytes:
                return body
            if type(body) is str:
                return body.encode("utf-8")
        except (ValueError, TypeError, UnicodeError):
            raise CourseError("INVALID_REQUEST") from None
        raise CourseError("INVALID_REQUEST")

    def _require_json_type(self, event):
        value = _single_header(event, "content-type")
        if value is None:
            raise CourseError("INVALID_REQUEST")
        base, _, rest = value.partition(";")
        if base.strip().lower() != "application/json":
            raise CourseError("INVALID_REQUEST")
        rest = rest.strip()
        if rest and rest.lower().replace(" ", "") not in ("charset=utf-8", 'charset="utf-8"'):
            raise CourseError("INVALID_REQUEST")

    def _login_body(self, parsed):
        if type(parsed) is not dict or set(parsed) != {"loginId", "password"}:
            raise CourseError("INVALID_REQUEST")
        login_id, password = parsed["loginId"], parsed["password"]
        if type(login_id) is not str or type(password) is not str or not login_id or not password:
            raise CourseError("INVALID_REQUEST")
        try:
            login_id.encode("utf-8")
            password.encode("utf-8")
        except UnicodeError:
            raise CourseError("INVALID_REQUEST") from None
        return {"loginId": login_id, "password": password}

    def _start_body(self, parsed):
        if type(parsed) is not dict or set(parsed) != set(START_REQUEST_FIELDS):
            raise CourseError("INVALID_REQUEST")
        return StartCommand(
            require_uuid(parsed["clientRequestId"]),
            require_public_id(parsed["enrollmentId"]),
            require_public_id(parsed["courseId"]),
            require_public_id(parsed["courseItemLinkId"]),
            require_hash(parsed["definitionHash"]),
        )

    def _report_body(self, parsed):
        required = {
            "enrollmentId", "courseItemLinkId", "startId", "reportId", "contentVersion", "event",
        }
        if type(parsed) is not dict or set(parsed) != required:
            raise CourseError("INVALID_REQUEST")
        if type(parsed["contentVersion"]) is not str:
            raise CourseError("INVALID_REQUEST")
        event_type, intervals, display_id = _parse_event(parsed["event"])
        return {
            "enrollmentId": require_public_id(parsed["enrollmentId"]),
            "courseItemLinkId": require_public_id(parsed["courseItemLinkId"]),
            "report": ContentReport(
                require_uuid(parsed["reportId"]),
                require_uuid(parsed["startId"]),
                parsed["contentVersion"],
                event_type,
                intervals,
                display_id,
            ),
        }

    def _exact_strings(self, parsed, keys):
        if type(parsed) is not dict or set(parsed) != set(keys):
            raise CourseError("INVALID_REQUEST")
        out = {}
        for key in keys:
            value = parsed[key]
            if type(value) is not str or not value:
                raise CourseError("INVALID_REQUEST")
            out[key] = value
        return out

    def _require_auth(self, event, *, allow_logout=False):
        if self._authenticate is None:
            raise CourseError("TEMPORARILY_UNAVAILABLE")
        token = _bearer(event)
        return self._authenticate(token, allow_logout_receipt=allow_logout)

    def _handle(self, spec, auth, params, query, body, event):
        route = spec.route_id
        if route == "login":
            return self._handle_login(body)
        if route == "session":
            return self._handle_session(auth), 200
        if route == "session_refresh":
            return self._handle_refresh(auth), 200
        if route == "logout":
            self._hook(self._logout)(auth)
            return None, 204
        if route == "course_list":
            return parse_json(self._service.list_courses(
                auth, page=query["page"], page_size=query["pageSize"],
            )), 200
        if route == "course_detail":
            view = self._service.get_course(
                auth, course_id=_path_id(params["courseId"]), enrollment_id=query["enrollmentId"],
            )
            return course_detail_data(view), 200
        if route == "item_detail":
            return parse_json(self._service.get_item(
                auth,
                course_id=_path_id(params["courseId"]),
                enrollment_id=query["enrollmentId"],
                placement_id=_path_id(params["courseItemLinkId"]),
            )), 200
        if route == "learning_start":
            receipt = self._service.start_content(auth, body)
            return parse_receipt_data(receipt.response_json), 201 if receipt.created else 200
        if route == "content_report":
            receipt = self._service.report_content(
                auth,
                course_id=_path_id(params["courseId"]),
                enrollment_id=body["enrollmentId"],
                placement_id=body["courseItemLinkId"],
                report=body["report"],
            )
            return parse_receipt_data(receipt.response_json), 200
        if route == "attempt_create":
            receipt = self._service.start_attempt(auth, body)
            data = parse_receipt_data(receipt.response_json)
            data = {**data, "resumeCredential": self._resume(auth, data["attemptId"])}
            return data, 201 if receipt.created else 200
        if route == "attempt_get":
            return self._attempt_from_record(self._hook(self._load_attempt)(auth, params["attemptId"]), False), 200
        if route == "attempt_reauthorize":
            record = self._hook(self._reauthorize)(auth, params["attemptId"], body["resumeCredential"])
            data = self._attempt_from_record(record, False)
            return {**data, "resumeCredential": self._resume(auth, params["attemptId"])}, 200
        if route == "attempt_cancel":
            self._hook(self._cancel)(auth, params["attemptId"], map_cancel_reason(body["reason"]))
            return None, 204
        if route == "calculation_post":
            return self._calculation_http(self._hook(self._measurement_submit)(auth, params["attemptId"], event))
        if route == "calculation_get":
            return self._calculation_http(self._hook(self._calculation_result)(auth, params["attemptId"]))
        if route == "chart_link":
            record = self._hook(self._chart_link)(auth, params["attemptId"])
            return chart_link_data(url=record["url"], expires_at=record["expiresAt"]), 200
        raise CourseError("NOT_FOUND")

    def _handle_login(self, body):
        if body["loginId"] != _DUMMY_LOGIN:
            raise CourseError("CONTRACT_PENDING")
        session = self._hook(self._login)(body["loginId"], body["password"])
        auth = session["auth"]
        availability = {"state": "waiting", "reason": "arc_progress_unavailable"}
        try:
            availability = aggregate_availability(self._service.refresh_for_session(auth))
        except CourseError as error:
            if error.code in _AUTH_CODES:
                raise
            if error.code == "CONTRACT_PENDING":
                availability = {"state": "waiting", "reason": "contract_pending"}
        expires = session["expires_at"]
        if type(expires) is not str:
            expires = utc_timestamp(expires)
        return session_data(
            session_id=session["session_id"], expires_at=expires,
            learning_availability=availability, access_token=session["access_token"],
        ), 201

    def _handle_session(self, auth):
        snapshot = self._hook(self._session_reader)(auth)
        expires = snapshot["expires_at"]
        if type(expires) is not str:
            expires = utc_timestamp(expires)
        return session_data(
            session_id=snapshot["session_id"], expires_at=expires,
            learning_availability=snapshot["learning_availability"],
        )

    def _handle_refresh(self, auth):
        snapshot = self._hook(self._session_reader)(auth)
        availability = {"state": "waiting", "reason": "arc_progress_unavailable"}
        try:
            availability = aggregate_availability(self._service.refresh_for_session(auth))
        except CourseError as error:
            if error.code in _AUTH_CODES:
                raise
            if error.code == "CONTRACT_PENDING":
                availability = {"state": "waiting", "reason": "contract_pending"}
        expires = snapshot["expires_at"]
        if type(expires) is not str:
            expires = utc_timestamp(expires)
        return session_data(
            session_id=snapshot["session_id"], expires_at=expires, learning_availability=availability,
        )

    def _attempt_from_record(self, record, include_resume):
        data = attempt_view_data(
            attempt_id=record["attempt_id"], state=record["state"], created_at=record["created_at"],
            condition=record["condition"], course_id=record.get("course_id"),
            enrollment_id=record.get("enrollment_id"), course_item_link_id=record.get("course_item_link_id"),
            definition_hash=record.get("definition_hash"), role=record.get("role"),
            legacy=bool(record.get("legacy")),
        )
        if include_resume:
            credential = record.get("resume_credential")
            if type(credential) is not str or not credential:
                credential = self._resume(record.get("auth"), record["attempt_id"])
            data = {**data, "resumeCredential": credential}
        return data

    def _resume(self, auth, attempt_id):
        value = self._hook(self._issue_resume)(auth, attempt_id)
        if type(value) is not str or not value:
            raise CourseError("TEMPORARILY_UNAVAILABLE")
        return value

    def _calculation_http(self, record):
        state = record["state"]
        if state in ("created", "cancelled"):
            raise CourseError("INVALID_STATE")
        if state == "failed":
            raise CourseError(record.get("error_code") or "CALCULATION_FAILED")
        if state == "outcome_unknown":
            raise CourseError("CALCULATION_OUTCOME_UNKNOWN")
        if state in ("queued", "processing"):
            return calculation_view_data(attempt_id=record["attempt_id"], calculation_status="pending"), 202
        if state != "evaluated":
            raise CourseError("INVALID_STATE")
        return calculation_view_data(
            attempt_id=record["attempt_id"], calculation_status="succeeded",
            calculation=record.get("calculation"), evaluation=record.get("evaluation"),
            progress_application=record.get("progress_application"), submit_arc=record.get("submit_arc"),
        ), 200

    def _hook(self, hook):
        if not callable(hook):
            raise CourseError("TEMPORARILY_UNAVAILABLE")
        return hook

    def _error(self, error, request_id):
        timestamp = utc_timestamp(self._clock())
        return self._raw(error.status, json.dumps(error_envelope(error, timestamp=timestamp), allow_nan=False), request_id)

    def _raw(self, status, body, request_id):
        return {
            "statusCode": status,
            "headers": {
                "Content-Type": "application/json",
                "Cache-Control": "no-store",
                "X-Request-Id": request_id,
            },
            "body": body,
        }


def _path_id(value):
    return require_public_id(int(value))


def _parse_event(event):
    if type(event) is not dict or "type" not in event:
        raise CourseError("INVALID_REQUEST")
    event_type = event["type"]
    if type(event_type) is not str or event_type not in CONTENT_EVENT_TYPES:
        raise CourseError("INVALID_REQUEST")
    if event_type == "video_segments":
        if set(event) != {"type", "intervalsMs"}:
            raise CourseError("INVALID_REQUEST")
        intervals = event["intervalsMs"]
        if type(intervals) is not list or not intervals:
            raise CourseError("INVALID_REQUEST")
        owned = []
        for item in intervals:
            if type(item) is not list or len(item) != 2:
                raise CourseError("INVALID_REQUEST")
            start, end = item[0], item[1]
            if type(start) is not int or type(end) is not int:
                raise CourseError("INVALID_REQUEST")
            if not (0 <= start < end):
                raise CourseError("INVALID_REQUEST")
            owned.append((start, end))
        return "video_segments", tuple(owned), None
    if event_type == "document_displayed":
        if set(event) != {"type"}:
            raise CourseError("INVALID_REQUEST")
        return "document_displayed", (), None
    if set(event) != {"type", "displayReportId"}:
        raise CourseError("INVALID_REQUEST")
    return "document_confirmed", (), require_uuid(event["displayReportId"])


def _single_header(event, name):
    headers, multi = event.get("headers"), event.get("multiValueHeaders")
    headers = {} if headers is None else headers
    multi = {} if multi is None else multi
    if type(headers) is not dict or type(multi) is not dict:
        raise CourseError("INVALID_REQUEST")
    single = [value for key, value in headers.items() if type(key) is str and key.lower() == name]
    multiple = [value for key, value in multi.items() if type(key) is str and key.lower() == name]
    if len(single) > 1 or len(multiple) > 1:
        raise CourseError("INVALID_REQUEST")
    values = single
    if multiple:
        if type(multiple[0]) is not list or len(multiple[0]) != 1:
            raise CourseError("INVALID_REQUEST")
        if single and single[0] != multiple[0][0]:
            raise CourseError("INVALID_REQUEST")
        values = multiple[0]
    if not values:
        return None
    value = values[0]
    if type(value) is not str or not value or "\r" in value or "\n" in value:
        raise CourseError("INVALID_REQUEST")
    return value


def _bearer(event):
    value = _single_header(event, "authorization")
    if value is None:
        raise CourseError("SESSION_REQUIRED")
    if value[:7].lower() != "bearer " or len(value) < 8:
        raise CourseError("SESSION_REQUIRED")
    token = value[7:]
    if not token or " " in token:
        raise CourseError("SESSION_REQUIRED")
    return token

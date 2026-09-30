"""/api/v2 course HTTP boundary: route/method/query/body validation and envelopes.

``mock_journey.handler.handle`` hands every REST proxy event to
``CourseHttp.dispatch``, which returns ``{statusCode, headers, body}``. The API
is assembled by ``mock_journey.assembly.build_course_application``; the hooks
are a ``course_contracts.CourseHooks`` value bound in
``mock_journey.course_wiring.bind_course_http`` (D128), and every hook result is
one of the frozen records defined next to it. Trailing slashes are required and
no redirect is issued; any other path is 404.

1. measurement_submit(auth, attempt_id, event) -> CalculationRecord
   Invoked for POST /api/v2/attempts/{attemptId}/calculation/ with the event.
   This module does not parse CPR binary/multipart. The hook resolves an
   ambiguous upload Content-Type (400 INVALID_REQUEST) before any attempt read
   and then reuses CalculationService.submit / the legacy_bridge parser. HTTP
   maps created/cancelled→409 INVALID_STATE, queued/processing→202 pending,
   evaluated→200 succeeded, failed→503 CALCULATION_FAILED, outcome_unknown→503
   CALCULATION_OUTCOME_UNKNOWN. GET calculation_result must not execute
   calculation or submission.

2. issue_resume(auth, attempt_id) -> str
   Called only after ownership checks for attempt create replay and reauthorize.
   The hook uses AuthManager.resume_credential. The credential is added to the
   HTTP response only and must never be written into StartReceipt.response_json.

3. authenticate / login / logout / session_reader / session_check /
   load_attempt / reauthorize / cancel / chart_link adapt AuthManager, the typed
   JourneyService commands and CalculationService to AuthContext and CourseError.
   session_check serves POST /session/refresh/, which reports the availability of
   its own refresh and therefore never reads the stored course snapshot.
"""

import base64
import binascii
import json
import re
from types import MappingProxyType

from mock_journey.auth import LOGIN_ID
from mock_journey.course_contracts import (
    APP_ROUTES, CANCEL_REASON_WIRE_TO_INTERNAL, CONTENT_EVENT_TYPES, PAGE_DEFAULT, PAGE_SIZE_DEFAULT,
    PAGE_SIZE_MAX, PUBLIC_ID_MAX, START_REQUEST_FIELDS, UUID_PATTERN, AttemptRecord, CalculationRecord,
    ContentReport, CourseHooks, StartCommand, require_hash, require_public_id, require_uuid,
)
from mock_journey.course_errors import CourseError
from mock_journey.course_primitives import require_text
from mock_journey.course_response import (
    attempt_view_data, availability_or_waiting, calculation_view_data, chart_link_data,
    course_detail_data, error_envelope, parse_receipt_data, session_data, success_envelope,
    utc_timestamp,
)
from mock_journey.course_settings import require_course_settings
from mock_journey.errors import JourneyError
from mock_journey.typed import parse_json
from services.operational_logs import bound_identifiers, record_event, write_diagnostic


# Every client request check here answers 400 INVALID_REQUEST (D119 per path).
_INVALID = "INVALID_REQUEST"
_DUMMY_LOGIN = LOGIN_ID  # D13: the only login routed to the Dummy hook.
_POSITIVE = re.compile(r"[1-9][0-9]*\Z")
_LOGIN_ROUTE = next(spec for spec in APP_ROUTES if spec.route_id == "login")
# Public IDs are 1..PUBLIC_ID_MAX, so at most this many decimal digits. Path
# segments and query integers longer than that are refused before int().
_PUBLIC_ID_DIGITS = len(str(PUBLIC_ID_MAX))
_PATH_ID_PATTERN = rf"[1-9][0-9]{{0,{_PUBLIC_ID_DIGITS - 1}}}"
# Route rules that are not RouteSpec fields (RouteSpec stays the frozen contract DTO).
_REQUIRED_QUERY = MappingProxyType({
    "course_detail": ("enrollmentId",),
    "item_detail": ("enrollmentId",),
})
_QUERY_DEFAULTS = MappingProxyType({
    "course_list": (("page", PAGE_DEFAULT), ("pageSize", PAGE_SIZE_DEFAULT)),
})
# Body kinds read without the control-body byte limit: no body, or the
# measurement upload handled by the measurement_submit hook.
_UNLIMITED_BODY_KINDS = frozenset({"none", "measurement"})
# A measurement upload is only checked here (the parser decodes it once, later);
# the base64 text is validated in slices of whole quads so the decoded bytes are
# never held for the whole body.
_BASE64_SLICE = 64 * 1024


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
            parts.append(rf"(?P<attemptId>{UUID_PATTERN})")
        else:
            parts.append(rf"(?P<{name}>{_PATH_ID_PATTERN})")
        rest = rest[end + 1:]
    return re.compile(r"\A" + "".join(parts) + r"\Z"), names, spec


_COMPILED = tuple(_compile_path(spec) for spec in APP_ROUTES)


class CourseHttp:
    def __init__(self, service, settings, *, clock, uuid_factory, hooks):
        require_course_settings(settings, lambda: CourseError("TEMPORARILY_UNAVAILABLE"))
        if not callable(clock) or not callable(uuid_factory) or type(hooks) is not CourseHooks:
            raise CourseError("TEMPORARILY_UNAVAILABLE")
        self._service = service
        self._settings = settings
        self._clock = clock
        self._uuid_factory = uuid_factory
        self._hooks = hooks

    def dispatch(self, event):
        request_id = require_uuid(self._uuid_factory(), code=_INVALID)
        # Every operational record written for this request, including reused
        # journey hooks, carries the same id the app receives as X-Request-Id.
        with bound_identifiers(http_request_id=request_id):
            try:
                return self._dispatch(event, request_id)
            except CourseError as error:
                response = self._error(error, request_id)
                self._record_rejection(event, error)
                return response
            except JourneyError as error:
                error = CourseError(error.code)
                response = self._error(error, request_id)
                self._record_rejection(event, error)
                return response
            except Exception as error:
                response = self._error(CourseError("TEMPORARILY_UNAVAILABLE"), request_id)
                self._record_rejection(event, CourseError("TEMPORARILY_UNAVAILABLE"))
                # Shared diagnostic sanitizer: type and checkout frames only.
                write_diagnostic("error", "request_failed", {"exception": error})
                return response

    def _record_rejection(self, event, error):
        # Fail-open (N05). Only the fixed public code and status.
        login = (type(event) is dict and event.get("httpMethod") == _LOGIN_ROUTE.method
                 and event.get("path") == _LOGIN_ROUTE.path)
        record_event("login_failed" if login else "request_rejected", error_code=error.code, http_status=error.status)

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
        for key in _REQUIRED_QUERY.get(spec.route_id, ()):
            if key not in parsed:
                raise CourseError("INVALID_REQUEST")
        defaults = _QUERY_DEFAULTS.get(spec.route_id)
        if defaults is not None:
            for key, value in defaults:
                parsed.setdefault(key, value)
            if parsed["pageSize"] > PAGE_SIZE_MAX:
                raise CourseError("INVALID_REQUEST")
        return parsed

    def _query_int(self, value, key):
        # Reject above-contract integers before Python's decimal conversion
        # limit can turn a malformed client query into a 503 response.
        if len(value) > _PUBLIC_ID_DIGITS or _POSITIVE.fullmatch(value) is None:
            raise CourseError("INVALID_REQUEST")
        number = int(value)
        if key == "pageSize" and number > PAGE_SIZE_MAX:
            raise CourseError("INVALID_REQUEST")
        if number > PUBLIC_ID_MAX:
            raise CourseError("INVALID_REQUEST")
        return number

    def _body(self, event, spec):
        limit = (self._settings.max_control_body_bytes
                 if spec.body_kind not in _UNLIMITED_BODY_KINDS else None)
        raw = self._raw_body(event, max_bytes=limit, keep=spec.body_kind != "measurement")
        if spec.body_kind == "none":
            if raw:
                raise CourseError("INVALID_REQUEST")
            return None
        if spec.body_kind == "measurement":
            return None  # checked by _raw_body; decoded later by the parser
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

    def _raw_body(self, event, *, max_bytes=None, keep=True):
        """The body bytes, or None when ``keep`` is False and the body was only checked.

        With keep=False (measurement uploads) the same shape and encoding checks
        run, but the base64 text is validated without keeping the decoded bytes.
        """
        body = event.get("body")
        if body is None or body == "":
            return b""
        try:
            if event.get("isBase64Encoded"):
                if type(body) is not str:
                    raise CourseError("INVALID_REQUEST")
                # Bound allocation before decoding public login/control bodies.
                # The exact decoded limit is still checked by _body below.
                if max_bytes is not None and len(body) > 4 * ((max_bytes + 2) // 3):
                    raise CourseError("PAYLOAD_TOO_LARGE")
                if not keep:
                    check_base64(body)
                    return None
                return base64.b64decode(body, validate=True)
            if type(body) is bytes:
                return body
            if type(body) is str:
                if max_bytes is not None and len(body) > max_bytes:
                    raise CourseError("PAYLOAD_TOO_LARGE")
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
        # parse_json already refuses unencodable text; the UTF-8 check is kept as a guard.
        require_text(login_id, code=_INVALID, check_utf8=True)
        require_text(password, code=_INVALID, check_utf8=True)
        return {"loginId": login_id, "password": password}

    def _start_body(self, parsed):
        if type(parsed) is not dict or set(parsed) != set(START_REQUEST_FIELDS):
            raise CourseError("INVALID_REQUEST")
        return StartCommand(
            require_uuid(parsed["clientRequestId"], code=_INVALID),
            require_public_id(parsed["enrollmentId"], code=_INVALID),
            require_public_id(parsed["courseId"], code=_INVALID),
            require_public_id(parsed["courseItemLinkId"], code=_INVALID),
            require_hash(parsed["definitionHash"], code=_INVALID),
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
            "enrollmentId": require_public_id(parsed["enrollmentId"], code=_INVALID),
            "courseItemLinkId": require_public_id(parsed["courseItemLinkId"], code=_INVALID),
            "report": ContentReport(
                require_uuid(parsed["reportId"], code=_INVALID),
                require_uuid(parsed["startId"], code=_INVALID),
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
            out[key] = require_text(parsed[key], code=_INVALID, check_utf8=False)
        return out

    def _require_auth(self, event, *, allow_logout=False):
        token = _bearer(event)
        return self._hooks.authenticate(token, allow_logout_receipt=allow_logout)

    def _handle(self, spec, auth, params, query, body, event):
        route = spec.route_id
        if route == "login":
            return self._handle_login(body)
        if route == "session":
            return self._handle_session(auth), 200
        if route == "session_refresh":
            return self._handle_refresh(auth), 200
        if route == "logout":
            self._hooks.logout(auth)
            return None, 204
        if route == "course_list":
            return self._service.list_courses(auth, page=query["page"], page_size=query["pageSize"]), 200
        if route == "course_detail":
            view = self._service.get_course(
                auth, course_id=_path_id(params["courseId"]), enrollment_id=query["enrollmentId"],
            )
            return course_detail_data(view), 200
        if route == "item_detail":
            return self._service.get_item(
                auth,
                course_id=_path_id(params["courseId"]),
                enrollment_id=query["enrollmentId"],
                placement_id=_path_id(params["courseItemLinkId"]),
            ), 200
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
            return self._attempt_from_record(self._hooks.load_attempt(auth, params["attemptId"])), 200
        if route == "attempt_reauthorize":
            record = self._hooks.reauthorize(auth, params["attemptId"], body["resumeCredential"])
            data = self._attempt_from_record(record)
            return {**data, "resumeCredential": self._resume(auth, params["attemptId"])}, 200
        if route == "attempt_cancel":
            self._hooks.cancel(auth, params["attemptId"], map_cancel_reason(body["reason"]))
            return None, 204
        if route == "calculation_post":
            return self._calculation_http(self._hooks.measurement_submit(auth, params["attemptId"], event))
        if route == "calculation_get":
            return self._calculation_http(self._hooks.calculation_result(auth, params["attemptId"]))
        if route == "chart_link":
            record = self._hooks.chart_link(auth, params["attemptId"])
            return chart_link_data(url=record.url, expires_at=record.expires_at), 200
        raise CourseError("NOT_FOUND")

    def _handle_login(self, body):
        if body["loginId"] != _DUMMY_LOGIN:
            raise CourseError("CONTRACT_PENDING")
        session = self._hooks.login(body["loginId"], body["password"])
        # Not absorbed: an unexpected refresh failure stays a 503 response.
        availability = availability_or_waiting(
            self._service.refresh_for_session, session.auth, absorb_unexpected=False,
        )
        return session_data(
            session_id=session.session_id, expires_at=session.expires_at,
            learning_availability=availability, access_token=session.access_token,
            user_name=session.user_name,
        ), 201

    def _handle_session(self, auth):
        snapshot = self._hooks.session_reader(auth)
        return session_data(
            session_id=snapshot.session_id, expires_at=snapshot.expires_at,
            learning_availability=snapshot.learning_availability,
        )

    def _handle_refresh(self, auth):
        # The session is checked first; the availability comes from this refresh only.
        session = self._hooks.session_check(auth)
        availability = availability_or_waiting(
            self._service.refresh_for_session, auth, absorb_unexpected=False,
        )
        return session_data(
            session_id=session.session_id, expires_at=session.expires_at, learning_availability=availability,
        )

    def _attempt_from_record(self, record: AttemptRecord):
        # A resume credential is added only by the create/reauthorize routes via
        # issue_resume after ownership checks, never read back from a record.
        return attempt_view_data(
            attempt_id=record.attempt_id, state=record.state, created_at=record.created_at,
            condition=record.condition, course_id=record.course_id,
            enrollment_id=record.enrollment_id, course_item_link_id=record.course_item_link_id,
            definition_hash=record.definition_hash, role=record.role,
            legacy=record.legacy,
        )

    def _resume(self, auth, attempt_id):
        value = self._hooks.issue_resume(auth, attempt_id)
        return require_text(value, code="TEMPORARILY_UNAVAILABLE", check_utf8=False)

    def _calculation_http(self, record: CalculationRecord):
        state = record.state
        if state in ("created", "cancelled"):
            raise CourseError("INVALID_STATE")
        if state == "failed":
            raise CourseError(record.error_code or "CALCULATION_FAILED")
        if state == "outcome_unknown":
            raise CourseError("CALCULATION_OUTCOME_UNKNOWN")
        if state in ("queued", "processing"):
            return calculation_view_data(attempt_id=record.attempt_id, calculation_status="pending"), 202
        if state != "evaluated":
            raise CourseError("INVALID_STATE")
        return calculation_view_data(
            attempt_id=record.attempt_id, calculation_status="succeeded",
            calculation=record.calculation, evaluation=record.evaluation,
            progress_application=record.progress_application, submit_arc=record.submit_arc,
        ), 200

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


def check_base64(text):
    """Accept exactly what ``base64.b64decode(text, validate=True)`` accepts, slice by slice.

    Slices are whole quads, so every slice but the last must be padding-free
    alphabet text, and the last slice (at least one quad plus any tail, since
    the decoder forgives stray padding only after a complete quad) carries the
    padding verdict; the decoded bytes of each slice are dropped. Raises the
    decoder's ValueError/binascii error (callers map it to 400 INVALID_REQUEST).
    """
    if not text.isascii():
        raise ValueError("string argument should contain only ASCII characters")
    last = max(len(text) - 4, 0) // _BASE64_SLICE * _BASE64_SLICE
    for start in range(0, last, _BASE64_SLICE):
        piece = text[start:start + _BASE64_SLICE]
        if "=" in piece:
            raise binascii.Error("Excess data after padding")
        base64.b64decode(piece, validate=True)
    base64.b64decode(text[last:], validate=True)


def _path_id(value):
    return require_public_id(int(value), code=_INVALID)


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
    return "document_confirmed", (), require_uuid(event["displayReportId"], code=_INVALID)


def header_representation(event, name):
    """Resolve one REST proxy header's representation without judging its value.

    A header duplicated by letter case (in headers or in multiValueHeaders), a
    multiValueHeaders entry that is not a list of exactly one item, or a headers
    value that differs from the multiValueHeaders value is ambiguous
    (INVALID_REQUEST). Returns (values, multi_only): values is () when the
    header is absent, else a 1-tuple; multi_only is True when only
    multiValueHeaders carries it. Shared by _single_header (Authorization, JSON
    Content-Type) and course_wiring.measurement_event (upload Content-Type).
    """
    headers, multi = event.get("headers"), event.get("multiValueHeaders")
    headers = {} if headers is None else headers
    multi = {} if multi is None else multi
    if type(headers) is not dict or type(multi) is not dict:
        raise CourseError("INVALID_REQUEST")
    single = [value for key, value in headers.items() if type(key) is str and key.lower() == name]
    multiple = [value for key, value in multi.items() if type(key) is str and key.lower() == name]
    if len(single) > 1 or len(multiple) > 1:
        raise CourseError("INVALID_REQUEST")
    if not multiple:
        return tuple(single), False
    if type(multiple[0]) is not list or len(multiple[0]) != 1:
        raise CourseError("INVALID_REQUEST")
    if single and single[0] != multiple[0][0]:
        raise CourseError("INVALID_REQUEST")
    return (multiple[0][0],), not single


def _single_header(event, name):
    values, _ = header_representation(event, name)
    if not values:
        return None
    value = require_text(values[0], code=_INVALID, check_utf8=False)
    if "\r" in value or "\n" in value:
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

"""Pure VCC wire converters. DTO/bytes → camelCase data, not HTTP timestamp assembly."""

from datetime import datetime, timezone

from mock_journey.course_contracts import (
    ATTEMPT_VIEW_FIELDS, AVAILABILITY_FIELDS, CALCULATION_VIEW_FIELDS, CHART_LINK_FIELDS,
    CONDITION_KEYS, CONTENT_START_VIEW_FIELDS, COURSE_DETAIL_FIELDS, COURSE_ITEM_FIELDS,
    COURSE_LIST_ROW_FIELDS, COURSE_STATUSES, ENROLLMENT_FIELDS, ENROLLMENT_NULLABLE,
    EXCLUSION_REASON_ORDER, FILE_DETAIL_FIELDS, ITEM_DETAIL_OUTER_FIELDS, LOGIN_EXTRA_FIELDS,
    PAGE_DEFAULT, PAGE_SIZE_DEFAULT, PROGRESS_APPLICATIONS, PROGRESS_RECEIPT_FIELDS,
    RECEIPT_FORBIDDEN_KEYS, SESSION_VIEW_FIELDS, SUBMIT_ARC_FIELDS, SUMMARY_ITEM_FIELDS,
    SUCCESS_MESSAGE, TOKEN_TYPE_BEARER, TRAINING_PROGRAM_DETAIL_FIELDS, USAGE_VALUES,
    CourseView, RefreshResult, item_type_wire, parse_owned, require_hash, require_member,
    require_public_id, require_utc, require_uuid, scope_identity, validate_availability, validate_condition,
)
from mock_journey.course_errors import CourseError
from mock_journey.course_schema import (
    validate_course_metadata, validate_enrollment, validate_file_detail,
    validate_placement_detail, validate_placement_order, validate_training_detail,
)
from mock_journey.typed import digest, json_bytes, parse_json


_IDENTITY_LEAKS = frozenset({
    "source_id", "sourceId", "SourceId", "scope_key", "scopeKey", "learner_key", "learnerKey",
    "placement_key", "placementKey", "row_key", "rowKey", "principal", "token_hash",
    "password", "upstream", "source_progress", "sourceProgress",
})


def _fail(code="UPSTREAM_CONTRACT_MISMATCH"):
    raise CourseError(code)


def utc_timestamp(epoch_seconds):
    """Wire RFC3339 UTC. Envelope timestamp is response time, not a digest input."""
    if type(epoch_seconds) is bool or type(epoch_seconds) not in (int, float):
        _fail("TEMPORARILY_UNAVAILABLE")
    if type(epoch_seconds) is float and epoch_seconds != epoch_seconds:
        _fail("TEMPORARILY_UNAVAILABLE")
    dt = datetime.fromtimestamp(epoch_seconds, timezone.utc)
    if dt.microsecond == 0:
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    fraction = f"{dt.microsecond:06d}".rstrip("0")
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + fraction + "Z"


def _reject_leaks(value, *, secrets=False):
    forbidden = _IDENTITY_LEAKS | (RECEIPT_FORBIDDEN_KEYS if secrets else frozenset())
    if type(value) is dict:
        for key, nested in value.items():
            if key in forbidden:
                _fail()
            _reject_leaks(nested, secrets=secrets)
        return
    if type(value) is list:
        for nested in value:
            _reject_leaks(nested, secrets=secrets)


def _ordered(keys, values, *, nullable=()):
    if type(values) is not dict:
        _fail()
    out = {}
    for key in keys:
        if key not in values:
            _fail()
        value = values[key]
        if value is None and key not in nullable:
            _fail()
        out[key] = value
    _reject_leaks(out)
    return out


def _text(value):
    if type(value) is not str or not value:
        _fail()
    return value


def _exact_bool(value):
    if type(value) is not bool:
        _fail()
    return value


def availability_data(state, reason):
    state, reason = validate_availability(state, reason)
    return _ordered(AVAILABILITY_FIELDS, {"state": state, "reason": reason}, nullable=("reason",))


def aggregate_availability(refresh: RefreshResult):
    """D2 session Availability: inventory not ready wins; empty assignments ready."""
    if type(refresh) is not RefreshResult:
        _fail()
    inventory = refresh.inventory
    if inventory.state != "ready":
        return availability_data(inventory.state, inventory.reason)
    if not inventory.assignments:
        return availability_data("ready", None)
    gates = refresh.gates
    if any(gate.state == "reconciliation_required" for gate in gates):
        return availability_data("reconciliation_required", "progress_reconciliation_required")
    for gate in gates:
        if gate.state == "waiting":
            return availability_data("waiting", gate.reason)
    expected = {digest(scope_identity(binding.scope)) for binding in inventory.assignments}
    actual = {gate.scope_key for gate in gates if gate.epoch == inventory.epoch}
    if len(gates) != len(expected) or actual != expected:
        return availability_data("waiting", "arc_progress_unavailable")
    return availability_data("ready", None)


def session_data(*, session_id, expires_at, learning_availability, access_token=None):
    require_uuid(session_id)
    payload = {
        "sessionId": session_id,
        "expiresAt": require_utc(expires_at),
        "learningAvailability": _ordered(AVAILABILITY_FIELDS, learning_availability, nullable=("reason",)),
    }
    data = _ordered(SESSION_VIEW_FIELDS, payload, nullable=())
    if access_token is None:
        return data
    extra = {"accessToken": _text(access_token), "tokenType": TOKEN_TYPE_BEARER}
    return {**data, **_ordered(LOGIN_EXTRA_FIELDS, extra)}


def submit_arc_data(*, status, exclusion_reasons=()):
    require_member(status, {"disabled", "excluded"})
    if status == "disabled":
        if exclusion_reasons:
            _fail()
        payload = {
            "status": "disabled",
            "ok": False,
            "error": "arc_contract_pending",
            "exclusionReasons": [],
        }
        return _ordered(SUBMIT_ARC_FIELDS, payload, nullable=())
    ordered = []
    seen = set()
    for reason in EXCLUSION_REASON_ORDER:
        if reason in exclusion_reasons:
            ordered.append(reason)
            seen.add(reason)
    extra = [reason for reason in exclusion_reasons if reason not in seen]
    if extra or not ordered:
        _fail()
    payload = {
        "status": "excluded",
        "ok": False,
        "error": None,
        "exclusionReasons": ordered,
    }
    return _ordered(SUBMIT_ARC_FIELDS, payload, nullable=("error",))


def _progress_map(progress, bundle):
    """Project the stored policy snapshot into app placement IDs exactly once."""
    parsed = parse_owned(progress) if type(progress) is bytes else progress
    if type(parsed) is not dict:
        _fail()
    _course_status(parsed)
    items = parsed.get("items")
    if type(items) is not dict:
        _fail()
    projected = {}
    scope_key = digest(scope_identity(bundle.scope))
    for placement in bundle.placements:
        row = items.get(digest([scope_key, placement.source_id]))
        if type(row) is not dict or not {
            "source_id", "public_link_id", "kind", "content_version", "completed", "passed",
        } <= set(row):
            _fail()
        _text(row["content_version"])
        if (type(row.get("public_link_id")) is not int
                or row["public_link_id"] != placement.public_link_id
                or type(row.get("source_id")) is not type(placement.source_id)
                or row["source_id"] != placement.source_id
                or row.get("kind") != placement.kind):
            _fail()
        completed, passed = row.get("completed"), row.get("passed")
        if type(completed) is not bool or (passed is not None and type(passed) is not bool):
            _fail()
        if placement.kind in ("video", "document") and passed is not None:
            _fail()
        projected[str(placement.public_link_id)] = {"isCompleted": completed, "isPassed": passed}
    return parsed, projected


def _item_progress(items, link_id):
    row = items.get(str(link_id), items.get(link_id))
    if row is None:
        _fail()
    if type(row) is not dict:
        _fail()
    completed = row.get("isCompleted", False)
    passed = row.get("isPassed")
    if type(completed) is not bool:
        _fail()
    if passed is not None and type(passed) is not bool:
        _fail()
    return completed, passed


def _course_status(progress):
    status = progress.get("course_status")
    if type(status) is not str or status not in COURSE_STATUSES:
        _fail()
    return status


def _title_and_icon(placement):
    detail = validate_placement_detail(placement)
    title = detail.get("title")
    icon = detail.get("iconType")
    mapping = item_type_wire(placement.kind)
    if icon is None:
        icon = mapping["summary_item_type"]
    return _text(title), _text(icon), detail


def summary_item(placement):
    mapping = item_type_wire(placement.kind)
    title, _, _ = _title_and_icon(placement)
    return _ordered(SUMMARY_ITEM_FIELDS, {
        "id": require_public_id(placement.public_item_id),
        "itemType": mapping["summary_item_type"],
        "title": title,
        "displayOrder": placement.position,
    })


def course_item(placement, progress_items):
    mapping = item_type_wire(placement.kind)
    title, icon, detail = _title_and_icon(placement)
    description = detail.get("description")
    if description is not None and type(description) is not str:
        _fail()
    completed, passed = _item_progress(progress_items, placement.public_link_id)
    return _ordered(COURSE_ITEM_FIELDS, {
        "id": require_public_id(placement.public_item_id),
        "courseItemLinkId": require_public_id(placement.public_link_id),
        "step": placement.position,
        "title": title,
        "iconType": icon,
        "itemType": mapping["course_item_type"],
        "contentType": mapping["content_type"],
        "description": description,
        "isCompleted": completed,
        "isPassed": passed,
    }, nullable=("contentType", "description", "isPassed"))


def enrollment_data(course_json, enrollment_id, *, course_id=None):
    parsed = parse_owned(course_json) if type(course_json) is bytes else course_json
    if type(parsed) is not dict:
        _fail()
    metadata = parsed.get("enrollment_metadata")
    if type(metadata) is not dict:
        _fail()
    row = metadata.get(str(enrollment_id), metadata.get(enrollment_id))
    if type(row) is not dict:
        _fail()
    validate_enrollment(row, enrollment_id=enrollment_id, course_id=parsed.get("courseId") if course_id is None else course_id)
    return _ordered(ENROLLMENT_FIELDS, row, nullable=tuple(ENROLLMENT_NULLABLE))


def course_list_row(view: CourseView):
    if type(view) is not CourseView:
        _fail()
    course = parse_owned(view.bundle.course_json)
    validate_course_metadata(course, view.public_ids)
    progress, items = _progress_map(view.progress_json, view.bundle)
    placements = view.bundle.placements
    validate_placement_order(placements)
    row = {
        "courseId": require_public_id(view.public_ids.course_id),
        "courseName": _text(course.get("courseName")),
        "status": _course_status(progress),
        "summary": [summary_item(item) for item in placements],
        "certificationType": course.get("certificationType"),
        "enrollmentId": require_public_id(view.public_ids.enrollment_id),
        "progressId": require_public_id(view.public_ids.progress_id),
        "learningAvailability": availability_data(view.gate.state, view.gate.reason),
    }
    if row["certificationType"] is not None and type(row["certificationType"]) is not str:
        _fail()
    return _ordered(COURSE_LIST_ROW_FIELDS, row, nullable=("certificationType",))


def course_list_data(views, *, count, page, page_size, path="/api/v2/courses/progress/"):
    if type(views) not in (list, tuple):
        _fail()
    if type(count) is not int or count < 0 or type(page) is not int or type(page_size) is not int:
        _fail()
    if page < PAGE_DEFAULT or page_size < 1:
        _fail()
    rows = [course_list_row(view) for view in views]
    previous = None
    next_uri = None
    if page > PAGE_DEFAULT:
        previous = f"{path}?page={page - 1}&pageSize={page_size}"
    if (page - 1) * page_size + len(rows) < count:
        next_uri = f"{path}?page={page + 1}&pageSize={page_size}"
    data = {
        "results": rows,
        "count": count,
        "next": next_uri,
        "previous": previous,
    }
    _reject_leaks(data)
    return data


def course_detail_data(view: CourseView):
    if type(view) is not CourseView:
        _fail()
    _, items = _progress_map(view.progress_json, view.bundle)
    placements = view.bundle.placements
    validate_placement_order(placements)
    validate_course_metadata(parse_owned(view.bundle.course_json), view.public_ids)
    data = {
        "courseItems": [course_item(item, items) for item in placements],
        "enrollment": enrollment_data(view.bundle.course_json, view.public_ids.enrollment_id,
                                      course_id=view.public_ids.course_id),
        "progressId": require_public_id(view.public_ids.progress_id),
        "definitionHash": require_hash(view.bundle.definition_hash),
        "learningAvailability": availability_data(view.gate.state, view.gate.reason),
    }
    return _ordered(COURSE_DETAIL_FIELDS, data)


def _file_detail(detail):
    if detail is None:
        return None
    return _ordered(FILE_DETAIL_FIELDS, validate_file_detail(detail), nullable=("url", "contentUrl"))


def _training_detail(detail):
    if detail is None:
        return None
    return _ordered(TRAINING_PROGRAM_DETAIL_FIELDS, validate_training_detail(detail), nullable=())


def item_detail_data(view: CourseView, placement_id: int):
    if type(view) is not CourseView:
        _fail()
    require_public_id(placement_id)
    validate_placement_order(view.bundle.placements)
    for placement in view.bundle.placements:
        if placement.public_link_id != placement_id:
            continue
        mapping = item_type_wire(placement.kind)
        parsed = validate_placement_detail(placement)
        nested = parsed.get("detail")
        if placement.kind in ("video", "document"):
            nested_wire = _file_detail(nested)
        elif placement.kind in ("training", "assessment"):
            nested_wire = _training_detail(nested)
        else:
            _fail()
        usage = parsed.get("usage")
        require_member(usage, USAGE_VALUES)
        payload = {
            "id": require_public_id(parsed.get("id", placement.public_item_id)),
            "title": _text(parsed.get("title")),
            "itemType": mapping["detail_item_type"],
            "displayOrder": parsed.get("displayOrder", placement.position),
            "courseItemLinkId": require_public_id(placement.public_link_id),
            "usage": usage,
            "logicalId": require_uuid(parsed.get("logicalId")),
            "description": parsed.get("description"),
            "detail": nested_wire,
        }
        if type(payload["displayOrder"]) is not int:
            _fail()
        return _ordered(ITEM_DETAIL_OUTER_FIELDS, payload, nullable=("description", "detail"))
    raise CourseError("NOT_FOUND")


def content_start_data(*, start_id, course_id, enrollment_id, course_item_link_id, content_version, definition_hash):
    data = {
        "startId": require_uuid(start_id),
        "courseId": require_public_id(course_id),
        "enrollmentId": require_public_id(enrollment_id),
        "courseItemLinkId": require_public_id(course_item_link_id),
        "contentVersion": _text(content_version),
        "definitionHash": require_hash(definition_hash),
    }
    return _ordered(CONTENT_START_VIEW_FIELDS, data)


def progress_receipt_data(
    *, start_id, report_id, course_item_link_id, is_completed, is_passed, course_status, application,
):
    require_member(application, PROGRESS_APPLICATIONS)
    if application == "historical_only":
        is_completed = None
        is_passed = None
        course_status = None
    elif type(is_completed) is not bool:
        _fail()
    else:
        if is_passed is not None and type(is_passed) is not bool:
            _fail()
        require_member(course_status, COURSE_STATUSES)
    data = {
        "startId": require_uuid(start_id),
        "reportId": require_uuid(report_id),
        "courseItemLinkId": require_public_id(course_item_link_id),
        "isCompleted": is_completed,
        "isPassed": is_passed,
        "courseStatus": course_status,
        "application": application,
    }
    return _ordered(
        PROGRESS_RECEIPT_FIELDS, data,
        nullable=("isCompleted", "isPassed", "courseStatus"),
    )


def _condition_wire(condition):
    validated = validate_condition(condition)
    return {key: validated[key] for key in CONDITION_KEYS}


def attempt_view_data(
    *, attempt_id, state, created_at, condition, course_id=None, enrollment_id=None,
    course_item_link_id=None, definition_hash=None, role=None, legacy=False,
):
    require_member(state, {
        "created", "queued", "processing", "evaluated", "cancelled", "failed", "outcome_unknown",
    })
    if legacy:
        data = {
            "attemptId": require_uuid(attempt_id),
            "state": state,
            "courseId": None,
            "enrollmentId": None,
            "courseItemLinkId": None,
            "definitionHash": None,
            "createdAt": require_utc(created_at),
            "role": None,
            "condition": _condition_wire(condition),
        }
        return _ordered(
            ATTEMPT_VIEW_FIELDS, data,
            nullable=("courseId", "enrollmentId", "courseItemLinkId", "definitionHash", "role"),
        )
    if None in (course_id, enrollment_id, course_item_link_id, definition_hash, role):
        _fail()
    require_member(role, {"training", "final_assessment"})
    data = {
        "attemptId": require_uuid(attempt_id),
        "state": state,
        "courseId": require_public_id(course_id),
        "enrollmentId": require_public_id(enrollment_id),
        "courseItemLinkId": require_public_id(course_item_link_id),
        "definitionHash": require_hash(definition_hash),
        "createdAt": require_utc(created_at),
        "role": role,
        "condition": _condition_wire(condition),
    }
    return _ordered(ATTEMPT_VIEW_FIELDS, data)


def calculation_view_data(
    *, attempt_id, calculation_status, calculation=None, evaluation=None,
    progress_application=None, submit_arc=None,
):
    require_member(calculation_status, {"pending", "succeeded"})
    if calculation_status == "pending":
        calculation = None
        evaluation = None
        progress_application = None
        submit_arc = submit_arc_data(status="disabled")
    else:
        if type(calculation) is not dict or type(evaluation) is not dict or type(progress_application) is not dict:
            _fail("STORED_INPUT_INVALID")
        if submit_arc is None:
            submit_arc = submit_arc_data(status="disabled")
        if type(submit_arc) is not dict:
            _fail()
        if submit_arc.get("ok") is True:
            _fail()
        require_member(submit_arc.get("status"), {"disabled", "excluded"})
        submit_arc = _ordered(SUBMIT_ARC_FIELDS, submit_arc, nullable=("error",))
    data = {
        "attemptId": require_uuid(attempt_id),
        "calculationStatus": calculation_status,
        "calculation": calculation,
        "evaluation": evaluation,
        "progressApplication": progress_application,
        "submit_arc": submit_arc,
    }
    out = _ordered(
        CALCULATION_VIEW_FIELDS, data,
        nullable=("calculation", "evaluation", "progressApplication"),
    )
    return out


def chart_link_data(*, url, expires_at):
    if url is None and expires_at is None:
        return _ordered(CHART_LINK_FIELDS, {"url": None, "expiresAt": None}, nullable=CHART_LINK_FIELDS)
    if url is None or expires_at is None:
        _fail()
    data = {"url": _text(url), "expiresAt": require_utc(expires_at)}
    return _ordered(CHART_LINK_FIELDS, data)


def success_envelope(data, *, timestamp):
    body = {
        "success": True,
        "data": data,
        "message": SUCCESS_MESSAGE,
        "timestamp": require_utc(timestamp),
    }
    return body


def error_envelope(error: CourseError, *, timestamp):
    if type(error) is not CourseError:
        _fail("TEMPORARILY_UNAVAILABLE")
    return {
        "success": False,
        "error": {"code": error.code, "message": error.message, "details": None},
        "timestamp": require_utc(timestamp),
    }


def course_list_bytes(views, *, count, page, page_size):
    return json_bytes(course_list_data(views, count=count, page=page, page_size=page_size))


def item_detail_bytes(view, placement_id):
    return json_bytes(item_detail_data(view, placement_id))


def parse_receipt_data(response_json):
    parsed = parse_json(response_json) if type(response_json) is bytes else parse_owned(response_json)
    if type(parsed) is not dict:
        _fail()
    _reject_leaks(parsed, secrets=True)
    return parsed

"""Frozen VCC internal contract v1. No SDK, files, sockets, or environment reads."""

from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Protocol
import re

from mock_journey.course_errors import COURSE_ERROR_CODES, CourseError
from mock_journey.course_settings import CourseSettings
from mock_journey.models import AuthContext
from mock_journey.typed import digest, json_bytes, parse_json


CONTRACT_VERSION = "vcc-internal-v1"
POLICY_VERSION = "vcc-policy-v1"
FIXTURE_MAPPING_VERSION = "vcc-fixture-mapping-v1"
FIXTURE_CATALOG_VERSION = "vcc-fixture-catalog-v1"
PAGE_DEFAULT = 1
PAGE_SIZE_DEFAULT = 100
PAGE_SIZE_MAX = 1000
PUBLIC_ID_MAX = 9007199254740991
SUCCESS_MESSAGE = "OK"
TOKEN_TYPE_BEARER = "Bearer"

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_UTC = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z\Z")

PLACEMENT_KINDS = frozenset({"video", "document", "training", "assessment"})
EXECUTION_STATUSES = frozenset({"ready", "absent", "unsupported", "contract_pending"})
START_ROLES = frozenset({"training", "final_assessment"})
START_KINDS = frozenset({"content", "attempt"})
CONTENT_EVENT_TYPES = frozenset({"video_segments", "document_displayed", "document_confirmed"})
AVAILABILITY_STATES = frozenset({"ready", "waiting", "reconciliation_required"})
WAITING_REASONS = frozenset({"arc_progress_unavailable", "contract_pending"})
RECONCILIATION_REASON = "progress_reconciliation_required"
FINAL_PHASES = frozenset({"free", "active", "recovery_required", "policy_pending", "passed"})
COURSE_STATUSES = frozenset({"NOT_STARTED", "IN_PROGRESS", "FINISHED", "CERTIFIED"})
PROGRESS_APPLICATIONS = frozenset({
    "applied", "unchanged", "pending_evidence", "pending_reconciliation", "historical_only",
})
CALCULATION_STATUSES = frozenset({"pending", "succeeded"})
SUBMIT_ARC_STATUSES = frozenset({"disabled", "excluded"})
EXCLUSION_REASON_ORDER = ("dummy", "non_arc_guideline", "progress_reset_before_result")
EXCLUSION_REASONS = frozenset(EXCLUSION_REASON_ORDER)
ATTEMPT_STATES = frozenset({
    "created", "queued", "processing", "evaluated", "cancelled", "failed", "outcome_unknown",
})
CANDIDATE_STATES = frozenset({
    "valid", "absent_uncommitted", "missing_committed", "invalid", "unreadable",
})
RECOVERY_ACTIONS = frozenset({
    "resume_candidate", "retry_local_call", "wait_configuration", "wait_integrity", "close_terminal",
})
USAGE_VALUES = frozenset({"OWNED", "ITEM_REFERENCE", "COURSE_SNAPSHOT"})
GOAL_KINDS = frozenset({"compressions", "ventilations", "cycles"})
EXECUTION_KEYS = (
    "condition", "calculation_profile", "profile_version", "adapter_version",
    "projection_version", "goal", "catalog_version",
)
CONDITION_KEYS = ("mode", "target", "training_type", "guideline", "cpr_cycle_type", "is_2rescuers")
CALCULATION_PROFILE_KEYS = frozenset({"Custom", "Open_Skill", "Usage", "Organization"})
CONTENT_IDENTITY_KEYS = ("source_item_id", "training_program_id", "asset_ids")
CANCEL_REASON_WIRE_TO_INTERNAL = MappingProxyType({
    "user_cancelled": "user_stopped",
    "connection_lost": "manikin_disconnected",
})
ITEM_TYPE_WIRE = MappingProxyType({
    "video": MappingProxyType({
        "summary_item_type": "video",
        "course_item_type": "content",
        "content_type": "video",
        "detail_item_type": "content",
    }),
    "document": MappingProxyType({
        "summary_item_type": "pdf",
        "course_item_type": "content",
        "content_type": "pdf",
        "detail_item_type": "content",
    }),
    "training": MappingProxyType({
        "summary_item_type": "training",
        "course_item_type": "training",
        "content_type": None,
        "detail_item_type": "training",
    }),
    "assessment": MappingProxyType({
        "summary_item_type": "assessment",
        "course_item_type": "assessment",
        "content_type": None,
        "detail_item_type": "assessment",
    }),
})
RECEIPT_FORBIDDEN_KEYS = frozenset({
    "resumeCredential", "resume_credential", "accessToken", "access_token", "password",
    "Authorization", "authorization", "cookie", "Cookie", "signedUrl", "signed_url",
    "chartUrl", "chart_url", "chart_dataset_url",
})
DIGEST_EXCLUDED_WIRE_FIELDS = frozenset({
    "timestamp", "accessToken", "resumeCredential", "url", "expiresAt",
})
SESSION_VIEW_FIELDS = ("sessionId", "expiresAt", "learningAvailability")
LOGIN_EXTRA_FIELDS = ("accessToken", "tokenType")
AVAILABILITY_FIELDS = ("state", "reason")
COURSE_LIST_ROW_FIELDS = (
    "courseId", "courseName", "status", "summary", "certificationType",
    "enrollmentId", "progressId", "learningAvailability",
)
SUMMARY_ITEM_FIELDS = ("id", "itemType", "title", "displayOrder")
COURSE_DETAIL_FIELDS = (
    "courseItems", "enrollment", "progressId", "definitionHash", "learningAvailability",
)
COURSE_ITEM_FIELDS = (
    "id", "courseItemLinkId", "step", "title", "iconType", "itemType", "contentType",
    "description", "isCompleted", "isPassed",
)
ENROLLMENT_FIELDS = (
    "id", "status", "courseTitle", "courseId", "loginAt", "finishedAt",
    "elapsedSeconds", "centerName", "enrollStatusCode",
)
ENROLLMENT_NULLABLE = frozenset({
    "loginAt", "finishedAt", "elapsedSeconds", "centerName", "enrollStatusCode",
})
ITEM_DETAIL_OUTER_FIELDS = (
    "id", "title", "itemType", "displayOrder", "courseItemLinkId",
    "usage", "logicalId", "description", "detail",
)
FILE_DETAIL_FIELDS = ("id", "fileName", "order", "url", "contentUrl")
TRAINING_PROGRAM_DETAIL_FIELDS = (
    "id", "title", "trainingType", "feedbackType", "trainingMode", "training", "assessment", "content",
)
CONTENT_START_VIEW_FIELDS = (
    "startId", "courseId", "enrollmentId", "courseItemLinkId", "contentVersion", "definitionHash",
)
PROGRESS_RECEIPT_FIELDS = (
    "startId", "reportId", "courseItemLinkId", "isCompleted", "isPassed", "courseStatus", "application",
)
ATTEMPT_VIEW_FIELDS = (
    "attemptId", "state", "courseId", "enrollmentId", "courseItemLinkId",
    "definitionHash", "createdAt", "role", "condition",
)
CALCULATION_VIEW_FIELDS = (
    "attemptId", "calculationStatus", "calculation", "evaluation", "progressApplication", "submit_arc",
)
START_REQUEST_FIELDS = (
    "clientRequestId", "courseId", "enrollmentId", "courseItemLinkId", "definitionHash",
)
CHART_LINK_FIELDS = ("url", "expiresAt")
SUBMIT_ARC_FIELDS = ("status", "ok", "error", "exclusionReasons")


def _fail():
    raise CourseError("INVALID_REQUEST")


def _text(value):
    if type(value) is not str or not value:
        _fail()
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise CourseError("INVALID_REQUEST") from None
    return value


def _exact_bool(value):
    if type(value) is not bool:
        _fail()
    return value


def _json_int(value):
    if type(value) is not int:
        _fail()
    return value


def _natural_int(value):
    if type(value) is not int or value < 0:
        _fail()
    return value


def _positive_int(value):
    if type(value) is not int or value <= 0:
        _fail()
    return value


def require_source_id(value, *, allow_null=False):
    if value is None:
        if allow_null:
            return None
        _fail()
    if type(value) is int or type(value) is str:
        if type(value) is str:
            _text(value)
        return value
    _fail()


def require_public_id(value):
    if type(value) is not int or value < 1 or value > PUBLIC_ID_MAX:
        _fail()
    return value


def require_uuid(value):
    _text(value)
    if _UUID.fullmatch(value) is None:
        _fail()
    return value


def require_hash(value):
    _text(value)
    if _HASH.fullmatch(value) is None:
        _fail()
    return value


def require_utc(value):
    _text(value)
    if _UTC.fullmatch(value) is None:
        _fail()
    return value


def require_member(value, allowed):
    _text(value)
    if value not in allowed:
        _fail()
    return value


def owned_json_bytes(value, *, allow_none=False):
    if value is None:
        if allow_none:
            return None
        _fail()
    try:
        if type(value) is bytes:
            parsed = parse_json(value)
        elif type(value) in (dict, list):
            parsed = parse_json(json_bytes(value))
        else:
            _fail()
        return json_bytes(parsed)
    except CourseError:
        raise
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise CourseError("INVALID_REQUEST") from None


def parse_owned(body):
    try:
        return parse_json(body)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise CourseError("INVALID_REQUEST") from None


def _forbid_receipt_secrets(value):
    if type(value) is dict:
        for key, nested in value.items():
            if key in RECEIPT_FORBIDDEN_KEYS:
                _fail()
            _forbid_receipt_secrets(nested)
        return
    if type(value) is list:
        for nested in value:
            _forbid_receipt_secrets(nested)


def _owned_tuple(value, builder):
    if type(value) is tuple:
        items = value
    elif type(value) is list:
        items = tuple(value)
    else:
        _fail()
    return tuple(builder(item) for item in items)


def _owned_intervals(value):
    def pair(item):
        if type(item) not in (list, tuple) or len(item) != 2:
            _fail()
        start, end = item[0], item[1]
        if type(start) is not int or type(end) is not int:
            _fail()
        return (start, end)
    return _owned_tuple(value, pair)


def validate_content_identity(value):
    if type(value) is not dict or set(value) != set(CONTENT_IDENTITY_KEYS):
        _fail()
    require_source_id(value["source_item_id"])
    require_source_id(value["training_program_id"], allow_null=True)
    if type(value["asset_ids"]) is not list:
        _fail()
    return {
        "source_item_id": value["source_item_id"],
        "training_program_id": value["training_program_id"],
        "asset_ids": [require_source_id(item) for item in value["asset_ids"]],
    }


def validate_condition(value):
    if type(value) is not dict or set(value) != set(CONDITION_KEYS):
        _fail()
    return {
        "mode": _text(value["mode"]),
        "target": _text(value["target"]),
        "training_type": _text(value["training_type"]),
        "guideline": _text(value["guideline"]),
        "cpr_cycle_type": _text(value["cpr_cycle_type"]),
        "is_2rescuers": _exact_bool(value["is_2rescuers"]),
    }


def validate_goal(value):
    if type(value) is not dict or set(value) != {"kind", "required"}:
        _fail()
    return {"kind": require_member(value["kind"], GOAL_KINDS), "required": _positive_int(value["required"])}


def validate_execution_definition(value):
    """Completed 7-key execution. A 5-key Catalog definition is not ready."""
    if type(value) is not dict or tuple(sorted(value)) != tuple(sorted(EXECUTION_KEYS)):
        _fail()
    profile = value["calculation_profile"]
    if type(profile) is not dict or not set(profile) <= CALCULATION_PROFILE_KEYS:
        _fail()
    execution = {
        "condition": validate_condition(value["condition"]),
        "calculation_profile": parse_owned(json_bytes(profile)),
        "profile_version": _text(value["profile_version"]),
        "adapter_version": _text(value["adapter_version"]),
        "projection_version": _text(value["projection_version"]),
        "goal": validate_goal(value["goal"]),
        "catalog_version": _text(value["catalog_version"]),
    }
    return execution


def validate_availability(state, reason):
    require_member(state, AVAILABILITY_STATES)
    if state == "ready":
        if reason is not None:
            _fail()
        return state, None
    if state == "waiting":
        return state, require_member(reason, WAITING_REASONS)
    return state, require_member(reason, {RECONCILIATION_REASON})


def item_type_wire(kind):
    require_member(kind, PLACEMENT_KINDS)
    return dict(ITEM_TYPE_WIRE[kind])


@dataclass(frozen=True)
class RouteSpec:
    route_id: str
    method: str
    path: str
    auth_required: bool
    query_allowed: tuple[str, ...]
    body_kind: str
    success_statuses: tuple[int, ...]

    def __post_init__(self):
        _text(self.route_id)
        require_member(self.method, {"GET", "POST", "PUT", "DELETE"})
        _text(self.path)
        if not self.path.startswith("/api/v2/") or not self.path.endswith("/"):
            _fail()
        _exact_bool(self.auth_required)
        object.__setattr__(self, "query_allowed", _owned_tuple(self.query_allowed, _text))
        require_member(self.body_kind, {
            "none", "json_object", "login", "empty_object", "start_request",
            "content_report", "reauthorize", "cancel", "measurement",
        })
        object.__setattr__(self, "success_statuses", _owned_tuple(self.success_statuses, _positive_int))


APP_ROUTES = (
    RouteSpec("login", "POST", "/api/v2/sessions/", False, (), "login", (201,)),
    RouteSpec("session", "GET", "/api/v2/session/", True, (), "none", (200,)),
    RouteSpec("session_refresh", "POST", "/api/v2/session/refresh/", True, (), "empty_object", (200,)),
    RouteSpec("logout", "DELETE", "/api/v2/session/", True, (), "none", (204,)),
    RouteSpec("course_list", "GET", "/api/v2/courses/progress/", True, ("page", "pageSize"), "none", (200,)),
    RouteSpec("course_detail", "GET", "/api/v2/courses/{courseId}/progress/", True, ("enrollmentId",), "none", (200,)),
    RouteSpec("item_detail", "GET", "/api/v2/courses/{courseId}/items/{courseItemLinkId}/", True, ("enrollmentId",), "none", (200,)),
    RouteSpec("learning_start", "POST", "/api/v2/learning-starts/", True, (), "start_request", (201, 200)),
    RouteSpec("content_report", "PUT", "/api/v2/courses/{courseId}/progress/", True, (), "content_report", (200,)),
    RouteSpec("attempt_create", "POST", "/api/v2/attempts/", True, (), "start_request", (201, 200)),
    RouteSpec("attempt_get", "GET", "/api/v2/attempts/{attemptId}/", True, (), "none", (200,)),
    RouteSpec("attempt_reauthorize", "POST", "/api/v2/attempts/{attemptId}/reauthorize/", True, (), "reauthorize", (200,)),
    RouteSpec("attempt_cancel", "POST", "/api/v2/attempts/{attemptId}/cancel/", True, (), "cancel", (204,)),
    RouteSpec("calculation_post", "POST", "/api/v2/attempts/{attemptId}/calculation/", True, (), "measurement", (202, 200)),
    RouteSpec("calculation_get", "GET", "/api/v2/attempts/{attemptId}/calculation/", True, (), "none", (202, 200)),
    RouteSpec("chart_link", "GET", "/api/v2/attempts/{attemptId}/chart-link/", True, (), "none", (200,)),
)


def allowed_methods_for_path(path):
    return tuple(route.method for route in APP_ROUTES if route.path == path)


@dataclass(frozen=True)
class LearnerContext:
    provider: str
    tenant_id: object
    learner_id: object
    principal: str
    is_dummy: bool

    def __post_init__(self):
        _text(self.provider)
        object.__setattr__(self, "tenant_id", require_source_id(self.tenant_id))
        object.__setattr__(self, "learner_id", require_source_id(self.learner_id))
        _text(self.principal)
        _exact_bool(self.is_dummy)


@dataclass(frozen=True)
class CourseScope:
    learner: LearnerContext
    enrollment_id: object
    course_id: object

    def __post_init__(self):
        if type(self.learner) is not LearnerContext:
            _fail()
        object.__setattr__(self, "enrollment_id", require_source_id(self.enrollment_id))
        object.__setattr__(self, "course_id", require_source_id(self.course_id))


@dataclass(frozen=True)
class PublicIds:
    course_id: int
    enrollment_id: int
    progress_id: int

    def __post_init__(self):
        require_public_id(self.course_id)
        require_public_id(self.enrollment_id)
        require_public_id(self.progress_id)


@dataclass(frozen=True)
class AssignmentBinding:
    scope: CourseScope
    public_ids: PublicIds

    def __post_init__(self):
        if type(self.scope) is not CourseScope or type(self.public_ids) is not PublicIds:
            _fail()


@dataclass(frozen=True)
class Placement:
    source_id: object
    public_link_id: int
    public_item_id: int
    position: int
    kind: str
    content_version: str
    content_identity_json: bytes
    detail_json: bytes
    execution_json: bytes | None
    execution_status: str
    duration_ms: int | None

    def __post_init__(self):
        object.__setattr__(self, "source_id", require_source_id(self.source_id))
        require_public_id(self.public_link_id)
        require_public_id(self.public_item_id)
        _json_int(self.position)
        require_member(self.kind, PLACEMENT_KINDS)
        _text(self.content_version)
        identity = owned_json_bytes(self.content_identity_json)
        validate_content_identity(parse_owned(identity))
        object.__setattr__(self, "content_identity_json", identity)
        detail = owned_json_bytes(self.detail_json)
        if type(parse_owned(detail)) is not dict:
            _fail()
        object.__setattr__(self, "detail_json", detail)
        require_member(self.execution_status, EXECUTION_STATUSES)
        execution = owned_json_bytes(self.execution_json, allow_none=True)
        if self.execution_status == "ready":
            if execution is None:
                _fail()
            validate_execution_definition(parse_owned(execution))
        elif execution is not None:
            _fail()
        object.__setattr__(self, "execution_json", execution)
        if self.kind == "video":
            _positive_int(self.duration_ms)
        elif self.duration_ms is not None:
            _fail()


@dataclass(frozen=True)
class CourseBundle:
    scope: CourseScope
    public_ids: PublicIds
    source_revision: object
    mapping_version: str
    definition_hash: str
    placements: tuple
    course_json: bytes
    source_progress_json: bytes

    def __post_init__(self):
        if type(self.scope) is not CourseScope or type(self.public_ids) is not PublicIds:
            _fail()
        object.__setattr__(self, "source_revision", require_source_id(self.source_revision, allow_null=True))
        _text(self.mapping_version)
        require_hash(self.definition_hash)
        owned = _owned_tuple(self.placements, lambda item: item if type(item) is Placement else _fail())
        object.__setattr__(self, "placements", owned)
        course = owned_json_bytes(self.course_json)
        progress = owned_json_bytes(self.source_progress_json)
        if type(parse_owned(course)) is not dict or type(parse_owned(progress)) is not dict:
            _fail()
        object.__setattr__(self, "course_json", course)
        object.__setattr__(self, "source_progress_json", progress)


@dataclass(frozen=True)
class StartCommand:
    request_id: str
    enrollment_id: int
    course_id: int
    placement_id: int
    definition_hash: str

    def __post_init__(self):
        require_uuid(self.request_id)
        require_public_id(self.enrollment_id)
        require_public_id(self.course_id)
        require_public_id(self.placement_id)
        require_hash(self.definition_hash)


@dataclass(frozen=True)
class ContentReport:
    report_id: str
    start_id: str
    content_version: str
    event_type: str
    intervals_ms: tuple = ()
    display_report_id: str | None = None

    def __post_init__(self):
        require_uuid(self.report_id)
        require_uuid(self.start_id)
        _text(self.content_version)
        require_member(self.event_type, CONTENT_EVENT_TYPES)
        owned = _owned_intervals(self.intervals_ms)
        object.__setattr__(self, "intervals_ms", owned)
        if self.event_type == "video_segments":
            if not owned or self.display_report_id is not None:
                _fail()
            return
        if owned:
            _fail()
        if self.event_type == "document_displayed":
            if self.display_report_id is not None:
                _fail()
            return
        object.__setattr__(self, "display_report_id", require_uuid(self.display_report_id))


@dataclass(frozen=True)
class CourseBinding:
    scope_key: str
    placement_key: str
    start_role: str
    definition_hash: str
    content_version: str
    epoch: str
    policy_version: str

    def __post_init__(self):
        require_hash(self.scope_key)
        require_hash(self.placement_key)
        require_member(self.start_role, START_ROLES)
        require_hash(self.definition_hash)
        _text(self.content_version)
        _text(self.epoch)
        _text(self.policy_version)


@dataclass(frozen=True)
class InventoryTicket:
    learner_key: str
    epoch: str
    generation: int

    def __post_init__(self):
        require_hash(self.learner_key)
        _text(self.epoch)
        _natural_int(self.generation)


@dataclass(frozen=True)
class InventoryView:
    learner_key: str
    epoch: str
    generation: int
    revision: int
    state: str
    reason: str | None
    assignments: tuple

    def __post_init__(self):
        require_hash(self.learner_key)
        _text(self.epoch)
        _natural_int(self.generation)
        _natural_int(self.revision)
        state, reason = validate_availability(self.state, self.reason)
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "reason", reason)
        owned = _owned_tuple(self.assignments, lambda item: item if type(item) is AssignmentBinding else _fail())
        object.__setattr__(self, "assignments", owned)


@dataclass(frozen=True)
class RefreshTicket:
    scope_key: str
    epoch: str
    inventory_generation: int
    generation: int
    head_revision: int

    def __post_init__(self):
        require_hash(self.scope_key)
        _text(self.epoch)
        _natural_int(self.inventory_generation)
        _natural_int(self.generation)
        _natural_int(self.head_revision)


@dataclass(frozen=True)
class GateView:
    scope_key: str
    epoch: str
    state: str
    reason: str | None
    revision: int
    definition_hash: str | None

    def __post_init__(self):
        require_hash(self.scope_key)
        _text(self.epoch)
        state, reason = validate_availability(self.state, self.reason)
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "reason", reason)
        _natural_int(self.revision)
        if self.definition_hash is not None:
            require_hash(self.definition_hash)


@dataclass(frozen=True)
class RefreshResult:
    inventory: InventoryView
    gates: tuple

    def __post_init__(self):
        if type(self.inventory) is not InventoryView:
            _fail()
        owned = _owned_tuple(self.gates, lambda item: item if type(item) is GateView else _fail())
        object.__setattr__(self, "gates", owned)


@dataclass(frozen=True)
class CourseView:
    scope_key: str
    public_ids: PublicIds
    bundle: CourseBundle
    progress_json: bytes
    gate: GateView
    inventory: InventoryView

    def __post_init__(self):
        require_hash(self.scope_key)
        if type(self.public_ids) is not PublicIds or type(self.bundle) is not CourseBundle:
            _fail()
        progress = owned_json_bytes(self.progress_json)
        if type(parse_owned(progress)) is not dict:
            _fail()
        object.__setattr__(self, "progress_json", progress)
        if type(self.gate) is not GateView or type(self.inventory) is not InventoryView:
            _fail()


@dataclass(frozen=True)
class StartReceipt:
    created: bool
    start_id: str | None
    attempt_id: str | None
    scope_key: str
    placement_key: str
    definition_hash: str
    content_version: str
    bound_session_id: str
    epoch: str
    response_json: bytes

    def __post_init__(self):
        _exact_bool(self.created)
        require_hash(self.scope_key)
        require_hash(self.placement_key)
        require_hash(self.definition_hash)
        _text(self.content_version)
        require_uuid(self.bound_session_id)
        _text(self.epoch)
        start_id = None if self.start_id is None else require_uuid(self.start_id)
        attempt_id = None if self.attempt_id is None else require_uuid(self.attempt_id)
        if (start_id is None) == (attempt_id is None):
            _fail()
        object.__setattr__(self, "start_id", start_id)
        object.__setattr__(self, "attempt_id", attempt_id)
        body = owned_json_bytes(self.response_json)
        parsed = parse_owned(body)
        if type(parsed) is not dict:
            _fail()
        _forbid_receipt_secrets(parsed)
        object.__setattr__(self, "response_json", body)


@dataclass(frozen=True)
class StoredProgressReceipt:
    scope_key: str
    start_id: str
    report_id: str
    response_json: bytes

    def __post_init__(self):
        require_hash(self.scope_key)
        require_uuid(self.start_id)
        require_uuid(self.report_id)
        body = owned_json_bytes(self.response_json)
        parsed = parse_owned(body)
        if type(parsed) is not dict:
            _fail()
        _forbid_receipt_secrets(parsed)
        object.__setattr__(self, "response_json", body)


@dataclass(frozen=True)
class AttemptTemplate:
    existing_template_json: bytes
    binding: CourseBinding

    def __post_init__(self):
        body = owned_json_bytes(self.existing_template_json)
        if type(parse_owned(body)) is not dict:
            _fail()
        object.__setattr__(self, "existing_template_json", body)
        if type(self.binding) is not CourseBinding:
            _fail()


@dataclass(frozen=True)
class WritePlan:
    actions_json: bytes
    result_receipt_json: bytes
    affected_keys: tuple

    def __post_init__(self):
        actions = owned_json_bytes(self.actions_json)
        receipt = owned_json_bytes(self.result_receipt_json)
        _forbid_receipt_secrets(parse_owned(receipt))
        object.__setattr__(self, "actions_json", actions)
        object.__setattr__(self, "result_receipt_json", receipt)

        def pair(item):
            if type(item) not in (list, tuple) or len(item) != 2:
                _fail()
            return (_text(item[0]), _text(item[1]))

        object.__setattr__(self, "affected_keys", _owned_tuple(self.affected_keys, pair))


@dataclass(frozen=True)
class RecoveryEvidence:
    job_id: str
    attempt_id: str
    job_revision: int
    attempt_revision: int
    epoch: str
    call_id: str | None
    fence: int
    input_digest: str
    stage: str
    candidate_state: str
    code: str | None
    action: str
    snapshot_json: bytes | None = None

    def __post_init__(self):
        _text(self.job_id)
        _text(self.attempt_id)
        _natural_int(self.job_revision)
        _natural_int(self.attempt_revision)
        _text(self.epoch)
        if self.call_id is not None:
            _text(self.call_id)
        _natural_int(self.fence)
        require_hash(self.input_digest)
        _text(self.stage)
        require_member(self.candidate_state, CANDIDATE_STATES)
        if self.code is not None and self.code not in COURSE_ERROR_CODES:
            _fail()
        require_member(self.action, RECOVERY_ACTIONS)
        if self.snapshot_json is not None:
            object.__setattr__(self, "snapshot_json", owned_json_bytes(self.snapshot_json))


@dataclass(frozen=True)
class ArcReceipt:
    status: str
    receipt_id: str | None
    verified_payload_json: bytes | None

    def __post_init__(self):
        # Launch version: disabled/excluded only. Receipt and payload stay null.
        require_member(self.status, SUBMIT_ARC_STATUSES)
        if self.receipt_id is not None or self.verified_payload_json is not None:
            _fail()


class MappingRegistry:
    """mapping_version → immutable transform. Unknown versions stay unregistered."""

    def __init__(self, registered):
        if type(registered) is not dict:
            _fail()
        owned = {}
        for version, transform in registered.items():
            _text(version)
            if not callable(transform) or version in owned:
                _fail()
            owned[version] = transform
        self._registered = MappingProxyType(owned)

    @property
    def versions(self):
        return tuple(self._registered)

    def resolve(self, mapping_version: str):
        _text(mapping_version)
        return self._registered.get(mapping_version)


def definition_identity(bundle: CourseBundle) -> dict:
    """Explicit list/dict projection for typed.digest. Display metadata is omitted."""
    if type(bundle) is not CourseBundle:
        _fail()
    placements = []
    for item in bundle.placements:
        execution = None if item.execution_json is None else parse_owned(item.execution_json)
        placements.append({
            "source_id": item.source_id,
            "position": item.position,
            "kind": item.kind,
            "content_identity": parse_owned(item.content_identity_json),
            "content_version": item.content_version,
            "duration_ms": item.duration_ms,
            "execution_status": item.execution_status,
            "execution": execution,
        })
    return {
        "contract_version": CONTRACT_VERSION,
        "mapping_version": bundle.mapping_version,
        "scope": [
            bundle.scope.learner.provider,
            bundle.scope.learner.tenant_id,
            bundle.scope.learner.learner_id,
            bundle.scope.enrollment_id,
            bundle.scope.course_id,
        ],
        "placements": placements,
    }


def definition_digest(bundle: CourseBundle) -> str:
    identity = definition_identity(bundle)
    if type(identity) is not dict:
        _fail()
    return digest(identity)


def sealed_bundle(bundle: CourseBundle) -> CourseBundle:
    return replace(bundle, definition_hash=definition_digest(bundle))


def scope_identity(scope: CourseScope) -> list:
    if type(scope) is not CourseScope:
        _fail()
    return [
        scope.learner.provider,
        scope.learner.tenant_id,
        scope.learner.learner_id,
        scope.enrollment_id,
        scope.course_id,
    ]


def learner_identity(learner: LearnerContext) -> list:
    if type(learner) is not LearnerContext:
        _fail()
    return [learner.provider, learner.tenant_id, learner.learner_id]


class CourseProvider(Protocol):
    def resolve_learner(self, auth: AuthContext) -> LearnerContext: ...
    def list_assignments(self, learner: LearnerContext) -> tuple[AssignmentBinding, ...]: ...
    def fetch_bundle(self, binding: AssignmentBinding) -> CourseBundle: ...


class BundleValidator(Protocol):
    def validate_bundle(self, bundle: CourseBundle, settings: CourseSettings) -> CourseBundle: ...


class ExecutionMapper(Protocol):
    def map_execution(self, placement: Placement, *, mapping_version: str, mappings: MappingRegistry) -> bytes: ...


class CoursePolicy(Protocol):
    def can_start(self, view: "CourseView", command: StartCommand, role: str) -> None: ...
    def evaluate_content(self, start_evidence_json: bytes, report: ContentReport) -> bytes: ...
    def aggregate_progress(self, bundle: CourseBundle, progress_json: bytes) -> bytes: ...
    def classify_submission(
        self, binding: CourseBinding, verified_result_json: bytes, *, is_dummy: bool, current_epoch: str,
    ) -> bytes: ...


class CourseRepository(Protocol):
    def begin_inventory_for_session(self, auth: AuthContext) -> InventoryTicket | None: ...
    def load_inventory_for_session(self, auth: AuthContext) -> InventoryView: ...
    def load_inventory(self, auth: AuthContext, learner: LearnerContext) -> InventoryView: ...
    def begin_inventory(self, auth: AuthContext, learner: LearnerContext) -> InventoryTicket: ...
    def apply_inventory(
        self, auth: AuthContext, ticket: InventoryTicket,
        result_or_error: tuple[AssignmentBinding, ...] | CourseError,
    ) -> InventoryView: ...
    def ensure_epoch(self, auth: AuthContext, binding: AssignmentBinding) -> GateView: ...
    def begin_refresh(
        self, auth: AuthContext, binding: AssignmentBinding, inventory: InventoryTicket,
    ) -> RefreshTicket: ...
    def apply_refresh(
        self, auth: AuthContext, ticket: RefreshTicket, bundle_or_error: CourseBundle | CourseError,
    ) -> GateView: ...
    def load_view(self, auth: AuthContext, ids: PublicIds) -> CourseView: ...
    def find_created(self, auth: AuthContext, command: StartCommand, *, kind: str) -> StartReceipt | None: ...
    def load_start_view(self, auth: AuthContext, command: StartCommand) -> CourseView: ...
    def start(
        self, auth: AuthContext, command: StartCommand, *, kind: str, view: CourseView,
        template: AttemptTemplate | None,
    ) -> StartReceipt: ...
    def report(
        self, auth: AuthContext, *, course_id: int, enrollment_id: int, placement_id: int, report: ContentReport,
    ) -> StoredProgressReceipt: ...


class CourseService(Protocol):
    def stored_refresh(self, auth: AuthContext) -> RefreshResult: ...
    def list_courses(self, auth: AuthContext, *, page: int, page_size: int) -> bytes: ...
    def get_course(self, auth: AuthContext, *, course_id: int, enrollment_id: int) -> CourseView: ...
    def get_item(
        self, auth: AuthContext, *, course_id: int, enrollment_id: int, placement_id: int,
    ) -> bytes: ...
    def start_content(self, auth: AuthContext, command: StartCommand) -> StartReceipt: ...
    def start_attempt(self, auth: AuthContext, command: StartCommand) -> StartReceipt: ...
    def report_content(
        self, auth: AuthContext, *, course_id: int, enrollment_id: int, placement_id: int, report: ContentReport,
    ) -> StoredProgressReceipt: ...
    def refresh_for_session(self, auth: AuthContext) -> RefreshResult: ...


class CourseCalculationBridge(Protocol):
    def prepare(self, auth: AuthContext, command: StartCommand, view: CourseView) -> AttemptTemplate: ...


class CourseCompletionPlan(Protocol):
    def build(
        self, job: object, attempt: object, user: object, course_rows: object, verified_result: object,
    ) -> WritePlan: ...


class CourseRecovery(Protocol):
    def inspect(self, job_id: str, owner: object, fence: object) -> RecoveryEvidence: ...


class ArcGateway(Protocol):
    def submit(self, intent: object) -> ArcReceipt: ...


PUBLIC_SYMBOLS = (
    "CONTRACT_VERSION", "POLICY_VERSION", "FIXTURE_MAPPING_VERSION", "FIXTURE_CATALOG_VERSION",
    "LearnerContext", "CourseScope", "PublicIds", "AssignmentBinding", "Placement", "CourseBundle",
    "StartCommand", "ContentReport", "CourseBinding", "InventoryTicket", "InventoryView",
    "RefreshTicket", "GateView", "RefreshResult", "CourseView", "StartReceipt",
    "StoredProgressReceipt", "AttemptTemplate", "WritePlan", "RecoveryEvidence", "ArcReceipt",
    "MappingRegistry", "definition_identity", "definition_digest", "sealed_bundle",
    "scope_identity", "learner_identity",
    "CourseProvider", "BundleValidator", "ExecutionMapper", "CoursePolicy", "CourseRepository",
    "CourseService", "CourseCalculationBridge", "CourseCompletionPlan", "CourseRecovery", "ArcGateway",
    "APP_ROUTES", "ITEM_TYPE_WIRE", "validate_execution_definition", "validate_content_identity",
    "validate_availability", "item_type_wire", "owned_json_bytes", "CourseError", "CourseSettings",
)

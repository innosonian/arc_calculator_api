"""Verified course sources. No DB writes, HTTP envelopes, or invented enrollments."""

from dataclasses import replace

from mock_journey.course_contracts import (
    EXECUTION_KEYS, PLACEMENT_KINDS, AssignmentBinding, CourseBundle, LearnerContext,
    MappingRegistry, Placement, item_type_wire, owned_json_bytes, parse_owned, sealed_bundle,
    validate_execution_definition,
)
from mock_journey.course_errors import CourseError
from mock_journey.course_schema import (
    validate_course_metadata, validate_placement_detail, validate_placement_order,
)
from mock_journey.course_settings import CourseSettings
from mock_journey.models import AuthContext
from mock_journey.typed import digest


def _mismatch():
    raise CourseError("UPSTREAM_CONTRACT_MISMATCH")


def _as_error(value):
    if type(value) is CourseError:
        return value
    return CourseError(value)


def planned_error(script, fallback, index):
    """Per-call hook outcome. None means the real provider result, never an empty success."""
    if script is not None:
        if index <= len(script):
            item = script[index - 1]
            if item is None:
                return None
            return _as_error(item)
        return None
    if fallback is None:
        return None
    return _as_error(fallback)


def _bundle_bytes(bundle):
    size = len(bundle.course_json) + len(bundle.source_progress_json)
    for item in bundle.placements:
        size += len(item.content_identity_json) + len(item.detail_json)
        if item.execution_json is not None:
            size += len(item.execution_json)
    return size


def _reject_quiz(detail):
    if type(detail) is not dict:
        _mismatch()
    if detail.get("itemType") == "quiz" or detail.get("contentType") == "quiz":
        _mismatch()
    nested = detail.get("detail")
    if type(nested) is dict and (
        nested.get("itemType") == "quiz" or nested.get("trainingType") == "quiz"
    ):
        _mismatch()


def validate_assignments(bindings, settings: CourseSettings) -> tuple[AssignmentBinding, ...]:
    """Complete assignment set only. A partial page is not converted into success."""
    if type(settings) is not CourseSettings:
        raise TypeError("Invalid course settings.")
    if type(bindings) not in (tuple, list):
        _mismatch()
    owned = []
    seen = []
    for item in bindings:
        if type(item) is not AssignmentBinding:
            _mismatch()
        key = (item.scope.enrollment_id, item.scope.course_id)
        if key in seen:
            _mismatch()
        seen.append(key)
        owned.append(replace(item))
    if len(owned) > settings.max_assignments:
        _mismatch()
    return tuple(owned)


def validate_bundle(bundle: CourseBundle, settings: CourseSettings) -> CourseBundle:
    """Normalize a complete snapshot. Success still returns a new frozen bundle."""
    if type(settings) is not CourseSettings:
        raise TypeError("Invalid course settings.")
    if type(bundle) is not CourseBundle:
        raise CourseError("INVALID_REQUEST")
    placements = bundle.placements
    if not placements or len(placements) > settings.max_course_items:
        _mismatch()
    if _bundle_bytes(bundle) > settings.max_bundle_bytes:
        _mismatch()
    validate_course_metadata(parse_owned(bundle.course_json), bundle.public_ids)
    copied = validate_placements(placements)
    rebuilt = CourseBundle(
        scope=replace(bundle.scope, learner=replace(bundle.scope.learner)),
        public_ids=replace(bundle.public_ids),
        source_revision=bundle.source_revision,
        mapping_version=bundle.mapping_version,
        definition_hash=bundle.definition_hash,
        placements=copied,
        course_json=bundle.course_json,
        source_progress_json=bundle.source_progress_json,
    )
    return sealed_bundle(rebuilt)


def validate_placements(placements):
    """Placement identity is unique; a CourseItem may be referenced repeatedly."""
    source_ids = []
    link_ids = []
    item_definitions = {}
    copied = []
    for item in placements:
        if type(item) is not Placement:
            _mismatch()
        if item.kind not in PLACEMENT_KINDS:
            _mismatch()
        detail = validate_placement_detail(item)
        _reject_quiz(detail)
        wire = item_type_wire(item.kind)
        if detail.get("itemType") != wire["detail_item_type"]:
            _mismatch()
        if "detail" not in detail:
            _mismatch()
        inner = detail["detail"]
        if inner is not None and type(inner) is not dict:
            _mismatch()
        if inner is None and item.execution_status == "ready":
            # detail=null is not a filled-in program; ready would invent a start definition.
            _mismatch()
        if item.source_id in source_ids or item.public_link_id in link_ids:
            _mismatch()
        definition = digest({
            "kind": item.kind,
            "content_identity": parse_owned(item.content_identity_json),
            "content_version": item.content_version,
            "duration_ms": item.duration_ms,
            "execution_status": item.execution_status,
            "execution": None if item.execution_json is None else parse_owned(item.execution_json),
            "detail": {key: value for key, value in detail.items()
                       if key not in {"displayOrder", "courseItemLinkId", "usage"}},
        })
        previous = item_definitions.get(item.public_item_id)
        if previous is not None and previous != definition:
            _mismatch()
        item_definitions[item.public_item_id] = definition
        source_ids.append(item.source_id)
        link_ids.append(item.public_link_id)
        copied.append(replace(item))
    validate_placement_order(copied)
    return tuple(copied)


def map_execution(placement: Placement, *, mapping_version: str, mappings: MappingRegistry) -> bytes:
    """Registered mapping versions only. 5-key catalog rows are not ready execution."""
    if type(placement) is not Placement:
        raise CourseError("INVALID_REQUEST")
    if type(mappings) is not MappingRegistry:
        raise CourseError("INVALID_REQUEST")
    transform = mappings.resolve(mapping_version)
    if transform is None:
        # Unregistered mapping is not approximated from program/course names.
        raise CourseError("EXECUTION_DEFINITION_UNSUPPORTED")
    if placement.kind in {"video", "document"} or placement.execution_status == "absent":
        raise CourseError("EXECUTION_DEFINITION_MISSING")
    if placement.execution_status == "unsupported":
        raise CourseError("EXECUTION_DEFINITION_UNSUPPORTED")
    if placement.execution_status == "contract_pending":
        raise CourseError("CONTRACT_PENDING")
    if placement.execution_status != "ready":
        raise CourseError("EXECUTION_DEFINITION_UNSUPPORTED")
    try:
        body = owned_json_bytes(transform(placement))
        parsed = parse_owned(body)
        if type(parsed) is not dict or tuple(sorted(parsed)) != tuple(sorted(EXECUTION_KEYS)):
            raise CourseError("EXECUTION_DEFINITION_UNSUPPORTED")
        return owned_json_bytes(validate_execution_definition(parsed))
    except CourseError as error:
        if error.code == "INVALID_REQUEST":
            raise CourseError("EXECUTION_DEFINITION_UNSUPPORTED") from None
        raise


class UnavailableCourseProvider:
    """G-ARC stand-in. External calls stay at zero; empty lists and fake IDs are not success."""

    def resolve_learner(self, auth: AuthContext) -> LearnerContext:
        raise CourseError("CONTRACT_PENDING")

    def list_assignments(self, learner: LearnerContext) -> tuple[AssignmentBinding, ...]:
        raise CourseError("CONTRACT_PENDING")

    def fetch_bundle(self, binding: AssignmentBinding) -> CourseBundle:
        raise CourseError("CONTRACT_PENDING")


PROVIDER_SYMBOLS = (
    "UnavailableCourseProvider", "validate_bundle", "validate_assignments", "map_execution",
    "planned_error",
)

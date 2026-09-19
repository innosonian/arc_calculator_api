"""Freeze a verified course placement onto the existing attempt template.

Does not look up the fifteen-slot catalog by course id, clone the calculator,
write storage, or refetch a provider. W5 merges optional course_binding.
"""

from mock_journey.course_contracts import (
    CONDITION_KEYS, EXECUTION_KEYS, POLICY_VERSION, AttemptTemplate, AuthContext,
    CourseBinding, CourseView, StartCommand, definition_digest, parse_owned,
    scope_identity, validate_condition, validate_execution_definition,
)
from mock_journey.course_errors import CourseError
from mock_journey.typed import digest, json_bytes


def _fail(code="INVALID_REQUEST"):
    raise CourseError(code)


def _role(kind, *, last):
    if kind in {"training", "assessment"} and not last:
        return "training"
    if kind == "assessment" and last:
        return "final_assessment"
    _fail("EXECUTION_DEFINITION_UNSUPPORTED")


def _placement(view, command):
    if type(view) is not CourseView or type(command) is not StartCommand:
        _fail()
    if (command.course_id != view.public_ids.course_id
            or command.enrollment_id != view.public_ids.enrollment_id):
        _fail("NOT_FOUND")
    if command.definition_hash != view.bundle.definition_hash:
        _fail("DEFINITION_CHANGED")
    if definition_digest(view.bundle) != view.bundle.definition_hash:
        _fail("DEFINITION_CHANGED")
    for item in view.bundle.placements:
        if item.public_link_id == command.placement_id:
            return item
    _fail("NOT_FOUND")


def _execution(item):
    status = item.execution_status
    if status in ("unsupported", "contract_pending"):
        _fail("EXECUTION_DEFINITION_UNSUPPORTED")
    if status == "absent" or item.execution_json is None:
        _fail("EXECUTION_DEFINITION_MISSING")
    if status != "ready":
        _fail("EXECUTION_DEFINITION_UNSUPPORTED")
    try:
        execution = validate_execution_definition(parse_owned(item.execution_json))
    except CourseError:
        raise CourseError("EXECUTION_DEFINITION_UNSUPPORTED") from None
    validate_condition(execution["condition"])
    return execution


class CourseCalculationBridge:
    """CourseCalculationBridge.prepare: verified view → AttemptTemplate."""

    def prepare(self, auth: AuthContext, command: StartCommand, view: CourseView) -> AttemptTemplate:
        if type(auth) is not AuthContext:
            _fail()
        item = _placement(view, command)
        if view.bundle.scope.learner.principal != auth.principal:
            _fail("NOT_FOUND")
        execution = _execution(item)
        role = _role(item.kind, last=item == view.bundle.placements[-1])
        scope_key = digest(scope_identity(view.bundle.scope))
        if scope_key != view.scope_key:
            _fail("DEFINITION_CHANGED")
        placement_key = digest([scope_key, item.source_id])
        template = {key: execution[key] for key in EXECUTION_KEYS}
        template["condition"] = {key: execution["condition"][key] for key in CONDITION_KEYS}
        template["mapping_version"] = view.bundle.mapping_version
        binding = CourseBinding(
            scope_key, placement_key, role, view.bundle.definition_hash,
            item.content_version, view.gate.epoch, POLICY_VERSION,
        )
        # Resume secrets stay out of this snapshot. W5 adds them on the existing path.
        return AttemptTemplate(json_bytes(template), binding)

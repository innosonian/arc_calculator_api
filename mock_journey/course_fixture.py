"""Explicitly injected synthetic course catalog. No import-time I/O, env, or singleton."""

from dataclasses import replace

from mock_journey.catalog import PROGRAMS, TARGETS
from mock_journey.course_contracts import (
    EXECUTION_KEYS, PLACEMENT_KINDS, AssignmentBinding, CourseBundle, CourseScope,
    LearnerContext, MappingRegistry, Placement, PublicIds, item_type_wire, owned_json_bytes,
    parse_owned, require_public_id, require_source_id, validate_execution_definition,
)
from mock_journey.course_errors import CourseError
from mock_journey.course_provider import planned_error, validate_assignments, validate_bundle, validate_placements
from mock_journey.course_settings import CourseSettings
from mock_journey.models import AuthContext


def _mismatch():
    raise CourseError("UPSTREAM_CONTRACT_MISMATCH")


def _pending():
    raise CourseError("CONTRACT_PENDING")


def _own_document(document):
    if type(document) is not dict:
        _mismatch()
    try:
        return parse_owned(owned_json_bytes(document))
    except CourseError:
        raise CourseError("UPSTREAM_CONTRACT_MISMATCH") from None


def _mapped_public(value):
    # Correspondence table values only. Never int(source_id) or numeric-string coercion.
    try:
        return require_public_id(value)
    except CourseError:
        raise CourseError("UPSTREAM_CONTRACT_MISMATCH") from None


def _mapped_source(value):
    try:
        return require_source_id(value)
    except CourseError:
        raise CourseError("UPSTREAM_CONTRACT_MISMATCH") from None


def _check_optional_public(value, expected):
    if value is not None and (_mapped_public(value) != expected):
        _mismatch()


def _unique_rows(rows, key):
    if type(rows) is not list:
        _mismatch()
    indexed = {}
    for row in rows:
        if type(row) is not dict or key not in row:
            _mismatch()
        source = _mapped_source(row[key])
        if source in indexed:
            _mismatch()
        indexed[source] = row
    return indexed


def fixture_mapping_registry(mapping_document) -> MappingRegistry:
    """Register the fixture mapping version to the exact 7-key rows. No program_id name guessing."""
    owned = _own_document(mapping_document)
    version = owned.get("mapping_version")
    if type(version) is not str or not version:
        _mismatch()
    programs = {row[0] for row in PROGRAMS}
    rows = owned.get("mappings")
    if type(rows) is not dict:
        _mismatch()
    no_execution = owned.get("no_execution")
    if type(no_execution) is not list:
        no_execution = []
    absent = set(no_execution)
    validated_rows = {}
    for source_id, row in rows.items():
        if type(row) is not dict:
            _mismatch()
        program_id = row.get("program_id")
        target = row.get("target")
        execution = row.get("execution")
        if program_id not in programs or target not in TARGETS:
            raise CourseError("EXECUTION_DEFINITION_UNSUPPORTED")
        if program_id in {source_id, row.get("source_placement_id")}:
            _mismatch()
        if type(execution) is not dict or tuple(sorted(execution)) != tuple(sorted(EXECUTION_KEYS)):
            raise CourseError("EXECUTION_DEFINITION_UNSUPPORTED")
        try:
            validated = validate_execution_definition(execution)
        except CourseError:
            raise CourseError("EXECUTION_DEFINITION_UNSUPPORTED") from None
        if target != validated["condition"]["target"]:
            _mismatch()
        validated_rows[source_id] = validated

    def transform(placement):
        source_id = placement.source_id
        if source_id in absent:
            raise CourseError("EXECUTION_DEFINITION_MISSING")
        execution = validated_rows.get(source_id)
        if execution is None:
            raise CourseError("EXECUTION_DEFINITION_UNSUPPORTED")
        return owned_json_bytes(execution)

    return MappingRegistry({version: transform})


def _execution_from_item(item, kind, inner):
    if kind in {"video", "document"}:
        if item.get("execution") is not None:
            _mismatch()
        return None, "absent"
    if inner is None:
        return None, "absent"
    status = item.get("execution_status")
    execution = item.get("execution")
    if status == "unsupported":
        return None, "unsupported"
    if status == "contract_pending":
        return None, "contract_pending"
    if execution is None or status == "absent":
        return None, "absent"
    try:
        if type(execution) is not dict or tuple(sorted(execution)) != tuple(sorted(EXECUTION_KEYS)):
            return None, "unsupported"
        return validate_execution_definition(execution), "ready"
    except CourseError:
        return None, "unsupported"


def _learner_identity(learner):
    return (learner.provider, learner.tenant_id, learner.learner_id)


class FixtureCourseProvider:
    """Synthetic assigned-course source. Callers inject the catalog; this class never reads files."""

    def __init__(
        self, *, document, settings, mapping_document=None, mappings=None,
        list_hook=None, fetch_hook=None, list_error=None, fetch_error=None,
        list_script=None, fetch_script=None,
    ):
        if type(settings) is not CourseSettings:
            raise TypeError("Invalid course settings.")
        owned = _own_document(document)
        self._settings = settings
        self._document = owned
        self._list_hook = list_hook
        self._fetch_hook = fetch_hook
        self._list_error = list_error
        self._fetch_error = fetch_error
        self._list_script = list_script
        self._fetch_script = fetch_script
        self._list_call = 0
        self._fetch_call = 0
        if mappings is not None and type(mappings) is not MappingRegistry:
            raise TypeError("Invalid course settings.")
        if mappings is not None:
            self.mappings = mappings
        elif mapping_document is not None:
            self.mappings = fixture_mapping_registry(mapping_document)
        else:
            self.mappings = None
        self._learners_by_principal, self._learners_by_identity = self._index_learners(owned)
        self._course_row, self._enrollment_map, self._placement_map = self._index_ids(owned)
        self._placement_template = self._parse_placements(owned)
        self._bindings = self._parse_bindings(owned)
        self._binding_by_scope = {
            tuple(self._scope_parts(item.scope)): item for item in self._bindings
        }

    @property
    def list_call_count(self):
        return self._list_call

    @property
    def fetch_call_count(self):
        return self._fetch_call

    @staticmethod
    def _scope_parts(scope):
        learner = scope.learner
        return (learner.provider, learner.tenant_id, learner.learner_id, scope.enrollment_id, scope.course_id)

    def _index_learners(self, document):
        block = document.get("learners")
        if type(block) is not dict:
            _mismatch()
        by_principal = {}
        by_identity = {}
        for row in block.values():
            if type(row) is not dict:
                _mismatch()
            try:
                learner = LearnerContext(
                    row["provider"], row["tenant_id"], row["learner_id"], row["principal"], row["is_dummy"],
                )
            except (CourseError, KeyError, TypeError):
                raise CourseError("UPSTREAM_CONTRACT_MISMATCH") from None
            if learner.principal in by_principal or _learner_identity(learner) in by_identity:
                _mismatch()
            by_principal[learner.principal] = learner
            by_identity[_learner_identity(learner)] = learner
        return by_principal, by_identity

    def _index_ids(self, document):
        table = document.get("id_map")
        if type(table) is not dict:
            _mismatch()
        course = table.get("course")
        if type(course) is not dict or "source_id" not in course or "public_id" not in course:
            _pending()
        _mapped_source(course["source_id"])
        _mapped_public(course["public_id"])
        enrollments = _unique_rows(table.get("enrollments"), "source_id")
        for row in enrollments.values():
            if "public_id" not in row or "progress_id" not in row:
                _pending()
            _mapped_public(row["public_id"])
            _mapped_public(row["progress_id"])
        publics = [row["public_id"] for row in enrollments.values()]
        if len(set(publics)) != len(publics):
            _mismatch()
        placements = _unique_rows(table.get("placements"), "source_id")
        link_ids = []
        for row in placements.values():
            if "public_link_id" not in row or "public_item_id" not in row:
                _pending()
            link_ids.append(_mapped_public(row["public_link_id"]))
            _mapped_public(row["public_item_id"])
        if len(set(link_ids)) != len(link_ids):
            _mismatch()
        return course, enrollments, placements

    def _parse_placements(self, document):
        rows = document.get("placements")
        if type(rows) is not list or not rows:
            _mismatch()
        placements = []
        for item in rows:
            if type(item) is not dict:
                _mismatch()
            kind = item.get("kind")
            if type(kind) is not str or kind not in PLACEMENT_KINDS:
                _mismatch()
            source_id = _mapped_source(item.get("source_id"))
            mapped = self._placement_map.get(source_id)
            if mapped is None:
                _pending()
            public_link_id = _mapped_public(mapped["public_link_id"])
            public_item_id = _mapped_public(mapped["public_item_id"])
            _check_optional_public(item.get("public_link_id"), public_link_id)
            _check_optional_public(item.get("public_item_id"), public_item_id)
            detail = item.get("detail")
            if type(detail) is not dict or "detail" not in detail:
                _mismatch()
            if detail.get("itemType") == "quiz" or kind == "quiz":
                _mismatch()
            wire = item_type_wire(kind)
            if detail.get("itemType") != wire["detail_item_type"]:
                _mismatch()
            inner = detail["detail"]
            execution, status = _execution_from_item(item, kind, inner)
            try:
                placements.append(Placement(
                    source_id=source_id,
                    public_link_id=public_link_id,
                    public_item_id=public_item_id,
                    position=item["position"],
                    kind=kind,
                    content_version=item["content_version"],
                    content_identity_json=item["content_identity"],
                    detail_json=detail,
                    execution_json=execution,
                    execution_status=status,
                    duration_ms=item.get("duration_ms"),
                ))
            except (CourseError, KeyError, TypeError):
                raise CourseError("UPSTREAM_CONTRACT_MISMATCH") from None
        if placements[-1].kind != "assessment":
            _mismatch()
        if len(placements) > self._settings.max_course_items:
            _mismatch()
        source_ids = [item.source_id for item in placements]
        link_ids = [item.public_link_id for item in placements]
        if len(set(source_ids)) != len(source_ids) or len(set(link_ids)) != len(link_ids):
            _mismatch()
        return validate_placements(placements)

    def _parse_bindings(self, document):
        rows = document.get("assignments")
        if type(rows) is not list:
            _mismatch()
        bindings = []
        course_source = _mapped_source(self._course_row["source_id"])
        course_public = _mapped_public(self._course_row["public_id"])
        for row in rows:
            if type(row) is not dict or type(row.get("scope")) is not list or len(row["scope"]) != 5:
                _mismatch()
            provider, tenant_id, learner_id, enroll_source, assigned_course = row["scope"]
            if type(provider) is not str or not provider:
                _mismatch()
            for source_id in (tenant_id, learner_id, enroll_source, assigned_course):
                _mapped_source(source_id)
            learner = self._learners_by_identity.get((provider, tenant_id, learner_id))
            if learner is None:
                _mismatch()
            if assigned_course != course_source:
                _pending()
            enroll_row = self._enrollment_map.get(enroll_source)
            if enroll_row is None:
                _pending()
            public_enroll = _mapped_public(enroll_row["public_id"])
            progress_id = _mapped_public(enroll_row["progress_id"])
            _check_optional_public(row.get("enrollment_public_id"), public_enroll)
            _check_optional_public(row.get("course_public_id"), course_public)
            _check_optional_public(row.get("progress_id"), progress_id)
            try:
                scope = CourseScope(learner, enroll_source, assigned_course)
                public = PublicIds(course_public, public_enroll, progress_id)
            except CourseError:
                raise CourseError("UPSTREAM_CONTRACT_MISMATCH") from None
            bindings.append(AssignmentBinding(scope, public))
        return tuple(bindings)

    def _bundle_for(self, binding):
        assignment = None
        for row in self._document["assignments"]:
            if tuple(row["scope"]) == self._scope_parts(binding.scope):
                assignment = row
                break
        if assignment is None:
            raise CourseError("NOT_FOUND")
        try:
            bundle = CourseBundle(
                scope=binding.scope,
                public_ids=binding.public_ids,
                source_revision=assignment.get("source_revision"),
                mapping_version=self._document["mapping_version"],
                definition_hash="0" * 64,
                placements=self._placement_template,
                course_json=self._document["course_json"],
                source_progress_json=self._document["source_progress_json"],
            )
        except CourseError:
            raise CourseError("UPSTREAM_CONTRACT_MISMATCH") from None
        return validate_bundle(bundle, self._settings)

    def resolve_learner(self, auth: AuthContext) -> LearnerContext:
        if type(auth) is not AuthContext:
            raise CourseError("INVALID_REQUEST")
        learner = self._learners_by_principal.get(auth.principal)
        if learner is None:
            raise CourseError("NOT_FOUND")
        return replace(learner)

    def list_assignments(self, learner: LearnerContext) -> tuple[AssignmentBinding, ...]:
        self._list_call += 1
        if self._list_hook is not None:
            self._list_hook(self._list_call, learner)
        error = planned_error(self._list_script, self._list_error, self._list_call)
        if error is not None:
            raise error
        if type(learner) is not LearnerContext:
            raise CourseError("INVALID_REQUEST")
        stored = self._learners_by_identity.get(_learner_identity(learner))
        if stored is None:
            raise CourseError("NOT_FOUND")
        if stored != learner:
            _mismatch()
        assigned = tuple(item for item in self._bindings if _learner_identity(item.scope.learner) == _learner_identity(learner))
        return validate_assignments(assigned, self._settings)

    def fetch_bundle(self, binding: AssignmentBinding) -> CourseBundle:
        self._fetch_call += 1
        if self._fetch_hook is not None:
            self._fetch_hook(self._fetch_call, binding)
        error = planned_error(self._fetch_script, self._fetch_error, self._fetch_call)
        if error is not None:
            raise error
        if type(binding) is not AssignmentBinding:
            raise CourseError("INVALID_REQUEST")
        key = self._scope_parts(binding.scope)
        matched = self._binding_by_scope.get(key)
        if matched is None:
            raise CourseError("NOT_FOUND")
        if matched.public_ids != binding.public_ids:
            _mismatch()
        return self._bundle_for(matched)


FIXTURE_SYMBOLS = ("FixtureCourseProvider", "fixture_mapping_registry")

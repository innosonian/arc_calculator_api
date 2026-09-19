"""CourseService: provider/repository coordination. No SDK and no inline wire keys."""

from mock_journey.course_contracts import (
    AssignmentBinding, AttemptTemplate, ContentReport, CourseBundle, CourseView, GateView,
    InventoryTicket, InventoryView, LearnerContext, RefreshResult, StartCommand, StartReceipt,
    StoredProgressReceipt, PAGE_DEFAULT, PAGE_SIZE_MAX, learner_identity, require_public_id, scope_identity,
)
from mock_journey.course_errors import CourseError
from mock_journey.course_response import course_list_bytes, item_detail_bytes
from mock_journey.models import AuthContext
from mock_journey.typed import digest


_AUTH_CODES = frozenset({
    "LOGIN_FAILED", "SESSION_REQUIRED", "SESSION_EXPIRED", "SESSION_REVOKED",
})
_CONTENT_KINDS = frozenset({"video", "document"})
_ATTEMPT_KINDS = frozenset({"training", "assessment"})


class CourseService:
    """Matches W0 CourseService Protocol. GET paths never refresh or create enrollments."""

    def __init__(self, provider, repository, *, policy=None, calculation_bridge=None):
        if provider is None or repository is None:
            raise CourseError("TEMPORARILY_UNAVAILABLE")
        self._provider = provider
        self._repository = repository
        self._policy = policy
        self._calculation_bridge = calculation_bridge

    def refresh_for_session(self, auth: AuthContext) -> RefreshResult:
        ticket = self._repository.begin_inventory_for_session(auth)
        try:
            learner = self._provider.resolve_learner(auth)
            if type(learner) is not LearnerContext or learner.principal != auth.principal:
                raise CourseError("UPSTREAM_CONTRACT_MISMATCH")
        except CourseError as error:
            if error.code in _AUTH_CODES or ticket is None:
                raise
            return self._stored_refresh_result(auth, self._repository.apply_inventory(auth, ticket, error))
        except Exception:
            error = CourseError("TEMPORARILY_UNAVAILABLE")
            if ticket is None:
                raise error from None
            return self._stored_refresh_result(auth, self._repository.apply_inventory(auth, ticket, error))
        if ticket is None:
            ticket = self._repository.begin_inventory(auth, learner)
        elif digest(learner_identity(learner)) != ticket.learner_key:
            error = CourseError("UPSTREAM_CONTRACT_MISMATCH")
            return self._stored_refresh_result(auth, self._repository.apply_inventory(auth, ticket, error))
        listed = self._list_assignments(learner)
        inventory = self._repository.apply_inventory(auth, ticket, listed)
        if type(listed) is CourseError or inventory.state != "ready":
            return self._stored_refresh_result(auth, inventory)
        gates = []
        for binding in inventory.assignments:
            gates.append(self._refresh_assignment(auth, binding, ticket))
        return RefreshResult(inventory, tuple(gates))

    def _stored_refresh_result(self, auth, inventory):
        """A stale failure may lose to newer success; return that stored gate state."""
        if inventory.state != "ready":
            return RefreshResult(inventory, ())
        gates = []
        for binding in inventory.assignments:
            try:
                gates.append(self._repository.load_view(auth, binding.public_ids).gate)
            except CourseError as error:
                if error.code in _AUTH_CODES:
                    raise
                gates.append(self._waiting_gate(binding, error))
        return RefreshResult(inventory, tuple(gates))

    def stored_refresh(self, auth: AuthContext) -> RefreshResult:
        """Read the session availability snapshot without starting an ARC refresh."""
        return self._stored_refresh_result(auth, self._repository.load_inventory_for_session(auth))

    def list_courses(self, auth: AuthContext, *, page: int, page_size: int) -> bytes:
        if type(page) is not int or type(page_size) is not int or page < PAGE_DEFAULT:
            raise CourseError("INVALID_REQUEST")
        if page_size < 1 or page_size > PAGE_SIZE_MAX:
            raise CourseError("INVALID_REQUEST")
        _, assignments = self._stored_assignments(auth)
        views = [self._repository.load_view(auth, binding.public_ids) for binding in assignments]
        views.sort(key=lambda view: (view.public_ids.course_id, view.public_ids.enrollment_id))
        start = (page - 1) * page_size
        return course_list_bytes(views[start:start + page_size], count=len(views), page=page, page_size=page_size)

    def get_course(self, auth: AuthContext, *, course_id: int, enrollment_id: int) -> CourseView:
        require_public_id(course_id)
        require_public_id(enrollment_id)
        _, assignments = self._stored_assignments(auth)
        binding = self._assignment(assignments, course_id, enrollment_id)
        view = self._repository.load_view(auth, binding.public_ids)
        if not self._verified_bundle(view):
            raise CourseError("ARC_PROGRESS_UNAVAILABLE")
        return view

    def get_item(
        self, auth: AuthContext, *, course_id: int, enrollment_id: int, placement_id: int,
    ) -> bytes:
        require_public_id(placement_id)
        view = self.get_course(auth, course_id=course_id, enrollment_id=enrollment_id)
        return item_detail_bytes(view, placement_id)

    def start_content(self, auth: AuthContext, command: StartCommand) -> StartReceipt:
        return self._start(auth, command, kind="content")

    def start_attempt(self, auth: AuthContext, command: StartCommand) -> StartReceipt:
        return self._start(auth, command, kind="attempt")

    def report_content(
        self, auth: AuthContext, *, course_id: int, enrollment_id: int, placement_id: int,
        report: ContentReport,
    ) -> StoredProgressReceipt:
        require_public_id(course_id)
        require_public_id(enrollment_id)
        require_public_id(placement_id)
        if type(report) is not ContentReport:
            raise CourseError("INVALID_REQUEST")
        return self._repository.report(
            auth, course_id=course_id, enrollment_id=enrollment_id, placement_id=placement_id, report=report,
        )

    def _start(self, auth, command, *, kind):
        if type(command) is not StartCommand:
            raise CourseError("INVALID_REQUEST")
        existing = self._repository.find_created(auth, command, kind=kind)
        if existing is not None:
            return existing
        view = self._repository.load_start_view(auth, command)
        self._require_new_start_gate(view)
        if command.definition_hash != view.bundle.definition_hash:
            raise CourseError("DEFINITION_CHANGED")
        placement = self._placement(view, command.placement_id)
        self._require_kind(placement.kind, kind)
        self._require_execution(placement, kind)
        role = self._start_role(view, placement)
        if self._policy is not None:
            self._policy.can_start(view, command, role)
        template = None
        if kind == "attempt":
            if self._calculation_bridge is None:
                raise CourseError("TEMPORARILY_UNAVAILABLE")
            template = self._calculation_bridge.prepare(auth, command, view)
            if type(template) is not AttemptTemplate:
                raise CourseError("TEMPORARILY_UNAVAILABLE")
        return self._repository.start(auth, command, kind=kind, view=view, template=template)

    def _stored_assignments(self, auth):
        inventory = self._repository.load_inventory_for_session(auth)
        if type(inventory) is not InventoryView:
            raise CourseError("TEMPORARILY_UNAVAILABLE")
        if inventory.state != "ready":
            raise CourseError("ARC_PROGRESS_UNAVAILABLE")
        return inventory, inventory.assignments

    def _list_assignments(self, learner):
        try:
            assignments = self._provider.list_assignments(learner)
        except CourseError as error:
            if error.code in _AUTH_CODES:
                raise
            return error
        except Exception:
            return CourseError("TEMPORARILY_UNAVAILABLE")
        if type(assignments) not in (tuple, list):
            return CourseError("UPSTREAM_CONTRACT_MISMATCH")
        owned = []
        for item in assignments:
            if type(item) is not AssignmentBinding:
                return CourseError("UPSTREAM_CONTRACT_MISMATCH")
            owned.append(item)
        return tuple(owned)

    def _refresh_assignment(self, auth, binding, ticket: InventoryTicket):
        try:
            self._repository.ensure_epoch(auth, binding)
            refresh_ticket = self._repository.begin_refresh(auth, binding, ticket)
        except CourseError as error:
            if error.code in _AUTH_CODES:
                raise
            return self._waiting_gate(binding, error)
        bundle_or_error = self._fetch_bundle(binding)
        try:
            return self._repository.apply_refresh(auth, refresh_ticket, bundle_or_error)
        except CourseError as error:
            if error.code in _AUTH_CODES:
                raise
            return self._waiting_gate(binding, error)

    def _fetch_bundle(self, binding):
        try:
            bundle = self._provider.fetch_bundle(binding)
        except CourseError as error:
            if error.code in _AUTH_CODES:
                raise
            return error
        except Exception:
            return CourseError("TEMPORARILY_UNAVAILABLE")
        if type(bundle) is not CourseBundle:
            return CourseError("UPSTREAM_CONTRACT_MISMATCH")
        return bundle

    def _waiting_gate(self, binding, error):
        reason = "contract_pending" if error.code == "CONTRACT_PENDING" else "arc_progress_unavailable"
        return GateView(digest(scope_identity(binding.scope)), "unavailable", "waiting", reason, 0, None)

    def _assignment(self, assignments, course_id, enrollment_id):
        for binding in assignments:
            ids = binding.public_ids
            if ids.course_id == course_id and ids.enrollment_id == enrollment_id:
                return binding
        raise CourseError("NOT_FOUND")

    def _verified_bundle(self, view):
        if type(view) is not CourseView:
            return False
        if not view.bundle.placements:
            return False
        if not view.bundle.definition_hash:
            return False
        return True

    def _require_new_start_gate(self, view):
        if type(view) is not CourseView:
            raise CourseError("TEMPORARILY_UNAVAILABLE")
        if view.inventory.state != "ready":
            raise CourseError("ARC_PROGRESS_UNAVAILABLE")
        if view.gate.state == "reconciliation_required":
            raise CourseError("PROGRESS_RECONCILIATION_REQUIRED")
        if view.gate.state != "ready":
            raise CourseError("ARC_PROGRESS_UNAVAILABLE")

    def _placement(self, view, placement_id):
        for item in view.bundle.placements:
            if item.public_link_id == placement_id:
                return item
        raise CourseError("NOT_FOUND")

    def _require_kind(self, kind, start_kind):
        allowed = _CONTENT_KINDS if start_kind == "content" else _ATTEMPT_KINDS
        if kind not in allowed:
            raise CourseError("INVALID_REQUEST")

    def _require_execution(self, placement, start_kind):
        if start_kind != "attempt":
            return
        status = placement.execution_status
        if status == "ready":
            return
        if status == "unsupported":
            raise CourseError("EXECUTION_DEFINITION_UNSUPPORTED")
        if status == "contract_pending":
            raise CourseError("CONTRACT_PENDING")
        raise CourseError("EXECUTION_DEFINITION_MISSING")

    def _start_role(self, view, placement):
        last = view.bundle.placements[-1]
        if placement.kind == "assessment" and placement.public_link_id == last.public_link_id:
            return "final_assessment"
        return "training"

"""VCC HTTP/service unit tests against wire_cases.json. Does not recapture golden."""

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json

from mock_journey.course_contracts import (
    APP_ROUTES, AssignmentBinding, AttemptTemplate, CourseBinding, CourseBundle, CourseScope,
    CourseView, GateView, InventoryTicket, InventoryView, LearnerContext, PAGE_SIZE_MAX,
    POLICY_VERSION, Placement, PublicIds, RefreshTicket, StartCommand, StartReceipt,
    StoredProgressReceipt, parse_owned,
)
from mock_journey.course_errors import CourseError, course_error_table
from mock_journey.course_http import CourseHttp, map_cancel_reason
from mock_journey.course_response import (
    attempt_view_data, content_start_data, progress_receipt_data, submit_arc_data,
)
from mock_journey.course_service import CourseService
from mock_journey.course_settings import fixture_course_settings
from mock_journey.models import AuthContext
from mock_journey.typed import digest, parse_json


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "vcc_contract" / "v1"
WIRE = parse_json((FIXTURES / "wire_cases.json").read_bytes())
BUNDLE_DOC = parse_json((FIXTURES / "course_bundle.json").read_bytes())
CONTRACT_HASH = hashlib.sha256((FIXTURES / "wire_cases.json").read_bytes()).hexdigest()
CLOCK = datetime(2026, 9, 18, tzinfo=timezone.utc)
EXPIRES = datetime(2026, 9, 19, tzinfo=timezone.utc)
SESSION_ID = "60000000-0000-4000-8000-000000000001"
START_ID = "40000000-0000-4000-8000-000000000001"
ATTEMPT_ID = "50000000-0000-4000-8000-000000000001"
LEGACY_ID = "90000000-0000-4000-8000-000000000001"
REQUEST_ID = "61000000-0000-4000-8000-000000000001"
EPOCH = "80000000-0000-4000-8000-000000000001"
TOKEN = "fixture-token"
RESUME = "resume-fixture"
CONDITION = {
    "mode": "training", "target": "adult", "training_type": "compression_only",
    "guideline": "ARC2025", "cpr_cycle_type": "302", "is_2rescuers": False,
}


def load_progress(bundle):
    items = {}
    scope = bundle.scope
    scope_key = digest([scope.learner.provider, scope.learner.tenant_id, scope.learner.learner_id,
                        scope.enrollment_id, scope.course_id])
    for item in bundle.placements:
        items[digest([scope_key, item.source_id])] = {
            "source_id": item.source_id, "public_link_id": item.public_link_id,
            "kind": item.kind, "content_version": item.content_version,
            "completed": False, "passed": None,
        }
    return {"course_status": "NOT_STARTED", "items": items}


def learner_from(doc):
    return LearnerContext(**doc)


def placement_from(doc, **overrides):
    payload = {
        "source_id": doc["source_id"],
        "public_link_id": doc["public_link_id"],
        "public_item_id": doc["public_item_id"],
        "position": doc["position"],
        "kind": doc["kind"],
        "content_version": doc["content_version"],
        "content_identity_json": deepcopy(doc["content_identity"]),
        "detail_json": deepcopy(doc["detail"]),
        "execution_json": deepcopy(doc["execution"]),
        "execution_status": doc["execution_status"],
        "duration_ms": doc["duration_ms"],
    }
    payload.update(overrides)
    return Placement(**payload)


def assignment_of(index):
    doc = BUNDLE_DOC["assignments"][index]
    learner = learner_from(BUNDLE_DOC["learners"]["real"])
    scope = CourseScope(learner, doc["scope"][3], doc["scope"][4])
    public = PublicIds(doc["course_public_id"], doc["enrollment_public_id"], doc["progress_id"])
    return AssignmentBinding(scope, public), doc


def bundle_of(index, *, placements=None):
    binding, doc = assignment_of(index)
    items = placements if placements is not None else [placement_from(item) for item in BUNDLE_DOC["placements"]]
    return CourseBundle(
        scope=binding.scope, public_ids=binding.public_ids, source_revision=doc["source_revision"],
        mapping_version=BUNDLE_DOC["mapping_version"], definition_hash=doc["definition_hash"],
        placements=items, course_json=deepcopy(BUNDLE_DOC["course_json"]),
        source_progress_json=deepcopy(BUNDLE_DOC["source_progress_json"]),
    ), binding, doc


def view_of(index, *, gate_state="ready", gate_reason=None, inventory=None, placements=None):
    bundle, binding, doc = bundle_of(index, placements=placements)
    if inventory is None:
        inventory = InventoryView(BUNDLE_DOC["learner_key"], EPOCH, 1, 1, "ready", None, (binding,))
    gate = GateView(doc["scope_key"], EPOCH, gate_state, gate_reason, 1, doc["definition_hash"])
    return CourseView(doc["scope_key"], bundle.public_ids, bundle, load_progress(bundle), gate, inventory), binding, doc


def command_digest(command):
    return digest({
        "request_id": command.request_id, "enrollment_id": command.enrollment_id,
        "course_id": command.course_id, "placement_id": command.placement_id,
        "definition_hash": command.definition_hash,
    })


class FakeProvider:
    def __init__(self, assignments):
        self.assignments = assignments
        self.list_error = None
        self.fetch_error = None
        self.calls = []
        self.learner = learner_from(BUNDLE_DOC["learners"]["real"])

    def resolve_learner(self, auth):
        self.calls.append("resolve_learner")
        return self.learner

    def list_assignments(self, learner):
        self.calls.append("list_assignments")
        if self.list_error is not None:
            raise CourseError(self.list_error)
        return tuple(self.assignments)

    def fetch_bundle(self, binding):
        self.calls.append("fetch_bundle")
        if self.fetch_error is not None:
            raise CourseError(self.fetch_error)
        for index, item in enumerate(self.assignments):
            if item.public_ids == binding.public_ids:
                bundle, _, _ = bundle_of(index)
                return bundle
        raise CourseError("NOT_FOUND")


class FakePolicy:
    def __init__(self):
        self.errors = {}

    def can_start(self, view, command, role):
        code = self.errors.get(command.placement_id)
        if code:
            raise CourseError(code)


class FakeBridge:
    def prepare(self, auth, command, view):
        placement = next(item for item in view.bundle.placements if item.public_link_id == command.placement_id)
        last = max(view.bundle.placements, key=lambda item: item.position)
        role = "final_assessment" if placement.public_link_id == last.public_link_id else "training"
        return AttemptTemplate({"prepared": True}, CourseBinding(
            view.scope_key, digest([view.scope_key, placement.source_id]), role,
            command.definition_hash, placement.content_version, EPOCH, POLICY_VERSION,
        ))


class FakeRepository:
    def __init__(self, views, inventory):
        self.views = views
        self.inventory = inventory
        self.calls = []
        self.created = {}
        self.reports = {}
        self.verified = True
        self.report_error = None
        self.application = None
        self.pending_evidence = False
        self.bound_session = SESSION_ID
        self.item_error = None

    def begin_inventory(self, auth, learner):
        self.calls.append("begin_inventory")
        return InventoryTicket(self.inventory.learner_key, self.inventory.epoch, self.inventory.generation + 1)

    def begin_inventory_for_session(self, auth):
        return self.begin_inventory(auth, None)

    def load_inventory_for_session(self, auth):
        return self.load_inventory(auth, None)

    def load_inventory(self, auth, learner):
        self.calls.append("load_inventory")
        return self.inventory

    def apply_inventory(self, auth, ticket, result_or_error):
        self.calls.append("apply_inventory")
        if type(result_or_error) is CourseError:
            self.inventory = InventoryView(
                ticket.learner_key, ticket.epoch, ticket.generation, self.inventory.revision + 1,
                "waiting", "arc_progress_unavailable", (),
            )
            return self.inventory
        self.inventory = InventoryView(
            ticket.learner_key, ticket.epoch, ticket.generation, self.inventory.revision + 1,
            "ready", None, tuple(result_or_error),
        )
        return self.inventory

    def ensure_epoch(self, auth, binding):
        self.calls.append("ensure_epoch")
        return self._view(binding.public_ids).gate

    def begin_refresh(self, auth, binding, inventory):
        self.calls.append("begin_refresh")
        view = self._view(binding.public_ids)
        return RefreshTicket(view.scope_key, view.gate.epoch, inventory.generation, 1, view.gate.revision)

    def apply_refresh(self, auth, ticket, bundle_or_error):
        self.calls.append("apply_refresh")
        view = next(item for item in self.views.values() if item.scope_key == ticket.scope_key)
        if type(bundle_or_error) is CourseError:
            reason = "contract_pending" if bundle_or_error.code == "CONTRACT_PENDING" else "arc_progress_unavailable"
            return GateView(ticket.scope_key, ticket.epoch, "waiting", reason, view.gate.revision, view.gate.definition_hash)
        return GateView(ticket.scope_key, ticket.epoch, "ready", None, view.gate.revision + 1, bundle_or_error.definition_hash)

    def load_view(self, auth, ids):
        self.calls.append("load_view")
        if self.item_error:
            raise CourseError(self.item_error)
        if not self.verified:
            raise CourseError("ARC_PROGRESS_UNAVAILABLE")
        return self._view(ids)

    def find_created(self, auth, command, *, kind):
        self.calls.append("find_created")
        key = (auth.session_id, command.request_id, kind)
        if key not in self.created:
            return None
        stored_digest, receipt = self.created[key]
        if stored_digest != command_digest(command):
            raise CourseError("IDEMPOTENCY_CONFLICT")
        return StartReceipt(
            False, receipt.start_id, receipt.attempt_id, receipt.scope_key, receipt.placement_key,
            receipt.definition_hash, receipt.content_version, receipt.bound_session_id, receipt.epoch,
            parse_owned(receipt.response_json),
        )

    def load_start_view(self, auth, command):
        self.calls.append("load_start_view")
        progress_id = 601 if command.enrollment_id == 501 else 602
        return self._view(PublicIds(command.course_id, command.enrollment_id, progress_id))

    def start(self, auth, command, *, kind, view, template):
        self.calls.append("start")
        placement = next(item for item in view.bundle.placements if item.public_link_id == command.placement_id)
        if kind == "content":
            data = content_start_data(
                start_id=START_ID, course_id=command.course_id, enrollment_id=command.enrollment_id,
                course_item_link_id=command.placement_id, content_version=placement.content_version,
                definition_hash=command.definition_hash,
            )
            receipt = StartReceipt(
                True, START_ID, None, view.scope_key, digest([view.scope_key, placement.source_id]),
                command.definition_hash, placement.content_version, auth.session_id, view.gate.epoch, data,
            )
        else:
            execution = parse_owned(placement.execution_json)
            role = "final_assessment" if placement.kind == "assessment" else "training"
            data = attempt_view_data(
                attempt_id=ATTEMPT_ID, state="created", created_at=WIRE["clock"], condition=execution["condition"],
                course_id=command.course_id, enrollment_id=command.enrollment_id,
                course_item_link_id=command.placement_id, definition_hash=command.definition_hash, role=role,
            )
            receipt = StartReceipt(
                True, None, ATTEMPT_ID, view.scope_key, digest([view.scope_key, placement.source_id]),
                command.definition_hash, placement.content_version, auth.session_id, view.gate.epoch, data,
            )
        self.created[(auth.session_id, command.request_id, kind)] = (command_digest(command), receipt)
        return receipt

    def report(self, auth, *, course_id, enrollment_id, placement_id, report):
        self.calls.append("report")
        if auth.session_id != self.bound_session:
            raise CourseError("NOT_FOUND")
        if self.report_error:
            raise CourseError(self.report_error)
        key = (report.start_id, report.report_id)
        current = digest({
            "start_id": report.start_id, "report_id": report.report_id, "event": report.event_type,
            "intervals": [list(item) for item in report.intervals_ms], "version": report.content_version,
            "display": report.display_report_id,
        })
        if key in self.reports:
            stored_digest, stored = self.reports[key]
            if stored_digest != current:
                raise CourseError("IDEMPOTENCY_CONFLICT")
            return stored
        application = self.application or "applied"
        if application == "historical_only":
            completed = passed = status = None
        elif report.event_type == "document_displayed":
            completed, passed, status = False, None, "IN_PROGRESS"
        elif report.event_type == "document_confirmed" and self.pending_evidence:
            completed, passed, status = False, None, "IN_PROGRESS"
            application = "pending_evidence"
        elif report.event_type == "document_confirmed":
            completed, passed, status = True, None, "IN_PROGRESS"
        else:
            completed = report.intervals_ms == ((0, 10000), (10000, 20000))
            passed, status = None, "IN_PROGRESS"
        data = progress_receipt_data(
            start_id=report.start_id, report_id=report.report_id, course_item_link_id=placement_id,
            is_completed=completed, is_passed=passed, course_status=status, application=application,
        )
        stored = StoredProgressReceipt(next(iter(self.views.values())).scope_key, report.start_id, report.report_id, data)
        self.reports[key] = (current, stored)
        return stored

    def _view(self, ids):
        key = (ids.course_id, ids.enrollment_id)
        if key not in self.views:
            raise CourseError("NOT_FOUND")
        return self.views[key]


class World:
    def __init__(self):
        view_501, bind_501, _ = view_of(0)
        view_502, bind_502, _ = view_of(1)
        inventory = InventoryView(
            BUNDLE_DOC["learner_key"], EPOCH, 1, 1, "ready", None, (bind_501, bind_502),
        )
        view_501 = CourseView(
            view_501.scope_key, view_501.public_ids, view_501.bundle, view_501.progress_json, view_501.gate, inventory,
        )
        view_502 = CourseView(
            view_502.scope_key, view_502.public_ids, view_502.bundle, view_502.progress_json, view_502.gate, inventory,
        )
        self.views = {(101, 501): view_501, (101, 502): view_502}
        self.repository = FakeRepository(self.views, inventory)
        self.provider = FakeProvider((bind_501, bind_502))
        self.policy = FakePolicy()
        self.auth = AuthContext(SESSION_ID, "fixture-learner@example.test", 1, int(EXPIRES.timestamp()))
        self.availability = {"state": "ready", "reason": None}
        self.attempts = {
            ATTEMPT_ID: {
                "attempt_id": ATTEMPT_ID, "state": "created", "created_at": WIRE["clock"],
                "condition": CONDITION, "course_id": 101, "enrollment_id": 501,
                "course_item_link_id": 1003, "definition_hash": WIRE["definition_hash_501"],
                "role": "training", "legacy": False,
            },
            LEGACY_ID: {
                "attempt_id": LEGACY_ID, "state": "created", "created_at": WIRE["clock"],
                "condition": CONDITION, "legacy": True,
            },
        }
        self.calculations = {
            ATTEMPT_ID: {
                "attempt_id": ATTEMPT_ID, "state": "queued", "calculation": None,
                "evaluation": None, "progress_application": None,
            }
        }
        self.cancelled = []
        self.executed = []
        service = CourseService(self.provider, self.repository, policy=self.policy, calculation_bridge=FakeBridge())
        self.http = CourseHttp(
            service, fixture_course_settings(), clock=lambda: int(CLOCK.timestamp()), uuid_factory=lambda: REQUEST_ID,
            authenticate=self._authenticate, login=self._login, logout=lambda auth: None,
            session_reader=self._session, issue_resume=lambda auth, ident: RESUME,
            load_attempt=self._load_attempt, reauthorize=self._reauthorize, cancel=self._cancel,
            measurement_submit=self._measure, calculation_result=self._result, chart_link=self._chart,
        )

    def _authenticate(self, token, *, allow_logout_receipt=False):
        if token != TOKEN:
            raise CourseError("SESSION_REQUIRED")
        return self.auth

    def _login(self, login_id, password):
        if login_id != "test@test.com" or password != "2222":
            raise CourseError("LOGIN_FAILED")
        return {
            "auth": self.auth, "session_id": SESSION_ID, "access_token": "session-token",
            "expires_at": int(EXPIRES.timestamp()),
        }

    def _session(self, auth):
        return {
            "session_id": SESSION_ID, "expires_at": EXPIRES.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "learning_availability": self.availability,
        }

    def _load_attempt(self, auth, attempt_id):
        if attempt_id not in self.attempts:
            raise CourseError("NOT_FOUND")
        return self.attempts[attempt_id]

    def _reauthorize(self, auth, attempt_id, credential):
        if attempt_id not in self.attempts:
            raise CourseError("NOT_FOUND")
        return self.attempts[attempt_id]

    def _cancel(self, auth, attempt_id, reason):
        if self.attempts.get(attempt_id, {}).get("state") not in (None, "created"):
            raise CourseError("INVALID_STATE")
        self.cancelled.append(reason)

    def _measure(self, auth, attempt_id, event):
        self.executed.append("measure")
        return self.calculations[attempt_id]

    def _result(self, auth, attempt_id):
        return self.calculations[attempt_id]

    def _chart(self, auth, attempt_id):
        if attempt_id not in self.attempts:
            raise CourseError("NOT_FOUND")
        return {"url": "https://fixture.invalid/chart", "expiresAt": WIRE["clock"]}


def route_path(spec, params=None):
    path = spec.path
    for key, value in (params or {}).items():
        path = path.replace("{" + key + "}", str(value))
    return path


def event(method, path, *, body=None, query=None, query_multi=None, auth=True, headers=None, raw=None):
    payload = {
        "httpMethod": method,
        "path": path,
        "headers": {"Content-Type": "application/json", **(headers or {})},
        "queryStringParameters": query,
        "multiValueQueryStringParameters": query_multi,
        "body": raw if raw is not None else (None if body is None else json.dumps(body, allow_nan=False)),
    }
    if method in ("GET", "DELETE") and body is None and raw is None:
        payload["headers"] = dict(headers or {})
        payload["body"] = None
    if auth:
        payload["headers"]["Authorization"] = f"Bearer {TOKEN}"
    return payload


def decode(response):
    if response["statusCode"] == 204:
        assert response["body"] == ""
        return None
    return json.loads(response["body"])


def assert_placeholder(actual, expected):
    if type(expected) is dict:
        for key, value in expected.items():
            assert key in actual
            assert_placeholder(actual[key], value)
        return
    if type(expected) is list:
        assert len(actual) == len(expected)
        for left, right in zip(actual, expected):
            assert_placeholder(left, right)
        return
    if type(expected) is str and expected.startswith("<") and expected.endswith(">"):
        assert type(actual) is str and actual
        return
    assert actual == expected


def set_gate(world, state, reason=None):
    view = world.views[(101, 501)]
    gate = GateView(view.gate.scope_key, view.gate.epoch, state, reason, view.gate.revision, view.gate.definition_hash)
    world.views[(101, 501)] = CourseView(
        view.scope_key, view.public_ids, view.bundle, view.progress_json, gate, view.inventory,
    )
    world.repository.views = world.views


def prepare_error(world, case_id):
    if case_id == "inventory_not_ready":
        world.repository.inventory = InventoryView(
            BUNDLE_DOC["learner_key"], EPOCH, 1, 1, "waiting", "arc_progress_unavailable", (),
        )
    elif case_id == "never_verified_bundle":
        world.repository.verified = False
    elif case_id == "confirmed_not_assigned":
        world.repository.inventory = InventoryView(BUNDLE_DOC["learner_key"], EPOCH, 1, 1, "ready", None, ())
    elif case_id in ("gate_waiting_new_start", "waiting_new_start"):
        set_gate(world, "waiting", "arc_progress_unavailable")
    elif case_id == "reconciliation":
        set_gate(world, "reconciliation_required", "progress_reconciliation_required")
    elif case_id == "already_completed_training":
        world.policy.errors[1003] = "ITEM_ALREADY_COMPLETED"
    elif case_id == "prerequisites":
        world.policy.errors[1005] = "PREREQUISITES_NOT_COMPLETED"
    elif case_id == "final_active":
        world.policy.errors[1005] = "FINAL_ASSESSMENT_ACTIVE"
    elif case_id == "final_recovery":
        world.policy.errors[1005] = "FINAL_ASSESSMENT_RECOVERY_REQUIRED"
    elif case_id == "policy_pending":
        world.policy.errors[1005] = "COMPLETION_POLICY_PENDING"
    elif case_id == "already_passed":
        world.policy.errors[1005] = "ASSESSMENT_ALREADY_PASSED"
    elif case_id == "execution_missing":
        world.policy.errors[1003] = "EXECUTION_DEFINITION_MISSING"
    elif case_id == "execution_unsupported":
        world.policy.errors[1003] = "EXECUTION_DEFINITION_UNSUPPORTED"
    elif case_id == "content_version_mismatch":
        world.repository.report_error = "CONTENT_VERSION_MISMATCH"
    elif case_id == "capacity":
        world.repository.report_error = "PROGRESS_CAPACITY_EXCEEDED"
    elif case_id == "other_session":
        world.repository.bound_session = "00000000-0000-4000-8000-000000000099"
        world.attempts.pop(ATTEMPT_ID, None)
    elif case_id == "unsupported_kind":
        world.repository.item_error = "UPSTREAM_CONTRACT_MISMATCH"
    elif case_id == "after_accept":
        world.attempts[ATTEMPT_ID]["state"] = "queued"
    elif case_id == "created_or_cancelled":
        world.calculations[ATTEMPT_ID]["state"] = "created"
    elif case_id == "failed":
        world.calculations[ATTEMPT_ID]["state"] = "failed"
    elif case_id == "outcome_unknown":
        world.calculations[ATTEMPT_ID]["state"] = "outcome_unknown"
    elif case_id == "unauthorized":
        world.attempts.clear()


def error_body_for(route, case):
    body = case.get("request")
    if case["id"] == "get_with_body":
        return {}
    if case["id"] == "client_flags_forbidden":
        return {
            "enrollmentId": 501, "courseItemLinkId": 1001, "startId": START_ID,
            "reportId": "30000000-0000-4000-8000-000000000001", "contentVersion": "video-v1",
            "event": {"type": "video_segments", "intervalsMs": [[0, 10000], [10000, 20000]]},
            "isCompleted": True,
        }
    if case["id"] == "empty_intervals":
        return {
            "enrollmentId": 501, "courseItemLinkId": 1001, "startId": START_ID,
            "reportId": "30000000-0000-4000-8000-000000000001", "contentVersion": "video-v1",
            "event": {"type": "video_segments", "intervalsMs": []},
        }
    if case["id"] == "other_body_same_id":
        return {
            "clientRequestId": "10000000-0000-4000-8000-000000000001",
            "courseId": 101, "enrollmentId": 501, "courseItemLinkId": 1002,
            "definitionHash": WIRE["definition_hash_501"],
        }
    if case["id"] == "other_digest_same_report":
        return {
            "enrollmentId": 501, "courseItemLinkId": 1001, "startId": START_ID,
            "reportId": "30000000-0000-4000-8000-000000000001", "contentVersion": "video-v1",
            "event": {"type": "video_segments", "intervalsMs": [[0, 10000], [5000, 9000]]},
        }
    return body


class TestContractFixture:
    def test_fixture_version_and_hash_are_v1(self):
        assert WIRE["contract_version"] == "vcc-internal-v1"
        assert CONTRACT_HASH == hashlib.sha256((FIXTURES / "wire_cases.json").read_bytes()).hexdigest()
        assert len(APP_ROUTES) == 16


class TestV05Wire:
    def test_every_route_error_with_expect_matches_golden_envelope(self):
        for route in WIRE["routes"]:
            spec = next(item for item in APP_ROUTES if item.route_id == route["id"])
            for case in route["errors"]:
                if "expect" not in case:
                    continue
                world = World()
                prepare_error(world, case["id"])
                if case["id"] == "other_body_same_id":
                    first = {
                        "clientRequestId": "10000000-0000-4000-8000-000000000001",
                        "courseId": 101, "enrollmentId": 501, "courseItemLinkId": 1001,
                        "definitionHash": WIRE["definition_hash_501"],
                    }
                    world.http.dispatch(event("POST", spec.path, body=first))
                if case["id"] == "other_digest_same_report":
                    seed = {
                        "enrollmentId": 501, "courseItemLinkId": 1001, "startId": START_ID,
                        "reportId": "30000000-0000-4000-8000-000000000001", "contentVersion": "video-v1",
                        "event": {"type": "video_segments", "intervalsMs": [[0, 10000], [10000, 20000]]},
                    }
                    world.http.dispatch(event("PUT", "/api/v2/courses/101/progress/", body=seed))
                method = case.get("method", route["method"])
                path = case.get("path", route_path(spec, {
                    "courseId": 101, "courseItemLinkId": 1001 if "1005" not in case["id"] else 1005,
                    "attemptId": ATTEMPT_ID,
                }))
                if case["id"] in (
                    "prerequisites", "final_active", "final_recovery", "policy_pending", "already_passed",
                ):
                    path = route_path(spec, {"courseId": 101, "courseItemLinkId": 1005, "attemptId": ATTEMPT_ID})
                body = error_body_for(route, case)
                if body is None and spec.method == method:
                    if spec.body_kind == "empty_object":
                        body = {}
                    elif spec.body_kind == "cancel":
                        body = {"reason": "user_cancelled"}
                    elif spec.body_kind == "reauthorize":
                        body = {"resumeCredential": "resume-fixture"}
                    elif spec.body_kind == "content_report":
                        body = {
                            "enrollmentId": 501, "courseItemLinkId": 1001, "startId": START_ID,
                            "reportId": "30000000-0000-4000-8000-000000000001", "contentVersion": "video-v1",
                            "event": {"type": "video_segments", "intervalsMs": [[0, 10000], [10000, 20000]]},
                        }
                    elif spec.body_kind == "start_request":
                        link = 1001
                        if route["id"] == "attempt_create":
                            link = 1005 if case["id"] in (
                                "prerequisites", "final_active", "final_recovery", "policy_pending", "already_passed",
                            ) else 1003
                        body = {
                            "clientRequestId": "10000000-0000-4000-8000-000000000001",
                            "courseId": 101, "enrollmentId": 501, "courseItemLinkId": link,
                            "definitionHash": WIRE["definition_hash_501"],
                        }
                query = case.get("query")
                if query is None and "enrollmentId" in spec.query_allowed and case["id"] not in (
                    "missing_enrollmentId", "unknown_query", "multi_query", "empty_query", "bool_page",
                    "oversize_page_size",
                ):
                    query = {"enrollmentId": "501"}
                raw = None
                headers = None
                if method in ("GET", "DELETE") and body is not None:
                    raw = json.dumps(body)
                    body = None
                    headers = {"Content-Type": "application/json"}
                authed = case.get("auth", route["auth"])
                if case["id"] == "no_auth":
                    authed = False
                response = world.http.dispatch(event(
                    method, path, body=body, query=query, query_multi=case.get("query_multi"),
                    auth=authed, headers=headers, raw=raw,
                ))
                parsed = decode(response)
                expected = case["expect"]
                assert response["statusCode"] == expected["http"], (route["id"], case["id"], parsed)
                assert parsed["success"] is False
                assert parsed["error"] == expected["body"]["error"]
                assert parsed["timestamp"] == WIRE["clock"]
                assert parsed["error"]["details"] is None
                assert response["headers"]["X-Request-Id"] == REQUEST_ID

    def test_unknown_path_and_payload_too_large_match_golden(self):
        world = World()
        missing = world.http.dispatch(event("GET", WIRE["unknown_path"]["path"]))
        assert missing["statusCode"] == 404
        assert decode(missing)["error"]["code"] == "NOT_FOUND"
        huge = "x" * (fixture_course_settings().max_control_body_bytes + 1)
        large = world.http.dispatch(event(
            "POST", "/api/v2/sessions/", raw='{"loginId":"test@test.com","password":"' + huge + '"}',
        ))
        assert large["statusCode"] == 413
        assert decode(large)["error"]["code"] == "PAYLOAD_TOO_LARGE"

    def test_duplicate_keys_nan_and_infinity_are_invalid_request(self):
        world = World()
        duplicate = world.http.dispatch(event(
            "POST", "/api/v2/sessions/", raw='{"loginId":"a","loginId":"b","password":"2222"}',
        ))
        assert decode(duplicate)["error"]["code"] == "INVALID_REQUEST"
        nan = world.http.dispatch(event(
            "POST", "/api/v2/sessions/", raw='{"loginId":"test@test.com","password":NaN}',
        ))
        assert decode(nan)["error"]["code"] == "INVALID_REQUEST"
        inf = world.http.dispatch(event(
            "POST", "/api/v2/sessions/", raw='{"loginId":"test@test.com","password":Infinity}',
        ))
        assert decode(inf)["error"]["code"] == "INVALID_REQUEST"

    def test_success_schema_matches_c3_c4_and_d2_extensions(self):
        world = World()
        listed = decode(world.http.dispatch(event("GET", "/api/v2/courses/progress/")))
        golden = next(case for case in WIRE["routes"] if case["id"] == "course_list")["success"][0]["data"]
        assert_placeholder(listed["data"], golden)
        detail = decode(world.http.dispatch(event(
            "GET", "/api/v2/courses/101/progress/", query={"enrollmentId": "501"},
        )))
        golden_detail = next(case for case in WIRE["routes"] if case["id"] == "course_detail")["success"][0]["data"]
        assert_placeholder(detail["data"], golden_detail)
        item = decode(world.http.dispatch(event(
            "GET", "/api/v2/courses/101/items/1001/", query={"enrollmentId": "501"},
        )))
        golden_item = next(case for case in WIRE["routes"] if case["id"] == "item_detail")["success"][0]["data"]
        assert_placeholder(item["data"], golden_item)

    def test_query_string_enrollment_is_not_json_string_id(self):
        world = World()
        ok = world.http.dispatch(event("GET", "/api/v2/courses/101/progress/", query={"enrollmentId": "501"}))
        assert ok["statusCode"] == 200
        bad = world.http.dispatch(event(
            "POST", "/api/v2/learning-starts/",
            body={
                "clientRequestId": "10000000-0000-4000-8000-000000000001",
                "courseId": "101", "enrollmentId": 501, "courseItemLinkId": 1001,
                "definitionHash": WIRE["definition_hash_501"],
            },
        ))
        assert decode(bad)["error"]["code"] == "INVALID_REQUEST"

    def test_put_on_progress_path_is_content_report_not_unknown(self):
        world = World()
        response = world.http.dispatch(event(
            "PUT", "/api/v2/courses/101/progress/",
            body={
                "enrollmentId": 501, "courseItemLinkId": 1001, "startId": START_ID,
                "reportId": "30000000-0000-4000-8000-000000000001", "contentVersion": "video-v1",
                "event": {"type": "video_segments", "intervalsMs": [[0, 10000], [10000, 20000]]},
            },
        ))
        assert response["statusCode"] == 200
        post = world.http.dispatch(event("POST", "/api/v2/courses/101/progress/", body={}))
        assert post["statusCode"] == 405


class TestV07IdempotentStart:
    def test_content_and_attempt_first_201_then_200_same_receipt(self):
        world = World()
        request = {
            "clientRequestId": "10000000-0000-4000-8000-000000000001",
            "courseId": 101, "enrollmentId": 501, "courseItemLinkId": 1001,
            "definitionHash": WIRE["definition_hash_501"],
        }
        first = world.http.dispatch(event("POST", "/api/v2/learning-starts/", body=request))
        second = world.http.dispatch(event("POST", "/api/v2/learning-starts/", body=request))
        assert first["statusCode"] == 201
        assert second["statusCode"] == 200
        assert decode(first)["data"]["startId"] == decode(second)["data"]["startId"]
        other = dict(request)
        other["courseItemLinkId"] = 1002
        conflict = world.http.dispatch(event("POST", "/api/v2/learning-starts/", body=other))
        assert decode(conflict)["error"]["code"] == "IDEMPOTENCY_CONFLICT"
        attempt_req = dict(request)
        attempt_req["courseItemLinkId"] = 1003
        created = world.http.dispatch(event("POST", "/api/v2/attempts/", body=attempt_req))
        replay = world.http.dispatch(event("POST", "/api/v2/attempts/", body=attempt_req))
        assert created["statusCode"] == 201
        assert replay["statusCode"] == 200
        assert decode(created)["data"]["attemptId"] == decode(replay)["data"]["attemptId"]


class TestV26ResumeAndCancel:
    def test_resume_credential_is_http_only_and_absent_from_receipt(self):
        world = World()
        request = {
            "clientRequestId": "10000000-0000-4000-8000-000000000001",
            "courseId": 101, "enrollmentId": 501, "courseItemLinkId": 1003,
            "definitionHash": WIRE["definition_hash_501"],
        }
        created = world.http.dispatch(event("POST", "/api/v2/attempts/", body=request))
        assert decode(created)["data"]["resumeCredential"] == RESUME
        stored = next(iter(world.repository.created.values()))[1]
        assert "resumeCredential" not in parse_owned(stored.response_json)
        got = decode(world.http.dispatch(event("GET", f"/api/v2/attempts/{ATTEMPT_ID}/")))
        assert "resumeCredential" not in got["data"]
        session = decode(world.http.dispatch(event("GET", "/api/v2/session/")))
        assert "accessToken" not in session["data"]

    def test_cancel_reason_maps_and_rejects_network_failure_alias(self):
        assert map_cancel_reason("user_cancelled") == "user_stopped"
        assert map_cancel_reason("connection_lost") == "manikin_disconnected"
        world = World()
        response = world.http.dispatch(event(
            "POST", f"/api/v2/attempts/{ATTEMPT_ID}/cancel/", body={"reason": "user_cancelled"},
        ))
        assert response["statusCode"] == 204
        assert world.cancelled == ["user_stopped"]
        world = World()
        lost = world.http.dispatch(event(
            "POST", f"/api/v2/attempts/{ATTEMPT_ID}/cancel/", body={"reason": "connection_lost"},
        ))
        assert lost["statusCode"] == 204
        assert world.cancelled == ["manikin_disconnected"]
        rejected = World().http.dispatch(event(
            "POST", f"/api/v2/attempts/{ATTEMPT_ID}/cancel/", body={"reason": "user_stopped"},
        ))
        assert decode(rejected)["error"]["code"] == "INVALID_REQUEST"
        network = World().http.dispatch(event(
            "POST", f"/api/v2/attempts/{ATTEMPT_ID}/cancel/", body={"reason": "api_unreachable"},
        ))
        assert decode(network)["error"]["code"] == "INVALID_REQUEST"


class TestV21CalculationView:
    def test_pending_succeeded_failed_and_unknown_are_not_status_enums(self):
        world = World()
        pending = decode(world.http.dispatch(event("GET", f"/api/v2/attempts/{ATTEMPT_ID}/calculation/")))
        assert pending["data"]["calculationStatus"] == "pending"
        assert pending["data"]["calculation"] is None
        assert pending["data"]["submit_arc"] == submit_arc_data(status="disabled")
        world.calculations[ATTEMPT_ID] = {
            "attempt_id": ATTEMPT_ID, "state": "evaluated",
            "calculation": {"preserved": True},
            "evaluation": {"goal": {"status": "evaluated", "program_completed": False}},
            "progress_application": {"applied": True},
            "submit_arc": submit_arc_data(status="disabled"),
        }
        succeeded = decode(world.http.dispatch(event("GET", f"/api/v2/attempts/{ATTEMPT_ID}/calculation/")))
        assert succeeded["data"]["calculationStatus"] == "succeeded"
        world.calculations[ATTEMPT_ID] = {
            "attempt_id": ATTEMPT_ID, "state": "evaluated",
            "calculation": {"preserved": True},
            "evaluation": {"goal": {"status": "evaluated", "program_completed": True}},
            "progress_application": {"applied": True},
            "submit_arc": submit_arc_data(status="excluded", exclusion_reasons=("dummy",)),
        }
        excluded = decode(world.http.dispatch(event("GET", f"/api/v2/attempts/{ATTEMPT_ID}/calculation/")))
        assert excluded["data"]["submit_arc"]["ok"] is False
        assert excluded["data"]["submit_arc"]["status"] == "excluded"
        world.calculations[ATTEMPT_ID] = {
            "attempt_id": ATTEMPT_ID, "state": "evaluated",
            "calculation": {"preserved": True},
            "evaluation": {"goal": {"status": "pending_policy"}},
            "progress_application": {"applied": True},
            "submit_arc": submit_arc_data(status="disabled"),
        }
        policy = world.http.dispatch(event("GET", f"/api/v2/attempts/{ATTEMPT_ID}/calculation/"))
        assert policy["statusCode"] == 200
        assert decode(policy)["data"]["evaluation"]["goal"]["status"] == "pending_policy"
        world.calculations[ATTEMPT_ID] = {"attempt_id": ATTEMPT_ID, "state": "failed"}
        failed = world.http.dispatch(event("GET", f"/api/v2/attempts/{ATTEMPT_ID}/calculation/"))
        assert failed["statusCode"] == 503
        assert decode(failed)["error"]["code"] == "CALCULATION_FAILED"
        world.calculations[ATTEMPT_ID] = {"attempt_id": ATTEMPT_ID, "state": "outcome_unknown"}
        unknown = world.http.dispatch(event("GET", f"/api/v2/attempts/{ATTEMPT_ID}/calculation/"))
        assert decode(unknown)["error"]["code"] == "CALCULATION_OUTCOME_UNKNOWN"
        before = world.executed.count("measure")
        world.http.dispatch(event("GET", f"/api/v2/attempts/{ATTEMPT_ID}/calculation/"))
        assert world.executed.count("measure") == before

    def test_measurement_post_uses_raw_event_hook(self):
        world = World()
        world.calculations[ATTEMPT_ID]["state"] = "queued"
        response = world.http.dispatch({
            "httpMethod": "POST",
            "path": f"/api/v2/attempts/{ATTEMPT_ID}/calculation/",
            "headers": {"Authorization": f"Bearer {TOKEN}", "Content-Type": "multipart/form-data"},
            "body": "raw-binary",
            "queryStringParameters": None,
        })
        assert response["statusCode"] == 202
        assert world.executed == ["measure"]
        world.calculations[ATTEMPT_ID] = {
            "attempt_id": ATTEMPT_ID, "state": "evaluated",
            "calculation": {"preserved": True},
            "evaluation": {"goal": {"status": "evaluated", "program_completed": False}},
            "progress_application": {"applied": True},
            "submit_arc": submit_arc_data(status="disabled"),
        }
        done = world.http.dispatch({
            "httpMethod": "POST",
            "path": f"/api/v2/attempts/{ATTEMPT_ID}/calculation/",
            "headers": {"Authorization": f"Bearer {TOKEN}"},
            "body": "raw-binary",
        })
        assert done["statusCode"] == 200


class TestV24LegacyAttempt:
    def test_legacy_attempt_nulls_course_fields_and_keeps_condition(self):
        world = World()
        data = decode(world.http.dispatch(event("GET", f"/api/v2/attempts/{LEGACY_ID}/")))["data"]
        golden = next(
            case for case in next(route for route in WIRE["routes"] if route["id"] == "attempt_get")["success"]
            if case["id"] == "legacy_attempt_null_course_fields"
        )["data"]
        assert_placeholder(data, golden)
        new = decode(world.http.dispatch(event("GET", f"/api/v2/attempts/{ATTEMPT_ID}/")))["data"]
        for field in WIRE["legacy_attempt"]["null_fields"]:
            assert new[field] is not None


class TestV16HistoricalReceipt:
    def test_historical_only_nulls_progress_fields(self):
        world = World()
        world.repository.application = "historical_only"
        response = world.http.dispatch(event(
            "PUT", "/api/v2/courses/101/progress/",
            body={
                "enrollmentId": 501, "courseItemLinkId": 1001, "startId": START_ID,
                "reportId": "30000000-0000-4000-8000-000000000001", "contentVersion": "video-v1",
                "event": {"type": "video_segments", "intervalsMs": [[0, 10000], [10000, 20000]]},
            },
        ))
        data = decode(response)["data"]
        assert data["application"] == "historical_only"
        assert data["isCompleted"] is None
        assert data["isPassed"] is None
        assert data["courseStatus"] is None


class TestGetDoesNotRefresh:
    def test_get_does_not_refresh_or_create(self):
        world = World()
        world.http.dispatch(event("GET", "/api/v2/courses/progress/"))
        world.http.dispatch(event("GET", "/api/v2/courses/101/progress/", query={"enrollmentId": "501"}))
        world.http.dispatch(event("GET", "/api/v2/courses/101/items/1001/", query={"enrollmentId": "501"}))
        world.http.dispatch(event("GET", f"/api/v2/attempts/{ATTEMPT_ID}/"))
        world.http.dispatch(event("GET", "/api/v2/session/"))
        assert "begin_inventory" not in world.repository.calls
        assert "list_assignments" not in world.provider.calls
        assert "fetch_bundle" not in world.provider.calls
        assert "start" not in world.repository.calls


class TestRefreshAndLogin:
    def test_login_dummy_waiting_and_real_student_contract_pending(self):
        world = World()
        world.provider.list_error = "ARC_PROGRESS_UNAVAILABLE"
        login = world.http.dispatch(event(
            "POST", "/api/v2/sessions/", body={"loginId": "test@test.com", "password": "2222"},
        ))
        assert login["statusCode"] == 201
        data = decode(login)["data"]
        assert data["tokenType"] == "Bearer"
        assert data["learningAvailability"]["state"] == "waiting"
        pending = World().http.dispatch(event(
            "POST", "/api/v2/sessions/", body={"loginId": "student@example.test", "password": "unused"},
        ))
        assert decode(pending)["error"]["code"] == "CONTRACT_PENDING"
        failed = World().http.dispatch(event(
            "POST", "/api/v2/sessions/", body={"loginId": "test@test.com", "password": "0000"},
        ))
        assert decode(failed)["error"]["code"] == "LOGIN_FAILED"

    def test_refresh_keeps_session_when_waiting_and_empty_assignments_are_ready(self):
        world = World()
        world.provider.assignments = ()
        refreshed = decode(world.http.dispatch(event("POST", "/api/v2/session/refresh/", body={})))
        assert refreshed["data"]["learningAvailability"]["state"] == "ready"
        world = World()
        world.provider.list_error = "ARC_PROGRESS_UNAVAILABLE"
        waiting = decode(world.http.dispatch(event("POST", "/api/v2/session/refresh/", body={})))
        assert waiting["success"] is True
        assert waiting["data"]["learningAvailability"]["reason"] == "arc_progress_unavailable"

    def test_list_failure_is_not_empty_ready_and_fetch_follows_ticket(self):
        world = World()
        world.provider.list_error = "ARC_PROGRESS_UNAVAILABLE"
        result = world.http._service.refresh_for_session(world.auth)
        assert result.inventory.state == "waiting"
        assert result.inventory.assignments == ()
        assert "fetch_bundle" not in world.provider.calls
        world = World()
        ordered = []
        original_begin = world.repository.begin_refresh
        original_fetch = world.provider.fetch_bundle

        def begin_refresh(auth, binding, inventory):
            ordered.append("begin_refresh")
            return original_begin(auth, binding, inventory)

        def fetch_bundle(binding):
            ordered.append("fetch_bundle")
            return original_fetch(binding)

        world.repository.begin_refresh = begin_refresh
        world.provider.fetch_bundle = fetch_bundle
        world.http._service.refresh_for_session(world.auth)
        assert ordered.index("begin_refresh") < ordered.index("fetch_bundle")
        assert ordered.count("begin_refresh") == ordered.count("fetch_bundle")

    def test_waiting_detail_with_stored_bundle_is_200(self):
        world = World()
        set_gate(world, "waiting", "arc_progress_unavailable")
        response = world.http.dispatch(event("GET", "/api/v2/courses/101/progress/", query={"enrollmentId": "501"}))
        assert response["statusCode"] == 200
        assert decode(response)["data"]["learningAvailability"]["state"] == "waiting"


class TestItemAndReportShapes:
    def test_file_training_null_detail_and_report_replay(self):
        world = World()
        document = decode(world.http.dispatch(event(
            "GET", "/api/v2/courses/101/items/1002/", query={"enrollmentId": "501"},
        )))["data"]
        assert document["itemType"] == "content"
        assert document["detail"]["fileName"] == "safety.pdf"
        training = decode(world.http.dispatch(event(
            "GET", "/api/v2/courses/101/items/1003/", query={"enrollmentId": "501"},
        )))["data"]
        assert training["itemType"] == "training"
        assert training["detail"]["trainingType"] == "chest compression only"
        placements = []
        for item in BUNDLE_DOC["placements"]:
            detail = deepcopy(item["detail"])
            if item["public_link_id"] == 1003:
                detail["detail"] = None
            placements.append(placement_from(item, detail_json=detail))
        null_view, _, _ = view_of(0, placements=placements)
        inventory = world.repository.inventory
        world.views[(101, 501)] = CourseView(
            null_view.scope_key, null_view.public_ids, null_view.bundle, null_view.progress_json,
            null_view.gate, inventory,
        )
        world.repository.views = world.views
        nullable = world.http.dispatch(event(
            "GET", "/api/v2/courses/101/items/1003/", query={"enrollmentId": "501"},
        ))
        assert nullable["statusCode"] == 200
        assert decode(nullable)["data"]["detail"] is None
        body = {
            "enrollmentId": 501, "courseItemLinkId": 1001, "startId": START_ID,
            "reportId": "30000000-0000-4000-8000-000000000001", "contentVersion": "video-v1",
            "event": {"type": "video_segments", "intervalsMs": [[0, 10000]]},
        }
        first = decode(world.http.dispatch(event("PUT", "/api/v2/courses/101/progress/", body=body)))["data"]
        conflict_body = dict(body)
        conflict_body["event"] = {"type": "video_segments", "intervalsMs": [[0, 10000], [10000, 20000]]}
        conflict = world.http.dispatch(event("PUT", "/api/v2/courses/101/progress/", body=conflict_body))
        assert decode(conflict)["error"]["code"] == "IDEMPOTENCY_CONFLICT"
        replay = decode(world.http.dispatch(event("PUT", "/api/v2/courses/101/progress/", body=body)))["data"]
        assert replay == first

    def test_page_defaults_and_max(self):
        world = World()
        defaulted = decode(world.http.dispatch(event("GET", "/api/v2/courses/progress/")))
        assert defaulted["data"]["count"] == 2
        assert defaulted["data"]["next"] is None
        paged = decode(world.http.dispatch(event(
            "GET", "/api/v2/courses/progress/", query={"page": "1", "pageSize": "1"},
        )))
        assert len(paged["data"]["results"]) == 1
        assert paged["data"]["next"] == "/api/v2/courses/progress/?page=2&pageSize=1"
        over = world.http.dispatch(event(
            "GET", "/api/v2/courses/progress/", query={"pageSize": str(PAGE_SIZE_MAX + 1)},
        ))
        assert decode(over)["error"]["code"] == "INVALID_REQUEST"


class TestStartOrder:
    def test_find_created_before_gate_and_leaks_are_absent(self):
        world = World()
        request = {
            "clientRequestId": "10000000-0000-4000-8000-000000000001",
            "courseId": 101, "enrollmentId": 501, "courseItemLinkId": 1001,
            "definitionHash": WIRE["definition_hash_501"],
        }
        world.http.dispatch(event("POST", "/api/v2/learning-starts/", body=request))
        set_gate(world, "waiting", "arc_progress_unavailable")
        replay = world.http.dispatch(event("POST", "/api/v2/learning-starts/", body=request))
        assert replay["statusCode"] == 200
        listed = json.dumps(decode(world.http.dispatch(event("GET", "/api/v2/courses/progress/"))))
        for leak in ("scope_key", "learner_key", "src-enroll-501", "src-place-1001", "password"):
            assert leak not in listed


class TestErrorTable:
    def test_fixed_errors_match_course_error(self):
        table = course_error_table()
        for code, spec in WIRE["fixed_errors"].items():
            error = CourseError(code)
            assert error.status == spec["http"]
            assert error.message == spec["message"]
            assert table[code]["status"] == spec["http"]






"""VCC HTTP/service fakes, wire fixtures and event helpers shared by course API tests.

Moved unchanged from tests/test_vcc_api.py so other test modules no longer
import a test module; test_vcc_api re-exports the same objects.
"""

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json

from mock_journey.course_contracts import (
    AssignmentBinding, AttemptTemplate, CourseBinding, CourseBundle, CourseScope, CourseView, GateView,
    InventoryTicket, InventoryView, POLICY_VERSION, PublicIds, RefreshTicket,
    StartReceipt, StoredProgressReceipt, parse_owned,
)
from mock_journey.course_errors import CourseError
from mock_journey.course_http import CourseHttp
from mock_journey.course_response import (
    attempt_view_data, content_start_data, progress_receipt_data, utc_timestamp,
)
from mock_journey.course_service import CourseService
from mock_journey.course_settings import fixture_course_settings
from mock_journey.models import AuthContext
from mock_journey.typed import digest, parse_json
from tests.course_hooks_support import attempt_record, calculation_record, chart_link_record, hooks_with, session_record
from tests.vcc_contract_support import learner_from, placement_from  # (E-15: one copy of the fixture builders)
from tests.vcc_support import EPOCH


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
        # session_check reuses the session reader, as the refresh route did before it had its own hook.
        self.http = CourseHttp(
            service, fixture_course_settings(), clock=lambda: int(CLOCK.timestamp()), uuid_factory=lambda: REQUEST_ID,
            hooks=hooks_with(
                authenticate=self._authenticate, login=self._login, logout=lambda auth: None,
                session_reader=self._session, session_check=self._session,
                issue_resume=lambda auth, ident: RESUME,
                load_attempt=self._load_attempt, reauthorize=self._reauthorize, cancel=self._cancel,
                measurement_submit=self._measure, calculation_result=self._result, chart_link=self._chart,
            ),
        )

    def _authenticate(self, token, *, allow_logout_receipt=False):
        if token != TOKEN:
            raise CourseError("SESSION_REQUIRED")
        return self.auth

    def _login(self, login_id, password):
        if login_id != "test@test.com" or password != "2222":
            raise CourseError("LOGIN_FAILED")
        return session_record(SESSION_ID, utc_timestamp(int(EXPIRES.timestamp())), auth=self.auth,
                              access_token="session-token", user_name="Test User")

    def _session(self, auth):
        return session_record(SESSION_ID, EXPIRES.strftime("%Y-%m-%dT%H:%M:%SZ"),
                              learning_availability=self.availability)

    def _load_attempt(self, auth, attempt_id):
        if attempt_id not in self.attempts:
            raise CourseError("NOT_FOUND")
        return attempt_record(**self.attempts[attempt_id])

    def _reauthorize(self, auth, attempt_id, credential):
        if attempt_id not in self.attempts:
            raise CourseError("NOT_FOUND")
        return attempt_record(**self.attempts[attempt_id])

    def _cancel(self, auth, attempt_id, reason):
        if self.attempts.get(attempt_id, {}).get("state") not in (None, "created"):
            raise CourseError("INVALID_STATE")
        self.cancelled.append(reason)

    def _measure(self, auth, attempt_id, event):
        self.executed.append("measure")
        return calculation_record(**self.calculations[attempt_id])

    def _result(self, auth, attempt_id):
        return calculation_record(**self.calculations[attempt_id])

    def _chart(self, auth, attempt_id):
        if attempt_id not in self.attempts:
            raise CourseError("NOT_FOUND")
        return chart_link_record("https://fixture.invalid/chart", WIRE["clock"])


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

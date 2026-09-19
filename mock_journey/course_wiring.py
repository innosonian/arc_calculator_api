"""Explicit course_v2 hooks onto existing auth/state/calculation surfaces.

This module does not discover providers, open sockets, or enable ARC transmit.
"""

from datetime import datetime, timezone
import json
import uuid

from mock_journey.course_calculation import CourseCalculationBridge
from mock_journey.course_contracts import CONDITION_KEYS, EXECUTION_KEYS, AttemptTemplate, RefreshResult
from mock_journey.course_errors import CourseError
from mock_journey.course_http import CourseHttp
from mock_journey.course_policy import CoursePolicy
from mock_journey.course_provider import UnavailableCourseProvider
from mock_journey.course_response import aggregate_availability, submit_arc_data
from mock_journey.course_service import CourseService
from mock_journey.course_settings import CourseSettings
from mock_journey.course_state import DynamoCourseRepository
from mock_journey.course_storage import CourseBlobStore
from mock_journey.course_submission import CourseCompletionPlan, DisabledArcGateway
from mock_journey.errors import JourneyError
from mock_journey.legacy_bridge import MeasurementInputError
from mock_journey.state import DynamoCourseStore
from mock_journey.typed import json_bytes, parse_json


COURSE_MODE = "course_v2"


def _rfc3339(seconds):
    return datetime.fromtimestamp(int(seconds), timezone.utc).isoformat().replace("+00:00", "Z")


def _uuid_text(factory):
    def produce():
        value = factory()
        return str(value)
    return produce


class DummyLearnerBinding:
    """Map Dummy login principal onto an explicit dummy learner. No invented enrollments."""

    def __init__(self, provider, dummy_learner):
        self._provider = provider
        self._dummy = dummy_learner

    def resolve_learner(self, auth):
        if self._dummy is not None and auth.principal == self._dummy.principal:
            from dataclasses import replace
            return replace(self._dummy)
        return self._provider.resolve_learner(auth)

    def list_assignments(self, learner):
        if self._dummy is not None and learner.principal == self._dummy.principal:
            if type(self._provider) is UnavailableCourseProvider:
                return self._provider.list_assignments(learner)
            return ()
        return self._provider.list_assignments(learner)

    def fetch_bundle(self, binding):
        return self._provider.fetch_bundle(binding)

    def __getattr__(self, name):
        return getattr(self._provider, name)


class AuthBoundCalculationBridge:
    """Add resume secrets, definition_json, and program/target without changing the 7-key snapshot."""

    def __init__(self, inner, auth_manager, uuid_factory, mapping_rows):
        self._inner = inner
        self._auth = auth_manager
        self._uuid = uuid_factory
        self._rows = mapping_rows if type(mapping_rows) is dict else {}

    def prepare(self, auth, command, view):
        template = self._inner.prepare(auth, command, view)
        payload = parse_json(template.existing_template_json)
        execution = {key: payload[key] for key in EXECUTION_KEYS}
        execution["condition"] = {key: execution["condition"][key] for key in CONDITION_KEYS}
        placement = None
        for item in view.bundle.placements:
            if item.public_link_id == command.placement_id:
                placement = item
                break
        if placement is None:
            raise CourseError("NOT_FOUND")
        row = self._rows.get(placement.source_id) or {}
        program_id = row.get("program_id")
        target = row.get("target")
        if type(program_id) is not str or type(target) is not str:
            raise CourseError("EXECUTION_DEFINITION_UNSUPPORTED")
        prepared = self._auth.prepare_resume({
            **payload,
            "attempt_id": str(self._uuid()),
            "principal": auth.principal,
            "creator_session_id": auth.session_id,
            "bound_session_id": auth.session_id,
            "program_id": program_id,
            "target": target,
            "profile_name": "tester",
            "definition_json": json_bytes(execution).decode("utf-8"),
            "course_id": command.course_id,
            "enrollment_id": command.enrollment_id,
            "course_item_link_id": command.placement_id,
            "active_counted": False,
        })
        return AttemptTemplate(json_bytes(prepared), template.binding)


class CourseApplication:
    """API role with course_v2 HTTP in front of the existing JourneyService."""

    course_mode = COURSE_MODE

    def __init__(self, journey, course_http, course_service, *, provider, repository, gateway):
        self.journey = journey
        self.course_http = course_http
        self.course_service = course_service
        self.provider = provider
        self.repository = repository
        self.gateway = gateway
        self.auth = journey.auth
        self.state = journey.state
        self.catalog = journey.catalog
        self.calculation = journey.calculation
        self.operations = journey.operations

    def login(self, body):
        return self.journey.login(body)

    def session(self, auth):
        return self.journey.session(auth)

    def require_calculation(self):
        return self.journey.require_calculation()


def _login_hook(journey):
    def login(login_id, password):
        result = journey.login({"login_id": login_id, "password": password})
        auth = journey.auth.authenticate(result["session_token"])
        return {
            "auth": auth,
            "session_id": result["session_id"],
            "access_token": result["session_token"],
            "expires_at": result["expires_at"],
        }
    return login


def _session_reader(journey, provider, repository, service):
    def read(auth):
        journey.session(auth)
        availability = {"state": "waiting", "reason": "arc_progress_unavailable"}
        try:
            availability = aggregate_availability(service.stored_refresh(auth))
        except CourseError as error:
            if error.code in {"LOGIN_FAILED", "SESSION_REQUIRED", "SESSION_EXPIRED", "SESSION_REVOKED"}:
                raise
            if error.code == "CONTRACT_PENDING":
                availability = {"state": "waiting", "reason": "contract_pending"}
        except JourneyError:
            raise
        except Exception:
            availability = {"state": "waiting", "reason": "arc_progress_unavailable"}
        return {
            "session_id": auth.session_id,
            "expires_at": _rfc3339(auth.expires_at),
            "learning_availability": availability,
        }
    return read


def _attempt_record(attempt):
    definition = json.loads(attempt["definition_json"]) if type(attempt.get("definition_json")) is str else {}
    condition = definition.get("condition") if type(definition) is dict else None
    created = attempt.get("created_at")
    if type(created) is int:
        created = _rfc3339(created)
    binding = attempt.get("course_binding")
    legacy = binding is None
    return {
        "attempt_id": attempt["attempt_id"],
        "state": attempt["state"],
        "created_at": created,
        "condition": condition,
        "course_id": None if legacy else attempt.get("course_id"),
        "enrollment_id": None if legacy else attempt.get("enrollment_id"),
        "course_item_link_id": None if legacy else attempt.get("course_item_link_id"),
        "definition_hash": None if legacy else (attempt.get("definition_hash") or (
            binding.get("definition_hash") if type(binding) is dict else None
        )),
        "role": None if legacy else (
            (binding.get("start_role") if type(binding) is dict else None) or attempt.get("role")
        ),
        "legacy": legacy,
        "auth": None,
    }


def _calculation_record(attempt, body):
    if type(body) is bytes:
        body = parse_json(body)
    calculation = body if attempt.get("state") == "evaluated" and type(body) is dict else None
    stored_submit = attempt.get("submit_arc")
    wire_submit = None if stored_submit is None else submit_arc_data(
        status=stored_submit["status"], exclusion_reasons=stored_submit["exclusion_reasons"],
    )
    return {
        "attempt_id": attempt["attempt_id"],
        "state": attempt["state"],
        "error_code": attempt.get("error_code"),
        "calculation": calculation,
        "evaluation": attempt.get("evaluation"),
        "progress_application": attempt.get("progress_application"),
        "submit_arc": wire_submit,
    }


def bind_course_http(journey, course_service, settings, *, clock, uuid_factory, provider, repository):
    calculation = journey.require_calculation()

    def authenticate(token, *, allow_logout_receipt=False):
        return journey.auth.authenticate(token, allow_logout_receipt=allow_logout_receipt)

    def logout(auth):
        journey.state.logout(auth)

    def issue_resume(auth, attempt_id):
        attempt = journey.state.get_attempt(auth, attempt_id)
        return journey.auth.resume_credential(attempt)

    def load_attempt(auth, attempt_id):
        return _attempt_record(journey.state.get_attempt(auth, attempt_id))

    def reauthorize(auth, attempt_id, resume_credential):
        journey.reauthorize(auth, attempt_id, {"resume_credential": resume_credential})
        return _attempt_record(journey.state.get_attempt(auth, attempt_id))

    def cancel(auth, attempt_id, reason):
        journey.cancel(auth, attempt_id, {"reason": reason})

    def calculation_snapshot(auth, attempt_id, status, body):
        # Keep the status from the same read as the body. A later worker commit
        # must not turn a pending control response into a calculation snapshot.
        if status == 202:
            if type(body) is not dict or body.get("state") not in ("queued", "processing"):
                raise CourseError("STORED_INPUT_INVALID")
            return _calculation_record({"attempt_id": attempt_id, "state": body["state"]}, None)
        if status != 200:
            raise CourseError("STORED_INPUT_INVALID")
        attempt = journey.state.get_attempt(auth, attempt_id)
        if attempt.get("state") != "evaluated":
            raise CourseError("STORED_INPUT_INVALID")
        return _calculation_record(attempt, body)

    def measurement_submit(auth, attempt_id, event):
        try:
            status, body = calculation.submit(auth, attempt_id, event)
        except MeasurementInputError:
            raise CourseError("MEASUREMENT_INPUT_INVALID") from None
        return calculation_snapshot(auth, attempt_id, status, body)

    def calculation_result(auth, attempt_id):
        status, body = calculation.result(auth, attempt_id)
        return calculation_snapshot(auth, attempt_id, status, body)

    def chart_link(auth, attempt_id):
        record = calculation.chart_link(auth, attempt_id)
        return {"url": record["chart_dataset_url"], "expiresAt": record["expires_at"]}

    return CourseHttp(
        course_service, settings, clock=clock, uuid_factory=_uuid_text(uuid_factory),
        authenticate=authenticate, login=_login_hook(journey), logout=logout,
        session_reader=_session_reader(journey, provider, repository, course_service),
        issue_resume=issue_resume, load_attempt=load_attempt, reauthorize=reauthorize,
        cancel=cancel, measurement_submit=measurement_submit,
        calculation_result=calculation_result, chart_link=chart_link,
    )


def mapping_rows(mapping_document):
    if type(mapping_document) is not dict:
        return {}
    rows = mapping_document.get("mappings")
    return rows if type(rows) is dict else {}


def assemble_course(journey, *, provider, course_settings, clock, uuid_factory,
                    mapping_document=None, dummy_learner=None, blob_store=None):
    if type(course_settings) is not CourseSettings:
        raise ValueError("Invalid explicit journey composition.")
    if provider is None:
        provider = UnavailableCourseProvider()
    if dummy_learner is not None:
        provider = DummyLearnerBinding(provider, dummy_learner)
    store = DynamoCourseStore(journey.state)
    policy = CoursePolicy(course_settings)
    repository = DynamoCourseRepository(
        store, course_settings, policy,
        blob_store if blob_store is not None else CourseBlobStore(journey.calculation.storage),
        clock=clock, uuid_factory=_uuid_text(uuid_factory),
    )
    journey.calculation.jobs.course_blobs = repository.blob_store
    bridge = AuthBoundCalculationBridge(
        CourseCalculationBridge(), journey.auth, uuid_factory, mapping_rows(mapping_document),
    )
    course_service = CourseService(provider, repository, policy=policy, calculation_bridge=bridge)
    http = bind_course_http(
        journey, course_service, course_settings, clock=clock, uuid_factory=uuid_factory,
        provider=provider, repository=repository,
    )
    return CourseApplication(
        journey, http, course_service, provider=provider, repository=repository,
        gateway=DisabledArcGateway(),
    )

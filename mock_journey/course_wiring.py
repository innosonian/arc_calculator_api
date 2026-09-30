"""Explicit course_v2 hooks onto existing auth/state/calculation surfaces.

This module does not discover providers, open sockets, or enable ARC transmit.
"""

import json
import uuid

from mock_journey.auth import DISPLAY_NAME
from mock_journey.course_calculation import CourseCalculationBridge
from mock_journey.course_contracts import (
    CONDITION_KEYS, EXECUTION_KEYS, AttemptRecord, AttemptTemplate, CalculationRecord, ChartLinkRecord,
    CourseHooks, SessionRecord,
)
from mock_journey.course_errors import CourseError
from mock_journey.course_http import CourseHttp, header_representation
from mock_journey.course_mode import COURSE_MODE
from mock_journey.course_policy import CoursePolicy
from mock_journey.course_primitives import rfc3339_seconds
from mock_journey.course_provider import UnavailableCourseProvider
from mock_journey.course_response import availability_or_waiting, submit_arc_data
from mock_journey.course_service import CourseService
from mock_journey.course_settings import CourseSettings
from mock_journey.course_state import DynamoCourseRepository
from mock_journey.course_storage import CourseBlobStore
from mock_journey.course_submission import CourseCompletionPlan, DisabledArcGateway
from mock_journey.legacy_bridge import MeasurementInputError
from mock_journey.state import DynamoCourseStore
from mock_journey.typed import json_bytes, parse_json


_rfc3339 = rfc3339_seconds  # the shared whole-second wire formatter (same output)


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
    """API role: the /api/v2 CourseHttp over the shared auth/state/calculation parts."""

    course_mode = COURSE_MODE

    def __init__(self, journey, course_http, course_service, *, provider, repository, gateway):
        self.course_http = course_http
        self.course_service = course_service
        self.provider = provider
        self.repository = repository
        self.gateway = gateway
        self.auth = journey.auth
        self.state = journey.state
        self.calculation = journey.calculation
        self.operations = journey.operations


def _login_hook(journey):
    def login(login_id, password):
        session, token = journey.login_command(login_id, password)
        auth = journey.auth.authenticate(token)
        return SessionRecord(
            session_id=session["session_id"], expires_at=_rfc3339(session["expires_at"]),
            auth=auth, access_token=token, user_name=DISPLAY_NAME,
        )
    return login


def _session_reader(journey, service):
    def read(auth):
        journey.check_session(auth)
        # The session read never fails on an unexpected availability error (it waits).
        availability = availability_or_waiting(service.stored_refresh, auth, absorb_unexpected=True)
        return SessionRecord(
            session_id=auth.session_id, expires_at=_rfc3339(auth.expires_at),
            learning_availability=availability,
        )
    return read


def _session_check(journey):
    # POST /session/refresh/: the session is checked, nothing stored is read.
    def check(auth):
        journey.check_session(auth)
        return SessionRecord(session_id=auth.session_id, expires_at=_rfc3339(auth.expires_at))
    return check


def _attempt_record(attempt):
    definition = json.loads(attempt["definition_json"]) if type(attempt.get("definition_json")) is str else {}
    condition = definition.get("condition") if type(definition) is dict else None
    created = attempt.get("created_at")
    if type(created) is int:
        created = _rfc3339(created)
    binding = attempt.get("course_binding")
    # legacy (D103): a stored v1 attempt has no course_binding; its course fields are null on the wire.
    if binding is None:
        return AttemptRecord(
            attempt_id=attempt["attempt_id"], state=attempt["state"], created_at=created, condition=condition,
            legacy=True,
        )
    binding_row = binding if type(binding) is dict else {}
    return AttemptRecord(
        attempt_id=attempt["attempt_id"],
        state=attempt["state"],
        created_at=created,
        condition=condition,
        course_id=attempt.get("course_id"),
        enrollment_id=attempt.get("enrollment_id"),
        course_item_link_id=attempt.get("course_item_link_id"),
        definition_hash=attempt.get("definition_hash") or binding_row.get("definition_hash"),
        role=binding_row.get("start_role"),
    )


def _calculation_record(attempt, body):
    if type(body) is bytes:
        body = parse_json(body)
    calculation = body if attempt.get("state") == "evaluated" and type(body) is dict else None
    if calculation is not None:
        # The only submission state on the wire is the top-level submit_arc below
        # (D10). A result stored in an older format may still carry submit_hstm or
        # another submit_* field; it is left out of the response, never rewritten
        # in storage (2026-09-28 user decision, recorded under D103).
        calculation = {key: value for key, value in calculation.items() if not key.startswith("submit_")}
    stored_submit = attempt.get("submit_arc")
    wire_submit = None if stored_submit is None else submit_arc_data(
        status=stored_submit["status"], exclusion_reasons=stored_submit["exclusion_reasons"],
    )
    return CalculationRecord(
        attempt_id=attempt["attempt_id"],
        state=attempt["state"],
        error_code=attempt.get("error_code"),
        calculation=calculation,
        evaluation=attempt.get("evaluation"),
        progress_application=attempt.get("progress_application"),
        submit_arc=wire_submit,
    )


def _adoptable(value):
    # The removed v1 upload adopted only such a value from multiValueHeaders.
    return (type(value) is str and bool(value) and "," not in value
            and "\r" not in value and "\n" not in value)


def measurement_event(event):
    """Resolve the upload Content-Type once, before any attempt is read.

    Same meaning as the removed v1 upload check (D103): a Content-Type that is
    duplicated by letter case, given as more or fewer than one multi-value, or
    different between headers and multiValueHeaders is ambiguous and refused
    (INVALID_REQUEST) before the parser runs. The representation rules are
    course_http.header_representation, the same ones Authorization uses.

    A present Content-Type value must also be one the removed v1 upload
    accepted (a nonempty str without a comma, CR or LF), whether it came in
    headers or in multiValueHeaders; otherwise it is INVALID_REQUEST, as in v1.
    A multi-value-only Content-Type is then copied once into the headers of a
    new event for the parser. The caller's event is never changed.
    """
    if type(event) is not dict:
        raise CourseError("INVALID_REQUEST")
    values, multi_only = header_representation(event, "content-type")
    if not values:
        return event
    if not _adoptable(values[0]):
        raise CourseError("INVALID_REQUEST")
    if not multi_only:
        return event
    headers = event.get("headers")
    headers = {} if headers is None else headers
    return {**event, "headers": {**headers, "Content-Type": values[0]}}


def bind_course_http(journey, course_service, settings, *, clock, uuid_factory):
    calculation = journey.calculation

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
        journey.reauthorize_command(auth, attempt_id, resume_credential)
        return _attempt_record(journey.state.get_attempt(auth, attempt_id))

    def cancel(auth, attempt_id, reason):
        journey.cancel_command(auth, attempt_id, reason)

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
        # After authentication, before the attempt read and the parser.
        event = measurement_event(event)
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
        return ChartLinkRecord(url=record["chart_dataset_url"], expires_at=record["expires_at"])

    hooks = CourseHooks(
        authenticate=authenticate, login=_login_hook(journey), logout=logout,
        session_reader=_session_reader(journey, course_service), session_check=_session_check(journey),
        issue_resume=issue_resume, load_attempt=load_attempt, reauthorize=reauthorize,
        cancel=cancel, measurement_submit=measurement_submit,
        calculation_result=calculation_result, chart_link=chart_link,
    )
    return CourseHttp(course_service, settings, clock=clock, uuid_factory=_uuid_text(uuid_factory), hooks=hooks)


def mapping_rows(mapping_document):
    if type(mapping_document) is not dict:
        return {}
    rows = mapping_document.get("mappings")
    return rows if type(rows) is dict else {}


def assemble_course(journey, *, provider, course_settings, clock, uuid_factory,
                    mapping_document=None, dummy_learner=None, blob_store=None):
    """Course application over an assembled journey.

    ``assembly.build_course_application`` passes the job repository's own
    ``course_blobs`` as ``blob_store``, so both repositories share one store
    object by construction; nothing here reassigns the job repository.
    """
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
    bridge = AuthBoundCalculationBridge(
        CourseCalculationBridge(), journey.auth, uuid_factory, mapping_rows(mapping_document),
    )
    course_service = CourseService(provider, repository, policy=policy, calculation_bridge=bridge)
    http = bind_course_http(journey, course_service, course_settings, clock=clock, uuid_factory=uuid_factory)
    return CourseApplication(
        journey, http, course_service, provider=provider, repository=repository,
        gateway=DisabledArcGateway(),
    )

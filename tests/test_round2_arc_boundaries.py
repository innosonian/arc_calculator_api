"""Round 2 session, assignment and external-contract boundaries."""

from dataclasses import replace

import pytest

from mock_journey.aws_settings import AwsSettings
from mock_journey.course_contracts import StartCommand, sealed_bundle
from mock_journey.course_errors import CourseError
from mock_journey.course_provider import ArcTakeCourseProvider, UnavailableCourseProvider
from mock_journey.course_response import submit_arc_data
from mock_journey.course_service import CourseService
from mock_journey.course_primitives import rfc3339_precise, rfc3339_seconds
from mock_journey.settings import SessionSettings
from mock_journey.typed import parse_json
from services import arc_cre, arc_oidc
from services.arc_contract import ContractPending
from tests.aws_runtime_support import configuration
from tests.journey_support import EventLog, JourneyStore, V2Journey
from tests.vcc_state_support import REQUEST, make_bundle, provision, repository, seed_auth


def test_session_settings_are_applied_at_login_and_read_without_renewal():
    h = V2Journey(JourneyStore.memory())
    settings = SessionSettings(ttl_seconds=90, hard_cap_seconds=60, inactivity_logout_seconds=30)
    h.api_settings = replace(h.api_settings, session=settings)
    h.restart()

    login = h.login()
    expected_expiry = rfc3339_seconds(h.clock() + 60)
    for method, path, body in (("GET", "/api/v2/session/", None), ("POST", "/api/v2/session/refresh/", {})):
        response = h.call(method, path, token=login.token, body=body)
        assert response.status == 200
        assert response.data["expiresAt"] == expected_expiry
        assert response.data["organizations"] == []
        assert response.data["organizationId"] is None
        assert response.data["hardCapSeconds"] == 60
        assert response.data["inactivityLogoutSeconds"] == 30
        h.advance(29)
    assert h.session(login.token)["expiresAt"] == expected_expiry
    h.advance(2)
    assert h.call("GET", "/api/v2/session/", token=login.token).error["code"] == "SESSION_EXPIRED"


def test_aws_api_session_config_is_optional_and_type_checked():
    import json

    raw = configuration("api")
    assert AwsSettings.parse(json.dumps(raw), "api").role_settings.session == SessionSettings()
    raw["session"] = {"ttl_seconds": 10, "hard_cap_seconds": None, "inactivity_logout_seconds": 5}
    assert AwsSettings.parse(json.dumps(raw), "api").role_settings.session == SessionSettings(10, None, 5)
    raw["session"]["ttl_seconds"] = True
    with pytest.raises(ValueError):
        AwsSettings.parse(json.dumps(raw), "api")


def test_oidc_body_stops_before_session_creation_or_token_handling(monkeypatch):
    h = V2Journey(JourneyStore.memory(), events=EventLog())
    monkeypatch.setattr(arc_oidc, "verify", lambda _: pytest.fail("OIDC verification is not configured"))
    body = {"grant": "arc_oidc", "idToken": "fixture-id-marker", "accessToken": "fixture-access-marker"}
    response = h.call("POST", "/api/v2/sessions/", body=body)
    assert (response.status, response.error["code"]) == (503, "CONTRACT_PENDING")
    assert h.store.rows() == []
    for token in (body["idToken"], body["accessToken"]):
        assert token not in str(response.body)
        assert token not in str(h.events.records)
    assert h.call("POST", "/api/v2/sessions/", body={**body, "password": "fixture-extra"}).status == 400


def test_sku_catalog_contains_only_local_round2_targets():
    from mock_journey.dev_course import COURSE_IDS
    from mock_journey.sku_catalog import SKU_CATALOG

    assert set(SKU_CATALOG.values()) == set(COURSE_IDS.values())
    assert len(SKU_CATALOG) == 4


def test_stub_interfaces_report_pending_without_returning_external_success():
    for invoke, error in (
        (lambda: arc_oidc.verify("fixture-id-marker"), ContractPending),
        (lambda: ArcTakeCourseProvider().fetch_assignments("fixture-access-marker"), CourseError),
        (lambda: arc_cre.submit({}), ContractPending),
    ):
        with pytest.raises(error) as raised:
            invoke()
        assert raised.value.code == "CONTRACT_PENDING"
    for status in ("pending", "sent", "acknowledged", "failed"):
        assert submit_arc_data(status=status) == {
            "status": status, "ok": status == "acknowledged", "error": None, "exclusionReasons": [],
        }


def test_assignment_expiry_is_visible_and_blocks_new_starts_but_keeps_replay():
    now = [1_000_000]
    repo, store, _ = repository(clock=now[0])
    repo.clock = lambda: now[0]
    auth = seed_auth(store)
    bundle = make_bundle()
    course = parse_json(bundle.course_json)
    enrollment = course["enrollment_metadata"]["501"]
    enrollment.update(sku="fixture-sku", dueDate=rfc3339_precise(now[0] + 0.5, code="INVALID_REQUEST"),
                      organizationId="fixture-org", organizationName="Fixture organization")
    bundle = sealed_bundle(replace(bundle, course_json=course))
    provision(repo, auth, bundle)
    service = CourseService(UnavailableCourseProvider(), repo, policy=repo.policy)
    command = StartCommand(REQUEST, 501, 101, 1001, bundle.definition_hash)
    receipt = service.start_content(auth, command)
    assert receipt.created
    now[0] += 0.5

    listed = service.list_courses(auth, page=1, page_size=10)
    assert listed["results"][0]["learningAvailability"] == {"state": "expired", "reason": "due_date_passed"}
    assert service.get_course(auth, course_id=101, enrollment_id=501).gate.state == "expired"
    from mock_journey.course_response import course_detail_data
    detail = course_detail_data(repo.load_view(auth, bundle.public_ids))["enrollment"]
    assert {key: detail[key] for key in ("sku", "dueDate", "organizationId", "organizationName")} == {
        key: enrollment[key] for key in ("sku", "dueDate", "organizationId", "organizationName")
    }
    assert service.start_content(auth, command).start_id == receipt.start_id
    with pytest.raises(CourseError) as raised:
        service.start_content(auth, replace(command, request_id="61000000-0000-4000-8000-000000000099"))
    assert raised.value.code == "ASSIGNMENT_EXPIRED"


def test_unselected_non_dummy_session_is_blocked_before_start():
    from mock_journey.storage_keys import session_key

    repo, store, _ = repository()
    auth = seed_auth(store)
    bundle = make_bundle()
    provision(repo, auth, bundle)
    session = store.get_item(session_key(auth.session_id))
    session["organizations"] = [{"id": "org-a", "name": "A"}, {"id": "org-b", "name": "B"}]
    store.seed(session)
    service = CourseService(UnavailableCourseProvider(), repo, policy=repo.policy)
    with pytest.raises(CourseError) as raised:
        service.start_content(auth, StartCommand(REQUEST, 501, 101, 1001, bundle.definition_hash))
    assert raised.value.code == "ORGANIZATION_REQUIRED"


def test_non_dummy_start_refuses_stale_assignment_snapshot_when_bundle_ref_is_missing():
    from mock_journey.storage_keys import course_head_key

    repo, store, _ = repository()
    auth = seed_auth(store)
    bundle = make_bundle()
    view = provision(repo, auth, bundle)
    row = store.get_item(course_head_key(view.scope_key, view.gate.epoch))
    row.pop("bundle_ref")
    store.seed(row)
    command = StartCommand(REQUEST, 501, 101, 1001, bundle.definition_hash)
    with pytest.raises(CourseError) as raised:
        repo.start(auth, command, kind="content", view=view, template=None)
    assert raised.value.code == "ARC_PROGRESS_UNAVAILABLE"

"""Characterization of the /api/v2 course layer before behavior-preserving cleanup.

Expected values are written out independently (literal codes, strings and field
orders), never captured from the current output. D119: a stored-data defect keeps
the 400 or 503 code its path returns today; this file pins those per-path codes so
merging the validation helpers cannot silently move a path between them.
"""

from dataclasses import fields, replace
import json
import math
from types import SimpleNamespace

import pytest

from mock_journey import course_contracts, course_errors, course_http, course_response, course_schema
from mock_journey.course_contracts import (
    APP_ROUTES, ATTEMPT_STATES, CALCULATION_STATUSES, ENROLLMENT_FIELDS, ENROLLMENT_NULLABLE,
    FILE_DETAIL_FIELDS, RECEIPT_FORBIDDEN_KEYS, START_ROLES, SUBMIT_ARC_STATUSES, Placement,
    StartReceipt, StoredProgressReceipt, WritePlan,
)
from mock_journey.course_errors import COURSE_ERROR_CODES, CourseError, error_spec
from mock_journey.course_http import CourseHttp
from mock_journey.course_provider import validate_assignments, validate_bundle
from mock_journey.course_response import (
    attempt_view_data, calculation_view_data, chart_link_data, course_detail_data, course_list_data,
    item_detail_data, parse_receipt_data, progress_receipt_data, session_data, utc_timestamp,
)
from mock_journey.course_settings import CourseSettings, fixture_course_settings
from mock_journey.errors import JourneyError
from mock_journey.models import AuthContext
from mock_journey.typed import parse_json
from tests.course_hooks_support import attempt_record, hooks_with, session_record
from tests.vcc_api_support import view_of
from tests.vcc_support import event


SESSION_ID = "73000000-0000-4000-8000-000000000001"
ATTEMPT_ID = "74000000-0000-4000-8000-000000000001"
START_ID = "75000000-0000-4000-8000-000000000001"
REPORT_ID = "76000000-0000-4000-8000-000000000001"
HTTP_ID = "71000000-0000-4000-8000-000000000001"
HASH = "a" * 64
TOKEN = "characterization-token"
AUTH = AuthContext(SESSION_ID, "dummy-tester", 1, 1_800_086_400)
CONDITION = {
    "mode": "training", "target": "adult", "training_type": "cpr", "guideline": "ARC2025",
    "cpr_cycle_type": "30:2", "is_2rescuers": False,
}
LONE_SURROGATE = "\ud800"


def code_of(call, *args, **kwargs):
    with pytest.raises(CourseError) as raised:
        call(*args, **kwargs)
    return raised.value.code


def _raise(error):
    raise error


# --- S8a-05 / D119: per-path public codes -------------------------------------


class TestContractHelperCodes:
    """course_contracts helpers always raise 400 INVALID_REQUEST and check UTF-8."""

    @pytest.mark.parametrize("value", ["", None, 1, b"x", LONE_SURROGATE, "a" + LONE_SURROGATE])
    def test_text_rejects_empty_non_str_and_lone_surrogate(self, value):
        assert code_of(course_contracts._text, value) == "INVALID_REQUEST"

    def test_text_returns_the_same_object(self):
        value = "text"
        assert course_contracts._text(value) is value

    @pytest.mark.parametrize("value", [0, 1, None, "true"])
    def test_exact_bool(self, value):
        assert code_of(course_contracts._exact_bool, value) == "INVALID_REQUEST"

    @pytest.mark.parametrize("helper,bad,good", [
        (course_contracts._json_int, [True, 1.0, "1", None], [-5, 0, 7]),
        (course_contracts._natural_int, [True, -1, 1.0, None], [0, 7]),
        (course_contracts._positive_int, [True, 0, -1, 1.0], [1, 7]),
    ])
    def test_integer_helpers(self, helper, bad, good):
        for value in bad:
            assert code_of(helper, value) == "INVALID_REQUEST"
        for value in good:
            assert helper(value) == value

    @pytest.mark.parametrize("call", [
        lambda: course_contracts.require_public_id(0),
        lambda: course_contracts.require_public_id(True),
        lambda: course_contracts.require_public_id(9007199254740992),
        lambda: course_contracts.require_uuid("74000000-0000-4000-8000-00000000000G"),
        lambda: course_contracts.require_uuid(LONE_SURROGATE),
        lambda: course_contracts.require_hash("A" * 64),
        lambda: course_contracts.require_utc("2026-09-28T00:00:00+00:00"),
        lambda: course_contracts.require_member("x", {"y"}),
        lambda: course_contracts.require_member("", {""}),
        lambda: course_contracts.require_source_id(True),
        lambda: course_contracts.require_source_id(""),
        lambda: course_contracts.require_source_id(None),
    ])
    def test_require_helpers_raise_invalid_request(self, call):
        assert code_of(call) == "INVALID_REQUEST"

    def test_require_helpers_accept_exact_values(self):
        assert course_contracts.require_public_id(9007199254740991) == 9007199254740991
        assert course_contracts.require_uuid(ATTEMPT_ID) == ATTEMPT_ID
        assert course_contracts.require_utc("2026-09-28T00:00:00.5Z") == "2026-09-28T00:00:00.5Z"
        assert course_contracts.require_source_id(None, allow_null=True) is None
        assert course_contracts.require_source_id(5) == 5

    def test_placement_with_corrupt_public_link_id_is_400(self):
        view, _, _ = view_of(0)
        placement = view.bundle.placements[0]
        assert code_of(replace, placement, public_link_id=0) == "INVALID_REQUEST"


class TestResponseHelperCodes:
    """course_response keeps 503 UPSTREAM for its own checks, 400 for contract helpers."""

    def test_response_text_is_upstream_and_does_not_check_utf8(self):
        assert code_of(course_response._text, "") == "UPSTREAM_CONTRACT_MISMATCH"
        assert code_of(course_response._text, 1) == "UPSTREAM_CONTRACT_MISMATCH"
        # No UTF-8 check here, unlike course_contracts._text.
        assert course_response._text(LONE_SURROGATE) == LONE_SURROGATE

    def test_response_exact_bool_is_upstream(self):
        assert code_of(course_response._exact_bool, 1) == "UPSTREAM_CONTRACT_MISMATCH"
        assert course_response._exact_bool(False) is False

    def test_session_data_codes(self):
        ready = {"state": "ready", "reason": None}
        good = {"session_id": SESSION_ID, "expires_at": "2027-01-15T08:00:00Z", "learning_availability": ready}
        assert code_of(session_data, **{**good, "expires_at": "2027-01-15 08:00:00"}) == "INVALID_REQUEST"
        assert code_of(session_data, **{**good, "session_id": "not-a-uuid"}) == "INVALID_REQUEST"
        assert code_of(session_data, **good, access_token="", user_name="Test User") == "UPSTREAM_CONTRACT_MISMATCH"
        # D129: a login reply needs a nonempty display name; GET/refresh (no token) never carry one.
        assert code_of(session_data, **good, access_token="tok") == "UPSTREAM_CONTRACT_MISMATCH"
        assert code_of(session_data, **good, access_token="tok", user_name="") == "UPSTREAM_CONTRACT_MISMATCH"
        assert list(session_data(**good)) == ["sessionId", "expiresAt", "learningAvailability"]
        assert list(session_data(**good, user_name="Test User")) == ["sessionId", "expiresAt", "learningAvailability"]
        assert code_of(session_data, **{**good, "learning_availability": {"state": "ready"}}) \
            == "UPSTREAM_CONTRACT_MISMATCH"
        data = session_data(**good, access_token="tok", user_name="Test User")
        assert list(data) == ["sessionId", "expiresAt", "learningAvailability", "accessToken", "tokenType", "userName"]
        assert data["tokenType"] == "Bearer"
        assert data["userName"] == "Test User"
        # accessToken is not an identity leak on the wire; only receipts forbid it.
        assert data["accessToken"] == "tok"

    def test_attempt_view_codes(self):
        base = {"attempt_id": ATTEMPT_ID, "state": "queued", "created_at": "2026-09-28T00:00:00Z",
                "condition": CONDITION, "legacy": True}
        assert list(attempt_view_data(**base)) == [
            "attemptId", "state", "courseId", "enrollmentId", "courseItemLinkId",
            "definitionHash", "createdAt", "role", "condition",
        ]
        assert code_of(attempt_view_data, **{**base, "created_at": 5}) == "INVALID_REQUEST"
        assert code_of(attempt_view_data, **{**base, "state": "done"}) == "INVALID_REQUEST"
        assert code_of(attempt_view_data, **{**base, "condition": {**CONDITION, "mode": ""}}) == "INVALID_REQUEST"
        course = {**base, "legacy": False, "course_id": 101, "enrollment_id": 301, "course_item_link_id": 1003,
                  "definition_hash": HASH, "role": "training"}
        assert attempt_view_data(**course)["role"] == "training"
        assert code_of(attempt_view_data, **{**course, "role": None}) == "UPSTREAM_CONTRACT_MISMATCH"
        assert code_of(attempt_view_data, **{**course, "role": "final"}) == "INVALID_REQUEST"
        assert code_of(attempt_view_data, **{**course, "course_id": 0}) == "INVALID_REQUEST"
        for state in ("created", "queued", "processing", "evaluated", "cancelled", "failed", "outcome_unknown"):
            assert attempt_view_data(**{**base, "state": state})["state"] == state

    def test_calculation_view_codes(self):
        base = {"attempt_id": ATTEMPT_ID, "calculation_status": "succeeded", "calculation": {},
                "evaluation": {}, "progress_application": {}}
        assert code_of(calculation_view_data, **{**base, "calculation": []}) == "STORED_INPUT_INVALID"
        assert code_of(calculation_view_data, **{**base, "submit_arc": []}) == "UPSTREAM_CONTRACT_MISMATCH"
        ok = {"status": "disabled", "ok": True, "error": "arc_contract_pending", "exclusionReasons": []}
        assert code_of(calculation_view_data, **base, submit_arc=ok) == "UPSTREAM_CONTRACT_MISMATCH"
        bad_status = {"status": "accepted", "ok": False, "error": None, "exclusionReasons": []}
        assert code_of(calculation_view_data, **base, submit_arc=bad_status) == "INVALID_REQUEST"
        assert code_of(calculation_view_data, **{**base, "calculation_status": "failed"}) == "INVALID_REQUEST"
        pending = calculation_view_data(attempt_id=ATTEMPT_ID, calculation_status="pending")
        assert pending["submit_arc"] == {
            "status": "disabled", "ok": False, "error": "arc_contract_pending", "exclusionReasons": [],
        }
        leaked = calculation_view_data(**{**base, "calculation": {"nested": [{"accessToken": "x"}]}})
        assert leaked["calculation"] == {"nested": [{"accessToken": "x"}]}
        assert code_of(calculation_view_data, **{**base, "calculation": {"x": [{"principal": 1}]}}) \
            == "UPSTREAM_CONTRACT_MISMATCH"

    def test_progress_receipt_codes(self):
        base = {"start_id": START_ID, "report_id": REPORT_ID, "course_item_link_id": 1001, "is_completed": True,
                "is_passed": None, "course_status": "IN_PROGRESS", "application": "applied"}
        assert code_of(progress_receipt_data, **{**base, "application": "x"}) == "INVALID_REQUEST"
        assert code_of(progress_receipt_data, **{**base, "is_completed": 1}) == "UPSTREAM_CONTRACT_MISMATCH"
        assert code_of(progress_receipt_data, **{**base, "is_passed": 0}) == "UPSTREAM_CONTRACT_MISMATCH"
        assert code_of(progress_receipt_data, **{**base, "course_status": "DONE"}) == "INVALID_REQUEST"
        historical = progress_receipt_data(**{**base, "application": "historical_only", "is_completed": "x"})
        assert historical["isCompleted"] is None and historical["courseStatus"] is None

    def test_chart_link_codes(self):
        assert code_of(chart_link_data, url="", expires_at="2026-09-28T00:00:00Z") == "UPSTREAM_CONTRACT_MISMATCH"
        assert code_of(chart_link_data, url="https://x", expires_at="bad") == "INVALID_REQUEST"
        assert code_of(chart_link_data, url=None, expires_at="2026-09-28T00:00:00Z") == "UPSTREAM_CONTRACT_MISMATCH"
        assert chart_link_data(url=None, expires_at=None) == {"url": None, "expiresAt": None}
        # The wire chart link carries the signed URL; only receipts forbid its key names.
        assert chart_link_data(url=LONE_SURROGATE, expires_at="2026-09-28T00:00:00Z")["url"] == LONE_SURROGATE

    def test_item_detail_title_empty_is_upstream_but_schema_accepts_it(self):
        view, _, _ = view_of(0)
        placement = view.bundle.placements[2]
        detail = parse_json(placement.detail_json)
        detail["title"] = ""
        changed = replace(placement, detail_json=detail)
        assert course_schema.validate_placement_detail(changed)["title"] == ""
        placements = list(view.bundle.placements)
        placements[2] = changed
        broken, _, _ = view_of(0, placements=placements)
        assert code_of(item_detail_data, broken, placement.public_link_id) == "UPSTREAM_CONTRACT_MISMATCH"
        assert code_of(course_detail_data, broken) == "UPSTREAM_CONTRACT_MISMATCH"
        assert code_of(item_detail_data, view, 999) == "NOT_FOUND"
        assert code_of(item_detail_data, view, 0) == "INVALID_REQUEST"


class TestSchemaAndProviderCodes:
    def test_schema_public_id_and_logical_id_become_upstream(self):
        good = {"id": 1, "fileName": "f", "order": 1, "url": None, "contentUrl": None}
        assert course_schema.validate_file_detail(dict(good)) == good
        for key, bad in (("id", 0), ("id", True), ("id", 9007199254740992), ("fileName", None),
                         ("order", None), ("url", 1), ("contentUrl", False)):
            assert code_of(course_schema.validate_file_detail, {**good, key: bad}) == "UPSTREAM_CONTRACT_MISMATCH"
        view, _, _ = view_of(0)
        placement = view.bundle.placements[0]
        for bad in ("not-a-uuid", "", "ABCDEF00-0000-4000-8000-000000000001"):
            detail = parse_json(placement.detail_json)
            detail["logicalId"] = bad
            assert code_of(course_schema.validate_placement_detail, replace(placement, detail_json=detail)) \
                == "UPSTREAM_CONTRACT_MISMATCH"

    def test_provider_title_and_settings_codes(self):
        view, _, _ = view_of(0)
        bundle = replace(view.bundle)
        settings = fixture_course_settings()
        placement = bundle.placements[0]
        detail = parse_json(placement.detail_json)
        detail["title"] = ""
        changed = replace(bundle, placements=(replace(placement, detail_json=detail),) + bundle.placements[1:])
        assert code_of(validate_bundle, changed, settings) == "UPSTREAM_CONTRACT_MISMATCH"
        course = parse_json(bundle.course_json)
        course["courseName"] = ""
        assert code_of(validate_bundle, replace(bundle, course_json=course), settings) == "UPSTREAM_CONTRACT_MISMATCH"
        for call in (lambda: validate_bundle(bundle, None), lambda: validate_assignments((), object())):
            with pytest.raises(TypeError) as raised:
                call()
            assert str(raised.value) == "Invalid course settings."
        assert code_of(validate_bundle, object(), settings) == "INVALID_REQUEST"

    def test_course_http_rejects_wrong_settings_type_as_unavailable(self):
        hooks = hooks_with()
        assert code_of(CourseHttp, object(), None, clock=lambda: 0, uuid_factory=lambda: HTTP_ID, hooks=hooks) \
            == "TEMPORARILY_UNAVAILABLE"
        assert code_of(CourseHttp, object(), fixture_course_settings(), clock=None, uuid_factory=lambda: HTTP_ID,
                       hooks=hooks) == "TEMPORARILY_UNAVAILABLE"


# --- S8a-13 / X1-11: RFC3339 formatters ---------------------------------------


class TestRfc3339:
    @pytest.mark.parametrize("value,expected", [
        (0, "1970-01-01T00:00:00Z"),
        (1, "1970-01-01T00:00:01Z"),
        (1.0, "1970-01-01T00:00:01Z"),
        (1.5, "1970-01-01T00:00:01.5Z"),
        (1.25, "1970-01-01T00:00:01.25Z"),
        (1.000001, "1970-01-01T00:00:01.000001Z"),
        (1_800_000_000, "2027-01-15T08:00:00Z"),
        (253402300799, "9999-12-31T23:59:59Z"),
        (-1, "1969-12-31T23:59:59Z"),
    ])
    def test_utc_timestamp_keeps_fraction_without_trailing_zeros(self, value, expected):
        assert utc_timestamp(value) == expected

    @pytest.mark.parametrize("value", [True, False, "1", None, float("nan"), [1]])
    def test_utc_timestamp_rejects_non_numbers_as_unavailable(self, value):
        assert code_of(utc_timestamp, value) == "TEMPORARILY_UNAVAILABLE"

    def test_utc_timestamp_infinity_is_not_a_course_error(self):
        with pytest.raises(OverflowError):
            utc_timestamp(math.inf)

    @pytest.mark.parametrize("value,expected", [
        (0, "1970-01-01T00:00:00Z"), (1.9, "1970-01-01T00:00:01Z"), (True, "1970-01-01T00:00:01Z"),
        (1_800_086_400, "2027-01-16T08:00:00Z"), (253402300799, "9999-12-31T23:59:59Z"),
    ])
    def test_whole_second_copies_truncate(self, value, expected):
        from mock_journey import course_records, course_wiring
        assert course_records._rfc3339(value) == expected
        assert course_wiring._rfc3339(value) == expected

    def test_whole_second_copies_reject_nan_with_value_error(self):
        from mock_journey import course_records, course_wiring
        for formatter in (course_records._rfc3339, course_wiring._rfc3339):
            with pytest.raises(ValueError):
                formatter(float("nan"))


# --- S8a-12 / X1-09: secret and identity key walkers ----------------------------


RECEIPT_KEYS = {
    "resumeCredential", "resume_credential", "accessToken", "access_token", "password",
    "Authorization", "authorization", "cookie", "Cookie", "signedUrl", "signed_url",
    "chartUrl", "chart_url", "chart_dataset_url",
}
IDENTITY_KEYS = {
    "source_id", "sourceId", "SourceId", "scope_key", "scopeKey", "learner_key", "learnerKey",
    "placement_key", "placementKey", "row_key", "rowKey", "principal", "token_hash",
    "password", "upstream", "source_progress", "sourceProgress",
}
RESUME_EXTRA = {"resume_nonce", "resume_digest", "resume_key_version"}


def start_receipt(body):
    return StartReceipt(True, START_ID, None, HASH, HASH, HASH, "v1", SESSION_ID, "e1", body)


class TestSecretWalkers:
    def test_key_sets_are_the_literal_sets(self):
        assert RECEIPT_FORBIDDEN_KEYS == frozenset(RECEIPT_KEYS)
        assert course_response._IDENTITY_LEAKS == frozenset(IDENTITY_KEYS)

    @pytest.mark.parametrize("key", sorted(RECEIPT_KEYS))
    def test_contract_receipts_reject_every_receipt_key_at_any_depth(self, key):
        for body in ({key: 1}, {"a": [{"b": {key: None}}]}, {"a": [[{key: "x"}]]}):
            assert code_of(start_receipt, body) == "INVALID_REQUEST"
            assert code_of(StoredProgressReceipt, HASH, START_ID, REPORT_ID, body) == "INVALID_REQUEST"
            assert code_of(WritePlan, [], body, ()) == "INVALID_REQUEST"

    @pytest.mark.parametrize("key", sorted(RESUME_EXTRA | IDENTITY_KEYS - RECEIPT_KEYS))
    def test_contract_receipts_accept_keys_outside_the_receipt_set(self, key):
        assert parse_json(start_receipt({"a": [{key: 1}]}).response_json) == {"a": [{key: 1}]}
        # Values are never inspected; only dict keys at any depth.
        assert parse_json(start_receipt({"a": [key, {"b": key}]}).response_json) == {"a": [key, {"b": key}]}

    @pytest.mark.parametrize("key", sorted(RECEIPT_KEYS | IDENTITY_KEYS))
    def test_parse_receipt_data_rejects_receipt_and_identity_keys(self, key):
        assert code_of(parse_receipt_data, json.dumps({"x": [{key: 1}]}).encode()) == "UPSTREAM_CONTRACT_MISMATCH"

    @pytest.mark.parametrize("key", sorted(RESUME_EXTRA))
    def test_parse_receipt_data_accepts_resume_extras(self, key):
        assert parse_receipt_data(json.dumps({key: 1}).encode()) == {key: 1}

    @pytest.mark.parametrize("key", sorted(RECEIPT_KEYS - IDENTITY_KEYS))
    def test_wire_ordering_rejects_identity_but_not_receipt_keys(self, key):
        base = {"attempt_id": ATTEMPT_ID, "calculation_status": "succeeded", "evaluation": {},
                "progress_application": {}}
        assert calculation_view_data(**base, calculation={"a": [{key: 1}]})["calculation"] == {"a": [{key: 1}]}

    @pytest.mark.parametrize("key", sorted(RECEIPT_KEYS | RESUME_EXTRA))
    def test_submission_walker_also_rejects_resume_extras(self, key):
        from mock_journey.course_submission import _forbid_secrets
        assert code_of(_forbid_secrets, {"a": [{key: 1}]}) == "INVALID_REQUEST"
        assert _forbid_secrets({"a": [key]}) is None

    def test_submission_walker_key_set_is_the_receipt_keys_plus_the_four_resume_internals(self):
        from mock_journey import course_submission
        assert course_submission._WRITE_FORBIDDEN_KEYS == RECEIPT_FORBIDDEN_KEYS | frozenset({
            "resume_credential", "resume_nonce", "resume_digest", "resume_key_version"})
        assert course_submission._WRITE_FORBIDDEN_KEYS == RECEIPT_KEYS | RESUME_EXTRA


# --- S8a-06 / S8b-06 / X1-13: availability fallback and auth code propagation --


AUTH_CODES = ("LOGIN_FAILED", "SESSION_REQUIRED", "SESSION_EXPIRED", "SESSION_REVOKED")


def http_with(refresh, *, session_reader=None, session_check=None):
    service = SimpleNamespace(refresh_for_session=refresh)
    session = session_record(SESSION_ID, "2027-01-16T08:00:00Z", auth=AUTH, access_token=TOKEN,
                             user_name="Test User")
    reader = session_reader or (lambda auth: session_record(
        SESSION_ID, "2027-01-16T08:00:00Z", learning_availability={"state": "ready", "reason": None},
    ))
    hooks = hooks_with(
        authenticate=lambda token, **k: AUTH if token == TOKEN else _raise(CourseError("SESSION_REQUIRED")),
        login=lambda login_id, password: session,
        session_reader=reader,
        session_check=session_check or (lambda auth: session_record(SESSION_ID, "2027-01-16T08:00:00Z")),
    )
    return CourseHttp(service, fixture_course_settings(), clock=lambda: 1_800_000_000,
                      uuid_factory=lambda: HTTP_ID, hooks=hooks)


def login(http):
    return http.dispatch(event("POST", "/api/v2/sessions/", body={"loginId": "test@test.com", "password": "2222"}))


def refresh(http):
    return http.dispatch(event("POST", "/api/v2/session/refresh/", body={}, token=TOKEN))


def body_of(response):
    return json.loads(response["body"])


class TestAvailabilityFallback:
    @pytest.mark.parametrize("route", [login, refresh])
    @pytest.mark.parametrize("error,reason", [
        (CourseError("CONTRACT_PENDING"), "contract_pending"),
        (CourseError("TEMPORARILY_UNAVAILABLE"), "arc_progress_unavailable"),
        (CourseError("UPSTREAM_CONTRACT_MISMATCH"), "arc_progress_unavailable"),
        (CourseError("NOT_FOUND"), "arc_progress_unavailable"),
    ])
    def test_non_auth_course_errors_wait(self, route, error, reason):
        response = route(http_with(lambda auth: _raise(error)))
        assert response["statusCode"] == (201 if route is login else 200)
        assert body_of(response)["data"]["learningAvailability"] == {"state": "waiting", "reason": reason}

    @pytest.mark.parametrize("route", [login, refresh])
    @pytest.mark.parametrize("code", AUTH_CODES)
    def test_auth_course_errors_propagate(self, route, code):
        response = route(http_with(lambda auth: _raise(CourseError(code))))
        assert response["statusCode"] == error_spec(code)[0]
        assert body_of(response)["error"]["code"] == code

    @pytest.mark.parametrize("route", [login, refresh])
    @pytest.mark.parametrize("error,code,status", [
        (RuntimeError("private"), "TEMPORARILY_UNAVAILABLE", 503),
        (JourneyError("SESSION_EXPIRED"), "SESSION_EXPIRED", 401),
        (JourneyError("STORED_INPUT_INVALID"), "STORED_INPUT_INVALID", 503),
    ])
    def test_non_course_errors_are_not_absorbed_by_http(self, route, error, code, status):
        response = route(http_with(lambda auth: _raise(error)))
        assert (response["statusCode"], body_of(response)["error"]["code"]) == (status, code)

    def test_refresh_checks_the_session_first_and_never_reads_the_stored_snapshot(self):
        calls = []

        def reader(auth):
            calls.append("session_reader")
            return session_record(SESSION_ID, "2027-01-16T08:00:00Z",
                                  learning_availability={"state": "ready", "reason": None})

        def check(auth):
            calls.append("session_check")
            return session_record(SESSION_ID, "2027-01-16T08:00:00Z")

        def refresh_call(auth):
            calls.append("refresh")
            raise CourseError("CONTRACT_PENDING")

        http = http_with(refresh_call, session_reader=reader, session_check=check)
        data = body_of(login(http))["data"]
        assert data["expiresAt"] == "2027-01-16T08:00:00Z"
        assert data["accessToken"] == TOKEN
        assert data["userName"] == "Test User"
        calls.clear()
        data = body_of(refresh(http))["data"]
        assert calls == ["session_check", "refresh"]
        assert data == {"sessionId": SESSION_ID, "expiresAt": "2027-01-16T08:00:00Z",
                        "learningAvailability": {"state": "waiting", "reason": "contract_pending"}}
        calls.clear()
        data = body_of(http.dispatch(event("GET", "/api/v2/session/", token=TOKEN)))["data"]
        assert calls == ["session_reader"]
        assert data["expiresAt"] == "2027-01-16T08:00:00Z"
        assert data["learningAvailability"] == {"state": "ready", "reason": None}

    def test_hooks_are_a_required_course_hooks_value_and_keyword_hooks_are_a_type_error(self):
        # The keyword/dict hook adapter is gone: doubles build CourseHooks (tests/course_hooks_support).
        service = SimpleNamespace(refresh_for_session=lambda auth: _raise(CourseError("CONTRACT_PENDING")))
        common = dict(clock=lambda: 1_800_000_000, uuid_factory=lambda: HTTP_ID)
        with pytest.raises(TypeError):
            CourseHttp(service, fixture_course_settings(), **common,
                       authenticate=lambda token, **k: AUTH, login=lambda login_id, password: None)
        with pytest.raises(TypeError):
            CourseHttp(service, fixture_course_settings(), **common)
        for hooks in (None, {}, {name: (lambda *a, **k: None) for name in course_contracts.HOOK_NAMES},
                      SimpleNamespace(authenticate=lambda token, **k: AUTH)):
            with pytest.raises(CourseError) as raised:
                CourseHttp(service, fixture_course_settings(), **common, hooks=hooks)
            assert raised.value.code == "TEMPORARILY_UNAVAILABLE"
        assert not hasattr(course_http, "legacy_hooks")

    def test_only_the_dummy_login_id_reaches_the_login_hook(self):
        http = http_with(lambda auth: _raise(CourseError("CONTRACT_PENDING")))
        response = http.dispatch(event("POST", "/api/v2/sessions/", body={"loginId": "other@test.com",
                                                                          "password": "2222"}))
        assert body_of(response)["error"]["code"] == "CONTRACT_PENDING"

    def test_session_reader_absorbs_unexpected_but_not_journey_or_auth_errors(self):
        from mock_journey.course_wiring import _session_reader

        journey = SimpleNamespace(check_session=lambda auth: None)

        def read(error):
            service = SimpleNamespace(stored_refresh=lambda auth: _raise(error))
            return _session_reader(journey, service)(AUTH)

        assert read(RuntimeError("x")).learning_availability == {
            "state": "waiting", "reason": "arc_progress_unavailable"}
        assert read(CourseError("CONTRACT_PENDING")).learning_availability == {
            "state": "waiting", "reason": "contract_pending"}
        assert read(CourseError("NOT_FOUND")).learning_availability == {
            "state": "waiting", "reason": "arc_progress_unavailable"}
        for code in AUTH_CODES:
            assert code_of(read, CourseError(code)) == code
        with pytest.raises(JourneyError):
            read(JourneyError("SESSION_EXPIRED"))

    def test_service_waiting_gate_reason(self):
        from mock_journey.course_service import CourseService
        view, binding, _ = view_of(0)
        service = CourseService(object(), object())
        assert service._waiting_gate(binding, CourseError("CONTRACT_PENDING")).reason == "contract_pending"
        for code in ("TEMPORARILY_UNAVAILABLE", "NOT_FOUND", "UPSTREAM_CONTRACT_MISMATCH"):
            gate = service._waiting_gate(binding, CourseError(code))
            assert (gate.state, gate.reason, gate.epoch, gate.revision, gate.definition_hash) == (
                "waiting", "arc_progress_unavailable", "unavailable", 0, None)
            assert gate.scope_key == view.scope_key


# --- S8b-18: reused journey error codes ------------------------------------------


class TestErrorTables:
    def test_reused_codes_are_exactly_the_journey_codes(self):
        literal = frozenset({
            "INVALID_REQUEST", "LOGIN_FAILED", "SESSION_REQUIRED", "SESSION_EXPIRED", "SESSION_REVOKED",
            "NOT_FOUND", "IDEMPOTENCY_CONFLICT", "ATTEMPT_INPUT_CONFLICT", "PROFILE_MISMATCH",
            "INVALID_STATE", "PAYLOAD_TOO_LARGE", "TEMPORARILY_UNAVAILABLE",
            "CALCULATOR_CONTRACT_MISMATCH", "CALCULATION_OUTCOME_UNKNOWN", "STORED_INPUT_INVALID",
            "CALCULATION_FAILED", "MEASUREMENT_INPUT_INVALID", "PROGRAM_ALREADY_COMPLETED",
        })
        from mock_journey import errors
        assert course_errors.REUSED_JOURNEY_CODES == literal
        assert frozenset(errors._ERRORS) == literal
        assert len(COURSE_ERROR_CODES) == 32  # D130 removed ITEM_ALREADY_COMPLETED and ASSESSMENT_ALREADY_PASSED
        assert COURSE_ERROR_CODES == literal | frozenset(course_errors._COURSE_ERRORS)

    def test_unknown_code_is_a_key_error(self):
        with pytest.raises(KeyError):
            CourseError("NOT_A_CODE")
        with pytest.raises(KeyError):
            error_spec("NOT_A_CODE")

    def test_reused_code_keeps_journey_status_and_message(self):
        assert error_spec("SESSION_REVOKED") == (403, "The session has been revoked.")
        assert error_spec("CONTRACT_PENDING") == (503, "The integration contract is not available.")


# --- S8a-08: route rules and query/path numbers ------------------------------------


class TestRouteRules:
    def test_compiled_path_patterns_are_exact(self):
        uuid = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
        digits = r"[1-9][0-9]{0,15}"
        patterns = {spec.route_id: (regex.pattern, tuple(names)) for regex, names, spec in course_http._COMPILED}
        assert patterns["login"] == (r"\A/api/v2/sessions/\Z", ())
        assert patterns["course_detail"] == (
            r"\A/api/v2/courses/(?P<courseId>" + digits + r")/progress/\Z", ("courseId",))
        assert patterns["item_detail"] == (
            r"\A/api/v2/courses/(?P<courseId>" + digits + r")/items/(?P<courseItemLinkId>" + digits + r")/\Z",
            ("courseId", "courseItemLinkId"))
        assert patterns["chart_link"] == (
            r"\A/api/v2/attempts/(?P<attemptId>" + uuid + r")/chart\-link/\Z", ("attemptId",))
        assert course_contracts._UUID.pattern == uuid + r"\Z"
        assert [spec.route_id for _, _, spec in course_http._COMPILED] == [spec.route_id for spec in APP_ROUTES]

    @pytest.mark.parametrize("query,status,code", [
        ({"pageSize": "1001"}, 400, "INVALID_REQUEST"),
        ({"pageSize": "0"}, 400, "INVALID_REQUEST"),
        ({"page": "01"}, 400, "INVALID_REQUEST"),
        ({"page": "12345678901234567"}, 400, "INVALID_REQUEST"),
        ({"page": "9007199254740992"}, 400, "INVALID_REQUEST"),
        ({"page": ""}, 400, "INVALID_REQUEST"),
        ({"enrollmentId": "1"}, 400, "INVALID_REQUEST"),
    ])
    def test_course_list_query_rejections(self, query, status, code):
        response = http_with(None).dispatch(event("GET", "/api/v2/courses/progress/", token=TOKEN, query=query))
        assert (response["statusCode"], body_of(response)["error"]["code"]) == (status, code)

    def test_course_list_query_defaults_and_limits(self):
        seen = []
        http = http_with(None)
        http._service.list_courses = lambda auth, *, page, page_size: (
            seen.append((page, page_size)) or {"results": [], "count": 0, "next": None, "previous": None})
        for query in ({}, {"page": "9007199254740991"}, {"pageSize": "1000"}, {"page": "2", "pageSize": "3"}):
            response = http.dispatch(event("GET", "/api/v2/courses/progress/", token=TOKEN, query=query))
            assert response["statusCode"] == 200
        assert seen == [(1, 100), (9007199254740991, 100), (1, 1000), (2, 3)]

    @pytest.mark.parametrize("path,query,status", [
        ("/api/v2/courses/101/progress/", {}, 400),
        ("/api/v2/courses/101/items/1001/", {}, 400),
        ("/api/v2/courses/1234567890123456/progress/", {"enrollmentId": "1"}, 200),
        ("/api/v2/courses/12345678901234567/progress/", {"enrollmentId": "1"}, 404),
        ("/api/v2/courses/0/progress/", {"enrollmentId": "1"}, 404),
        ("/api/v2/courses/9007199254740992/progress/", {"enrollmentId": "1"}, 400),
        ("/api/v2/courses/101/progress", {"enrollmentId": "1"}, 404),
        ("/api/v2/attempts/74000000-0000-4000-8000-00000000000A/", {}, 404),
    ])
    def test_required_query_and_path_digits(self, path, query, status):
        http = http_with(None)
        http._service.get_course = lambda auth, *, course_id, enrollment_id: _raise(CourseError("NOT_FOUND")) \
            if course_id != 1234567890123456 else view_of(0)[0]
        response = http.dispatch(event("GET", path, token=TOKEN, query=query))
        # The stub returns the fixture view for the 16-digit id; only routing is characterized.
        assert response["statusCode"] == status
        if status != 200:
            assert body_of(response)["error"]["code"] == {400: "INVALID_REQUEST", 404: "NOT_FOUND"}[status]

    def test_course_list_default_path_is_the_route_path(self):
        view, _, _ = view_of(0)
        data = course_list_data([view], count=5, page=2, page_size=1)
        assert data["previous"] == "/api/v2/courses/progress/?page=1&pageSize=1"
        assert data["next"] == "/api/v2/courses/progress/?page=3&pageSize=1"

    def test_body_size_limits_by_kind(self):
        http = http_with(None)
        limit = fixture_course_settings().max_control_body_bytes
        big = json.dumps({"loginId": "x" * limit, "password": "p"})
        response = http.dispatch(event("POST", "/api/v2/sessions/", body=big))
        assert body_of(response)["error"]["code"] == "PAYLOAD_TOO_LARGE"
        response = http.dispatch(event("GET", "/api/v2/session/", token=TOKEN, body="{}"))
        assert body_of(response)["error"]["code"] == "INVALID_REQUEST"


# --- S8a-05 in course_http: request body text checks -------------------------------


class TestHttpBodyText:
    @pytest.mark.parametrize("body", [
        {"loginId": "", "password": "2222"},
        {"loginId": "test@test.com", "password": ""},
        {"loginId": 1, "password": "2222"},
        {"loginId": "test@test.com", "password": None},
        {"loginId": "test@test.com"},
        '{"loginId": "\\ud800", "password": "2222"}',
        '{"loginId": "test@test.com", "password": "\\udc00"}',
    ])
    def test_login_body_rejections_are_400(self, body):
        response = http_with(None).dispatch(event("POST", "/api/v2/sessions/", body=body))
        assert (response["statusCode"], body_of(response)["error"]["code"]) == (400, "INVALID_REQUEST")

    @pytest.mark.parametrize("body", [{"resumeCredential": ""}, {"resumeCredential": 1}, {}, {"x": "y"}])
    def test_reauthorize_body_rejections_are_400(self, body):
        response = http_with(None).dispatch(event(
            "POST", f"/api/v2/attempts/{ATTEMPT_ID}/reauthorize/", body=body, token=TOKEN))
        assert (response["statusCode"], body_of(response)["error"]["code"]) == (400, "INVALID_REQUEST")

    @pytest.mark.parametrize("body", [{"reason": ""}, {"reason": "other"}, {"reason": None}])
    def test_cancel_body_rejections_are_400(self, body):
        response = http_with(None).dispatch(event(
            "POST", f"/api/v2/attempts/{ATTEMPT_ID}/cancel/", body=body, token=TOKEN))
        assert (response["statusCode"], body_of(response)["error"]["code"]) == (400, "INVALID_REQUEST")

    @pytest.mark.parametrize("field,value", [
        ("clientRequestId", "x"), ("courseId", 0), ("enrollmentId", True), ("courseItemLinkId", "1"),
        ("definitionHash", "A" * 64),
    ])
    def test_start_body_rejections_are_400(self, field, value):
        body = {"clientRequestId": START_ID, "courseId": 101, "enrollmentId": 301, "courseItemLinkId": 1001,
                "definitionHash": HASH}
        body[field] = value
        response = http_with(None).dispatch(event("POST", "/api/v2/learning-starts/", body=body, token=TOKEN))
        assert (response["statusCode"], body_of(response)["error"]["code"]) == (400, "INVALID_REQUEST")


# --- S8a-09: dormant resume branch and constant enums --------------------------------


class TestAttemptRoutesAndEnums:
    def test_attempt_get_never_issues_resume_and_reauthorize_issues_once(self):
        issued = []
        record = attempt_record(ATTEMPT_ID, "queued", "2026-09-28T00:00:00Z", CONDITION, legacy=True)
        http = http_with(None)
        http._hooks = replace(
            http._hooks,
            load_attempt=lambda auth, attempt_id: record,
            reauthorize=lambda auth, attempt_id, credential: record,
            issue_resume=lambda auth, attempt_id: issued.append((auth, attempt_id)) or "fresh",
        )
        data = body_of(http.dispatch(event("GET", f"/api/v2/attempts/{ATTEMPT_ID}/", token=TOKEN)))["data"]
        assert "resumeCredential" not in data and issued == []
        data = body_of(http.dispatch(event(
            "POST", f"/api/v2/attempts/{ATTEMPT_ID}/reauthorize/", body={"resumeCredential": "r"}, token=TOKEN,
        )))["data"]
        assert data["resumeCredential"] == "fresh"
        assert issued == [(AUTH, ATTEMPT_ID)]

    def test_icon_type_is_always_the_summary_item_type(self):
        view, _, _ = view_of(0)
        items = course_detail_data(view)["courseItems"]
        assert [(item["iconType"], item["itemType"], item["contentType"]) for item in items] == [
            ("video", "content", "video"), ("pdf", "content", "pdf"), ("training", "training", None),
            ("training", "training", None), ("assessment", "assessment", None),
        ]

    def test_enum_constants_match_the_response_sets(self):
        assert ATTEMPT_STATES == {"created", "queued", "processing", "evaluated", "cancelled", "failed",
                                  "outcome_unknown"}
        assert CALCULATION_STATUSES == {"pending", "succeeded"}
        assert SUBMIT_ARC_STATUSES == {"disabled", "excluded"}
        assert START_ROLES == {"training", "final_assessment"}


# --- S8a-04: wire schema declarations agree with the serializer ------------------------


class TestWireSchemaAgreement:
    def test_enrollment_nullables_match_the_schema(self):
        row = {"id": 301, "status": "ENROLLED", "courseTitle": "t", "courseId": 101, "loginAt": "a",
               "finishedAt": "b", "elapsedSeconds": 1, "centerName": "c", "enrollStatusCode": "d"}
        assert tuple(row) == ENROLLMENT_FIELDS
        accepted = set()
        for key in ENROLLMENT_FIELDS:
            try:
                course_schema.validate_enrollment({**row, key: None}, enrollment_id=301, course_id=101)
            except CourseError as error:
                assert error.code == "UPSTREAM_CONTRACT_MISMATCH"
            else:
                accepted.add(key)
        assert accepted == set(ENROLLMENT_NULLABLE) == {
            "loginAt", "finishedAt", "elapsedSeconds", "centerName", "enrollStatusCode"}

    def test_file_detail_nullables_match_the_serializer(self):
        row = {"id": 1, "fileName": "f", "order": 1, "url": "u", "contentUrl": "c"}
        assert tuple(row) == FILE_DETAIL_FIELDS
        accepted = set()
        for key in FILE_DETAIL_FIELDS:
            try:
                course_schema.validate_file_detail({**row, key: None})
            except CourseError:
                continue
            accepted.add(key)
            assert course_response._file_detail({**row, key: None})[key] is None
        assert accepted == {"url", "contentUrl"}

    def test_wire_field_orders_follow_the_contract_tuples(self):
        view, _, _ = view_of(0)
        detail = course_detail_data(view)
        assert tuple(detail) == ("courseItems", "enrollment", "progressId", "definitionHash",
                                 "learningAvailability")
        assert tuple(detail["enrollment"]) == ("id", "status", "courseTitle", "courseId", "loginAt",
                                               "finishedAt", "elapsedSeconds", "centerName", "enrollStatusCode")
        row = course_list_data([view], count=1, page=1, page_size=100)["results"][0]
        assert tuple(row) == ("courseId", "courseName", "status", "summary", "certificationType",
                              "enrollmentId", "progressId", "learningAvailability")
        assert tuple(row["summary"][0]) == ("id", "itemType", "title", "displayOrder")
        item = item_detail_data(view, 1001)
        assert tuple(item) == ("id", "title", "itemType", "displayOrder", "courseItemLinkId", "usage",
                               "logicalId", "description", "detail")
        assert tuple(item["detail"]) == ("id", "fileName", "order", "url", "contentUrl")
        training = item_detail_data(view, 1003)["detail"]
        assert tuple(training) == ("id", "title", "trainingType", "feedbackType", "trainingMode", "training",
                                   "assessment", "content")
        assert (item["id"], item["displayOrder"], item["courseItemLinkId"]) == (201, 1, 1001)


# --- S8b-17: settings validation ---------------------------------------------------------


class TestCourseSettingsValidation:
    NAMES = (
        "max_course_items", "max_assignments", "max_bundle_bytes", "max_control_body_bytes",
        "max_intervals_per_report", "max_merged_intervals_per_start", "max_reports_per_start",
        "max_transaction_actions", "max_conflict_retries",
    )

    def test_field_order_is_the_declared_order(self):
        assert tuple(item.name for item in fields(CourseSettings)) == self.NAMES

    @pytest.mark.parametrize("name", NAMES)
    @pytest.mark.parametrize("bad", [0, -1, True, 1.0, None, "1"])
    def test_every_field_rejects_non_positive_int(self, name, bad):
        with pytest.raises(ValueError) as raised:
            replace(fixture_course_settings(), **{name: bad})
        assert str(raised.value) == "Invalid course settings."
        assert type(raised.value) is ValueError

    def test_conflict_retry_bound_is_eight(self):
        assert replace(fixture_course_settings(), max_conflict_retries=8).max_conflict_retries == 8
        with pytest.raises(ValueError):
            replace(fixture_course_settings(), max_conflict_retries=9)
        assert replace(fixture_course_settings(), max_transaction_actions=10**30).max_transaction_actions == 10**30

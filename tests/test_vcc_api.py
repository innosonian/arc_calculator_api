"""VCC HTTP/service unit tests against wire_cases.json. Does not recapture golden."""

from copy import deepcopy
import hashlib
import json

from mock_journey.course_contracts import (
    APP_ROUTES, CourseView, GateView, InventoryView, PAGE_SIZE_MAX, parse_owned,
)
from mock_journey.course_errors import CourseError, course_error_table
from mock_journey.course_http import map_cancel_reason
from mock_journey.course_response import submit_arc_data
from mock_journey.course_settings import fixture_course_settings
from tests.vcc_api_support import (  # noqa: F401 (re-export)
    ATTEMPT_ID, BUNDLE_DOC, CLOCK, CONDITION, CONTRACT_HASH, EPOCH, EXPIRES, FIXTURES, LEGACY_ID, REQUEST_ID,
    RESUME, ROOT, SESSION_ID, START_ID, TOKEN, WIRE, FakeBridge, FakePolicy, FakeProvider, FakeRepository, World,
    assignment_of, bundle_of, command_digest, decode, event, learner_from, load_progress, placement_from,
    route_path, view_of,
)


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
    elif case_id == "prerequisites":
        world.policy.errors[1005] = "PREREQUISITES_NOT_COMPLETED"
    elif case_id == "final_active":
        world.policy.errors[1005] = "FINAL_ASSESSMENT_ACTIVE"
    elif case_id == "final_recovery":
        world.policy.errors[1005] = "FINAL_ASSESSMENT_RECOVERY_REQUIRED"
    elif case_id == "policy_pending":
        world.policy.errors[1005] = "COMPLETION_POLICY_PENDING"
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
                    "prerequisites", "final_active", "final_recovery", "policy_pending",
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
                                "prerequisites", "final_active", "final_recovery", "policy_pending",
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
        assert data["userName"] == "Test User"  # D129: login only
        assert list(data) == ["sessionId", "expiresAt", "learningAvailability", "accessToken", "tokenType", "userName"]
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



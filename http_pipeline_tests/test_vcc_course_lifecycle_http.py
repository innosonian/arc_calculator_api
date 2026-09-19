"""Assigned learner HTTP journey with real DynamoDB and original binary scoring.

The authenticated student binding is an explicit synthetic fixture. These tests
exercise the REST handler and worker; they do not certify ARC authentication or
an iOS device. Every response status and business transition is asserted.
"""

import json
from types import SimpleNamespace
import uuid

from mock_journey.course_errors import CourseError
from mock_journey.handler import handle
from tests.vcc_runtime_support import measurement_event, runtime
from tests.vcc_support import event


CONTEXT = SimpleNamespace(aws_request_id="vcc-course-lifecycle")


def request(env, method, path, *, expected, body=None, query=None):
    response = handle(event(method, path, token=env.token, body=body, query=query), CONTEXT, env.app)
    assert response["statusCode"] == expected, response["body"]
    if expected == 204:
        assert response["body"] == ""
        return None
    payload = json.loads(response["body"])
    assert payload["success"] is (expected < 400)
    assert type(payload["timestamp"]) is str
    return payload["data"] if expected < 400 else payload["error"]


def course(env, enrollment=501):
    return request(env, "GET", "/api/v2/courses/101/progress/", expected=200,
                   query={"enrollmentId": str(enrollment)})


def start_body(env, placement):
    return {
        "clientRequestId": str(uuid.uuid4()), "courseId": 101, "enrollmentId": 501,
        "courseItemLinkId": placement, "definitionHash": course(env)["definitionHash"],
    }


def start(env, placement, *, content=False):
    body = start_body(env, placement)
    path = "/api/v2/learning-starts/" if content else "/api/v2/attempts/"
    return request(env, "POST", path, body=body, expected=201), body


def report(env, start_data, content_event, *, report_id=None):
    body = {
        "enrollmentId": 501, "courseItemLinkId": start_data["courseItemLinkId"],
        "startId": start_data["startId"], "reportId": report_id or str(uuid.uuid4()),
        "contentVersion": start_data["contentVersion"], "event": content_event,
    }
    result = request(env, "PUT", "/api/v2/courses/101/progress/", body=body, expected=200)
    return result, body


def calculate(env, attempt_view, *, count=None, completed):
    attempt_id = attempt_view["attemptId"]
    # Use the accepted immutable definition solely to form the original multipart
    # binary payload. Both submission and result retrieval go through the handler.
    stored = env.app.state.get_attempt(env.auth, attempt_id)
    upload = measurement_event(stored, count=count)
    upload["path"] = f"/api/v2/attempts/{attempt_id}/calculation/"
    upload["headers"]["Authorization"] = f"Bearer {env.token}"
    accepted = handle(upload, CONTEXT, env.app)
    assert accepted["statusCode"] == 202, accepted["body"]
    pending = json.loads(accepted["body"])["data"]
    assert pending["calculationStatus"] == "pending"
    assert pending["calculation"] is None and pending["evaluation"] is None
    pending_get = request(env, "GET", upload["path"], expected=202)
    assert pending_get == pending
    stored = env.app.state.get_attempt(env.auth, attempt_id)
    assert stored["state"] == "queued" and stored["job_id"]
    assert env.worker.process(stored["job_id"]) is True
    result = request(env, "GET", upload["path"], expected=200)
    assert result["calculationStatus"] == "succeeded"
    assert type(result["calculation"]) is dict
    assert result["evaluation"]["program_completed"] is completed
    assert result["submit_arc"] == {
        "status": "disabled", "ok": False, "error": "arc_contract_pending", "exclusionReasons": [],
    }
    public_attempt = request(env, "GET", f"/api/v2/attempts/{attempt_id}/", expected=200)
    assert public_attempt["state"] == "evaluated"
    assert "resumeCredential" not in public_attempt
    assert public_attempt["condition"] == attempt_view["condition"]
    assert request(env, "GET", upload["path"], expected=200) == result
    replay = handle(upload, CONTEXT, env.app)
    assert replay["statusCode"] == 200, replay["body"]
    assert json.loads(replay["body"])["data"] == result
    return result


def test_assigned_course_full_http_lifecycle_and_final_retry_rules(dynamodb_client, dynamodb_table):
    env = runtime(dynamodb_client, dynamodb_table)
    listing = request(env, "GET", "/api/v2/courses/progress/", expected=200)
    assert listing["count"] == 2
    assert [(row["courseId"], row["enrollmentId"]) for row in listing["results"]] == [(101, 501), (101, 502)]
    assert all(row["status"] == "NOT_STARTED" for row in listing["results"])
    assert request(env, "GET", "/api/v2/session/", expected=200)["learningAvailability"] == {
        "state": "ready", "reason": None,
    }
    initial = course(env)
    assert [row["courseItemLinkId"] for row in initial["courseItems"]] == [1001, 1002, 1003, 1004, 1005]
    assert all(row["isCompleted"] is False and row["isPassed"] is None for row in initial["courseItems"])
    training = request(env, "GET", "/api/v2/courses/101/items/1003/", expected=200,
                       query={"enrollmentId": "501"})
    assert training["detail"]["trainingType"] == "chest compression only"
    assert training["detail"]["training"]["duration"] is None
    error = request(env, "POST", "/api/v2/attempts/", body=start_body(env, 1005), expected=409)
    assert error["code"] == "PREREQUISITES_NOT_COMPLETED"

    video, _ = start(env, 1001, content=True)
    first, first_body = report(env, video, {"type": "video_segments", "intervalsMs": [[0, 10000]]})
    assert first["isCompleted"] is False and first["isPassed"] is None
    complete_video, _ = report(env, video, {"type": "video_segments", "intervalsMs": [[10000, 20000]]})
    assert complete_video["isCompleted"] is True and complete_video["isPassed"] is None
    assert request(env, "PUT", "/api/v2/courses/101/progress/", body=first_body, expected=200) == first

    document, _ = start(env, 1002, content=True)
    display_id = str(uuid.uuid4())
    confirmed, _ = report(env, document, {"type": "document_confirmed", "displayReportId": display_id})
    assert confirmed["isCompleted"] is False and confirmed["application"] == "pending_evidence"
    displayed, _ = report(env, document, {"type": "document_displayed"}, report_id=display_id)
    assert displayed["isCompleted"] is True

    practice_a, _ = start(env, 1003)
    assert practice_a["role"] == "training"
    calculate(env, practice_a, completed=True)
    items = {row["courseItemLinkId"]: row for row in course(env)["courseItems"]}
    assert items[1003]["isCompleted"] is True and items[1004]["isCompleted"] is False
    assert all(row["isCompleted"] is False for row in course(env, enrollment=502)["courseItems"])
    error = request(env, "POST", "/api/v2/attempts/", body=start_body(env, 1003), expected=409)
    assert error["code"] == "ITEM_ALREADY_COMPLETED"
    practice_b, _ = start(env, 1004)
    calculate(env, practice_b, completed=True)
    assert all(row["isCompleted"] is True for row in course(env)["courseItems"][:-1])

    cancelled_final, _ = start(env, 1005)
    assert cancelled_final["role"] == "final_assessment"
    error = request(env, "POST", "/api/v2/attempts/", body=start_body(env, 1005), expected=409)
    assert error["code"] == "FINAL_ASSESSMENT_ACTIVE"
    request(env, "POST", f"/api/v2/attempts/{cancelled_final['attemptId']}/cancel/",
            body={"reason": "user_cancelled"}, expected=204)
    assert request(env, "GET", f"/api/v2/attempts/{cancelled_final['attemptId']}/", expected=200)["state"] == "cancelled"
    final_ids = []
    # Two independent failed attempts demonstrate that no one-retry counter is
    # introduced. The no-limit policy itself is separately tested in policy tests.
    for _ in range(2):
        failed_final, _ = start(env, 1005)
        final_ids.append(failed_final["attemptId"])
        result = calculate(env, failed_final, count=2, completed=False)
        assert "GOAL_NOT_MET" in result["evaluation"]["reason_codes"]
        assert course(env)["courseItems"][-1]["isCompleted"] is False
    assert len(set(final_ids)) == 2
    final, final_body = start(env, 1005)
    calculate(env, final, completed=True)
    finished = course(env)
    assert all(row["isCompleted"] is True for row in finished["courseItems"])
    assert finished["courseItems"][-1]["isPassed"] is True
    listing = request(env, "GET", "/api/v2/courses/progress/", expected=200)
    assert [(row["enrollmentId"], row["status"]) for row in listing["results"]] == [(501, "FINISHED"), (502, "NOT_STARTED")]
    error = request(env, "POST", "/api/v2/attempts/", body=start_body(env, 1005), expected=409)
    assert error["code"] == "ASSESSMENT_ALREADY_PASSED"
    replay = request(env, "POST", "/api/v2/attempts/", body=final_body, expected=200)
    assert replay["attemptId"] == final["attemptId"]


def test_session_and_course_waiting_agree_without_losing_existing_start(dynamodb_client, dynamodb_table, monkeypatch):
    env = runtime(dynamodb_client, dynamodb_table)
    video, original_request = start(env, 1001, content=True)
    original_fetch = env.app.provider.fetch_bundle

    def unavailable(_binding):
        raise CourseError("CONTRACT_PENDING")

    monkeypatch.setattr(env.app.provider, "fetch_bundle", unavailable)
    waiting = {"state": "waiting", "reason": "contract_pending"}
    assert request(env, "POST", "/api/v2/session/refresh/", body={}, expected=200)["learningAvailability"] == waiting
    assert request(env, "GET", "/api/v2/session/", expected=200)["learningAvailability"] == waiting
    assert course(env)["learningAvailability"] == waiting
    error = request(env, "POST", "/api/v2/learning-starts/", body=start_body(env, 1002), expected=503)
    assert error["code"] == "ARC_PROGRESS_UNAVAILABLE"
    assert request(env, "POST", "/api/v2/learning-starts/", body=original_request, expected=200) == video
    completed, _ = report(env, video, {"type": "video_segments", "intervalsMs": [[0, 20000]]})
    assert completed["isCompleted"] is True
    assert course(env)["courseItems"][0]["isCompleted"] is True
    monkeypatch.setattr(env.app.provider, "fetch_bundle", original_fetch)
    ready = {"state": "ready", "reason": None}
    assert request(env, "POST", "/api/v2/session/refresh/", body={}, expected=200)["learningAvailability"] == ready
    assert request(env, "GET", "/api/v2/session/", expected=200)["learningAvailability"] == ready
    assert course(env)["learningAvailability"] == ready
    assert course(env)["courseItems"][0]["isCompleted"] is True


def test_calculation_excluded_wire_after_owned_epoch_reset(dynamodb_client, dynamodb_table):
    env = runtime(dynamodb_client, dynamodb_table)
    attempt, _ = start(env, 1003)
    stored = env.app.state.get_attempt(env.auth, attempt["attemptId"])
    upload = measurement_event(stored)
    upload["path"] = f"/api/v2/attempts/{attempt['attemptId']}/calculation/"
    upload["headers"]["Authorization"] = f"Bearer {env.token}"
    response = handle(upload, CONTEXT, env.app)
    assert response["statusCode"] == 202, response["body"]
    job_id = env.app.state.get_attempt(env.auth, attempt["attemptId"])["job_id"]
    # The public API has no reset route. Inject only the reset authority's epoch
    # transition in this test-owned table to exercise late-result serialization.
    key = {"PK": {"S": f"USER#{env.auth.principal}"}, "SK": {"S": "STATE"}}
    user = dynamodb_client.get_item(TableName=dynamodb_table, Key=key, ConsistentRead=True)["Item"]
    dynamodb_client.update_item(
        TableName=dynamodb_table, Key=key,
        UpdateExpression="SET #epoch = :new, #revision = :next",
        ConditionExpression="#epoch = :old AND #revision = :current",
        ExpressionAttributeNames={"#epoch": "epoch", "#revision": "revision"},
        ExpressionAttributeValues={
            ":new": {"S": str(uuid.uuid4())}, ":old": user["epoch"],
            ":next": {"N": str(int(user["revision"]["N"]) + 1)}, ":current": user["revision"],
        },
    )
    assert env.worker.process(job_id) is True
    result = request(env, "GET", upload["path"], expected=200)
    assert result["calculationStatus"] == "succeeded"
    assert type(result["calculation"]) is dict
    assert result["evaluation"]["program_completed"] is True
    assert result["progressApplication"] == {"applied": False, "applied_epoch": None, "reason": "PROGRESS_RESET"}
    assert result["submit_arc"] == {
        "status": "excluded", "ok": False, "error": None,
        "exclusionReasons": ["progress_reset_before_result"],
    }
    assert "exclusion_reasons" not in result["submit_arc"]
    assert request(env, "GET", upload["path"], expected=200) == result

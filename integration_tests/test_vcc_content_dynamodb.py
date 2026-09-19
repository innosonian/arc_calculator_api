"""Content reports and D6 transition against DynamoDB Local."""

import json
from types import SimpleNamespace

from mock_journey.course_provider import UnavailableCourseProvider
from mock_journey.handler import handle
from tests.vcc_support import event
from integration_tests.test_vcc_state_dynamodb import application, login


CONTEXT = SimpleNamespace(aws_request_id="vcc-content")


def test_new_accept_stop_keeps_lookup(dynamodb_client, dynamodb_table):
    app = application(dynamodb_client, dynamodb_table)
    token, _ = login(app)
    listed = handle(event("GET", "/api/v2/courses/progress/", token=token, query={"page": "1", "pageSize": "10"}),
                    CONTEXT, app)
    assert listed["statusCode"] == 200
    # Stop new enrollments by swapping provider; Dummy inventory stays.
    app.provider = UnavailableCourseProvider()
    app.course_service._provider = UnavailableCourseProvider()
    listed = handle(event("GET", "/api/v2/courses/progress/", token=token, query={"page": "1", "pageSize": "10"}),
                    CONTEXT, app)
    assert listed["statusCode"] == 200
    assert json.loads(listed["body"])["data"]["count"] == 0


def test_session_read_does_not_repeat_login_credentials(dynamodb_client, dynamodb_table):
    # Response disclosure check only; operational-log redaction has its own
    # recorder/failure tests in the existing operational logging suite.
    app = application(dynamodb_client, dynamodb_table)
    token, _ = login(app)
    response = handle(event("GET", "/api/v2/session/", token=token), CONTEXT, app)
    assert response["statusCode"] == 200
    data = json.loads(response["body"])["data"]
    assert set(data) == {"sessionId", "expiresAt", "learningAvailability"}
    text = response["body"]
    assert "2222" not in text
    assert "resumeCredential" not in text and "accessToken" not in text
    assert token not in text

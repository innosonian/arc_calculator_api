"""Loopback HTTP + DynamoDB Local for course_v2 login/list/query/security."""

import json
from types import SimpleNamespace

from mock_journey.handler import handle
from tests.vcc_support import event
from integration_tests.test_vcc_state_dynamodb import application, login


CONTEXT = SimpleNamespace(aws_request_id="vcc-http")


def test_handler_login_list_matches_http_envelope(dynamodb_client, dynamodb_table):
    app = application(dynamodb_client, dynamodb_table)
    token, data = login(app)
    assert data["tokenType"] == "Bearer"
    listed = handle(event("GET", "/api/v2/courses/progress/", token=token, query={"page": "1", "pageSize": "10"}),
                    CONTEXT, app)
    body = json.loads(listed["body"])
    assert listed["statusCode"] == 200
    assert body["success"] is True
    assert body["data"]["count"] == 0
    session = handle(event("GET", "/api/v2/session/", token=token), CONTEXT, app)
    assert session["statusCode"] == 200
    assert "accessToken" not in json.loads(session["body"])["data"]


def test_duplicate_query_rejected(dynamodb_client, dynamodb_table):
    app = application(dynamodb_client, dynamodb_table)
    token, _ = login(app)
    raw = event("GET", "/api/v2/courses/progress/", token=token, query={"page": "1", "pageSize": "10"})
    raw["multiValueQueryStringParameters"] = {"page": ["1", "2"], "pageSize": ["10"]}
    response = handle(raw, CONTEXT, app)
    assert response["statusCode"] == 400

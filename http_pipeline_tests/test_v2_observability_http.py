"""Q7=A with real DynamoDB: reused journey hooks carry the X-Request-Id value.

Login, calculation acceptance and a rejection go through the REST handler of the
assembled course_v2 application. Only the operational recorder is a test fake.
"""

import json
from types import SimpleNamespace
import uuid

from mock_journey.handler import handle
from services.operational_logs import validate_record
from tests.vcc_runtime_support import measurement_event, runtime, start_attempt
from tests.vcc_support import event


RUNTIME_ID = "72000000-0000-4000-8000-000000000002"
CONTEXT = SimpleNamespace(aws_request_id=RUNTIME_ID)


class ListRecorder:
    def __init__(self):
        self.rows = []

    def record(self, category, name, fields):
        raw = validate_record({"schema": 1, "log_id": str(uuid.uuid4()),
                               "occurred_at": "2026-09-28T00:00:00.000000Z", "role": "api",
                               "category": category, "event": name, "fields": fields})
        self.rows.append(json.loads(raw))
        return True


def events(recorder, header_id):
    return [row["event"] for row in recorder.rows if row["fields"].get("http_request_id") == header_id]


def test_course_v2_hook_events_and_rejections_share_the_response_request_id(dynamodb_client, dynamodb_table):
    env = runtime(dynamodb_client, dynamodb_table)
    recorder = ListRecorder()
    env.app.operations = recorder

    login = handle(event("POST", "/api/v2/sessions/", body={"loginId": "test@test.com", "password": "2222"}),
                   CONTEXT, env.app)
    assert login["statusCode"] == 201, login["body"]
    login_id = login["headers"]["X-Request-Id"]
    assert events(recorder, login_id) == ["login_succeeded"]
    succeeded = recorder.rows[-1]["fields"]
    assert succeeded["request_id"] == RUNTIME_ID and type(succeeded["session_id"]) is str
    access_token = json.loads(login["body"])["data"]["accessToken"]
    assert access_token not in json.dumps(recorder.rows)

    attempt = start_attempt(env, 1003)
    upload = measurement_event(attempt)
    upload["path"] = f"/api/v2/attempts/{attempt['attempt_id']}/calculation/"
    upload["headers"]["Authorization"] = f"Bearer {env.token}"
    accepted = handle(upload, CONTEXT, env.app)
    assert accepted["statusCode"] == 202, accepted["body"]
    accepted_id = accepted["headers"]["X-Request-Id"]
    assert accepted_id != login_id
    assert "calculation_accepted" in events(recorder, accepted_id)
    row = next(row for row in recorder.rows if row["event"] == "calculation_accepted")
    assert row["fields"]["attempt_id"] == attempt["attempt_id"]
    assert row["fields"]["http_request_id"] == accepted_id

    missing = handle(event("GET", f"/api/v2/attempts/{uuid.uuid4()}/", token=env.token), CONTEXT, env.app)
    assert missing["statusCode"] == 404
    missing_id = missing["headers"]["X-Request-Id"]
    assert events(recorder, missing_id) == ["request_rejected"]
    assert recorder.rows[-1]["fields"]["error_code"] == "NOT_FOUND"
    assert recorder.rows[-1]["fields"]["http_status"] == 404

    raw = json.dumps(recorder.rows)
    assert env.token not in raw and upload["body"] not in raw

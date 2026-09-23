"""AWS entrypoints with real isolated DynamoDB and real internal scoring.

S3 and AWS invocation envelopes are explicit test doubles. This does not claim
actual API Gateway, SQS, IAM, Lambda Linux, or ARC service acceptance.
"""

import json
import uuid

import lambda_handler
from mock_journey import runtime as api_entry, worker as worker_entry, worker_runtime
from mock_journey.aws_runtime import build_runtime
from tests.mock_storage_support import MemoryS3
from tests.test_aws_dev_course import course_configuration
from tests.test_aws_runtime import context, environment
from tests.vcc_runtime_support import measurement_event
from tests.vcc_support import event


def test_dummy_dev_lambda_entrypoints_complete_real_binary_and_survive_reassembly(
    dynamodb_client, dynamodb_table, monkeypatch,
):
    objects = MemoryS3()

    class OwnedClientView:
        # Invocation log writers own their client views, not the test fixture's
        # actual shared connection pool.
        def __getattr__(self, name):
            return getattr(dynamodb_client, name)

        def close(self):
            pass

    def factory(service, **kwargs):
        assert kwargs["region_name"] == "us-east-1"
        assert service in ("dynamodb", "s3")
        return objects if service == "s3" else OwnedClientView()

    def build(role):
        config = course_configuration(role)
        config["state"]["table_name"] = dynamodb_table
        config["logs"].update(capacity=256, flush_budget_ms=1000)
        if role == "worker":
            config["worker"]["lease_seconds"] = 60
        return build_runtime(role, environment(role, config=config), client_factory=factory)

    api, worker = build("api"), build("worker")
    monkeypatch.setattr(api_entry, "_application", api.target)
    monkeypatch.setattr(worker_runtime, "get_worker", lambda: worker.target)
    token = None

    def request(method, path, expected=200, *, body=None, query=None, override_token=None):
        response = lambda_handler.run(event(method, path, body=body,
            query=query, token=override_token or token), context(remaining=120000))
        assert response["statusCode"] == expected, response["body"]
        if expected == 204:
            return None
        payload = json.loads(response["body"])
        return payload["data"] if expected < 400 else payload["error"]

    login = request("POST", "/api/v2/sessions/", 201,
                    body={"loginId": "test@test.com", "password": "2222"})
    token = login["accessToken"]
    listing = request("GET", "/api/v2/courses/progress/")
    assert listing["count"] == 15
    assert all(row["status"] == "NOT_STARTED" for row in listing["results"])
    course_id, enrollment, practice_id, final_id = 910004, 920004, 940041, 940042
    path = f"/api/v2/courses/{course_id}/progress/"
    query = {"enrollmentId": str(enrollment)}

    def start(link_id, expected=201):
        definition = request("GET", path, query=query)["definitionHash"]
        return request("POST", "/api/v2/attempts/", expected, body={
            "clientRequestId": str(uuid.uuid4()), "courseId": course_id,
            "enrollmentId": enrollment, "courseItemLinkId": link_id, "definitionHash": definition,
        })

    assert start(final_id, 409)["code"] == "PREREQUISITES_NOT_COMPLETED"
    results = []
    for link_id in (practice_id, final_id):
        accepted = start(link_id)
        attempt_id = accepted["attemptId"]
        auth = api.target.auth.authenticate(token)
        stored = api.target.state.get_attempt(auth, attempt_id)
        upload = measurement_event(stored)
        upload["path"] = f"/api/v2/attempts/{attempt_id}/calculation/"
        upload["headers"]["Authorization"] = f"Bearer {token}"
        response = lambda_handler.run(upload, context(remaining=120000))
        assert response["statusCode"] == 202, response["body"]
        assert request("GET", upload["path"], 202)["calculationStatus"] == "pending"
        job_id = api.target.state.get_attempt(auth, attempt_id)["job_id"]
        message = {"Records": [{"messageId": str(uuid.uuid4()), "body": json.dumps({"job_id": job_id})}]}
        assert worker_entry.run(message, context(remaining=120000)) == {"batchItemFailures": []}
        # Duplicate SQS delivery must preserve the committed bytes and result.
        saved_objects = {key: value["Body"] for key, value in objects.objects.items()}
        assert worker_entry.run(message, context(remaining=120000)) == {"batchItemFailures": []}
        assert {key: value["Body"] for key, value in objects.objects.items()} == saved_objects
        result = request("GET", upload["path"])
        assert result["calculationStatus"] == "succeeded"
        assert type(result["calculation"]) is dict
        assert result["evaluation"]["program_completed"] is True
        assert result["submit_arc"] == {"status": "excluded", "ok": False,
            "error": None, "exclusionReasons": ["dummy"]}
        results.append((upload["path"], result))

    assert start(final_id, 409)["code"] == "ASSESSMENT_ALREADY_PASSED"
    assert all(item["isCompleted"] for item in request("GET", path, query=query)["courseItems"])
    before = {key: value["Body"] for key, value in objects.objects.items()}
    rebuilt = build("api")
    monkeypatch.setattr(api_entry, "_application", rebuilt.target)
    for result_path, expected in results:
        assert request("GET", result_path) == expected
    assert {key: value["Body"] for key, value in objects.objects.items()} == before
    another = request("POST", "/api/v2/sessions/", 201,
                      body={"loginId": "test@test.com", "password": "2222"})["accessToken"]
    assert request("GET", results[0][0], 404, override_token=another)["code"] == "NOT_FOUND"
    # Dummy logout resets shared progress but must not destroy existing result objects.
    request("DELETE", "/api/v2/session/", 204, override_token=another)
    assert request("GET", results[0][0]) == results[0][1]
    assert request("GET", path, 503, query=query)["code"] == "ARC_PROGRESS_UNAVAILABLE"
    assert request("POST", "/api/v2/session/refresh/", body={})["learningAvailability"]["state"] == "ready"
    assert not any(item["isCompleted"] for item in request("GET", path, query=query)["courseItems"])
    assert {key: value["Body"] for key, value in objects.objects.items()} == before

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
from tests.aws_runtime_support import context, course_configuration, environment
from tests._synth import PACKET_BYTES, cpr_session, multipart_event
from tests.journey_support import synthetic_measurement
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
            # Real DynamoDB Local calls: the unit-test renewal timing (0.05 s interval,
            # 0.1 s timeout) made a slow renewal under suite load lose the lease and
            # defer the job. These values still satisfy the lease-renewal rule.
            config["worker"].update(lease_seconds=60, renewal_interval_seconds=5, renewal_timeout_seconds=10)
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
    assert login["userName"] == "Test User"  # D129
    listing = request("GET", "/api/v2/courses/progress/")
    assert listing["count"] == 4
    assert all(row["status"] == "NOT_STARTED" for row in listing["results"])
    course_id, enrollment = 911003, 921003
    practice_ids, final_id = (950031, 950032, 950033, 950034), 950035
    path = f"/api/v2/courses/{course_id}/progress/"
    query = {"enrollmentId": str(enrollment)}

    def start(link_id, expected=201):
        definition = request("GET", path, query=query)["definitionHash"]
        return request("POST", "/api/v2/attempts/", expected, body={
            "clientRequestId": str(uuid.uuid4()), "courseId": course_id,
            "enrollmentId": enrollment, "courseItemLinkId": link_id, "definitionHash": definition,
        })

    assert start(final_id, 409)["code"] == "PREREQUISITES_NOT_COMPLETED"
    aed = b"".join(bytes((0xB4, event_id)) + timestamp.to_bytes(8, "big")
                   for event_id, timestamp in ((0, 100), (1, 1000), (10, 3000), (13, 5000), (16, 6000), (19, 6500)))
    after_shock = cpr_session([(30, 2)])
    after_shock = b"".join(
        chunk[:20] + (int.from_bytes(chunk[20:], "big") + 7000).to_bytes(8, "big")
        for chunk in (after_shock[i:i + PACKET_BYTES] for i in range(0, len(after_shock), PACKET_BYTES))
    )
    measurements = (
        (synthetic_measurement(ventilation=False, count=30), b""),
        (synthetic_measurement(ventilation=True, count=10), b""),
        (cpr_session([(30, 2)] * 3), b""),
        (after_shock, aed),
        (after_shock, aed),
    )
    results = []
    for link_id, (cpr_bytes, aed_bytes) in zip((*practice_ids, final_id), measurements):
        accepted = start(link_id)
        attempt_id = accepted["attemptId"]
        auth = api.target.auth.authenticate(token)
        parts = {"rawHexBPfile": cpr_bytes, "condition": json.dumps(accepted["condition"])}
        if aed_bytes:
            parts["aedHexBPfile"] = aed_bytes
        upload = multipart_event(parts)
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

    # D130: the passed final may be started again; the pass is kept while the retake is active (D131).
    retake = start(final_id)
    assert retake["role"] == "final_assessment" and retake["attemptId"] != attempt_id
    assert start(final_id, 409)["code"] == "FINAL_ASSESSMENT_ACTIVE"
    assert all(item["isCompleted"] for item in request("GET", path, query=query)["courseItems"])
    request("POST", f"/api/v2/attempts/{retake['attemptId']}/cancel/", 204, body={"reason": "user_cancelled"})
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
    # Explicit Dummy reset clears progress without destroying existing result objects.
    request("POST", "/api/v2/dev/progress-reset/", 204, override_token=another,
            body={"clientRequestId": str(uuid.uuid4())})
    assert request("GET", results[0][0]) == results[0][1]
    assert request("GET", path, 503, query=query)["code"] == "ARC_PROGRESS_UNAVAILABLE"
    assert request("POST", "/api/v2/session/refresh/", body={})["learningAvailability"]["state"] == "ready"
    assert not any(item["isCompleted"] for item in request("GET", path, query=query)["courseItems"])
    assert {key: value["Body"] for key, value in objects.objects.items()} == before

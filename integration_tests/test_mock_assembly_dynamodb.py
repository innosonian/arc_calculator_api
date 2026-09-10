"""Independent role wiring against real local DDB; S3/SQS/calculator are test-only.

The test adapter is imported from the existing P3 tests, never from runtime.
These tests prove composition/state behavior, not actual calculation or deployment.
"""

from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from integration_tests.test_mock_jobs_dynamodb import jobs_world  # noqa: F401 -- explicit local GSI fixture
from integration_tests.test_mock_journey import LocalDefinitions, LocalCalculator, api, login, create, submit, stored_attempt
from mock_journey.assembly import ExecutionCatalog, build_application, build_worker, build_relay
from mock_journey.catalog import PROGRAMS, TARGETS, slot_key
from mock_journey.projection import ProjectionSchema
from mock_journey.settings import StateSettings, StorageSettings, ApiSettings, WorkerSettings, RelaySettings
from mock_journey.worker import handle as worker_handle
from tests.mock_storage_support import MemoryLegacyBindings, MemoryS3


@pytest.fixture
def assembled(dynamodb_client, jobs_world):
    now = [1000]
    clock = lambda: now[0]
    objects = MemoryS3()
    bindings = MemoryLegacyBindings(objects)
    state_settings = StateSettings(jobs_world.repository.table_name, 8)
    storage_settings = StorageSettings("development", bindings.bucket, bindings.directory, 1000000, 2000000)
    definitions = {slot_key(program[0], target): LocalDefinitions().get_definition(program[0], target)
                   for program in PROGRAMS for target in TARGETS}
    execution = ExecutionCatalog(definitions, {"test-projection": ProjectionSchema("test-projection", {})})
    application_settings = ApiSettings(state_settings, storage_settings, "local-assembly-integration", 2000000)
    service = build_application(application_settings, dynamodb_client=dynamodb_client, s3_client=objects,
                                legacy_bindings=bindings, resume_keys={"v1": b"R" * 32}, current_key_version="v1",
                                execution=execution, clock=clock)
    adapter = LocalCalculator()
    worker_settings = WorkerSettings(state_settings, storage_settings, 60, 5)
    worker = build_worker(worker_settings, dynamodb_client=dynamodb_client, s3_client=objects,
                          legacy_bindings=bindings, adapters=[adapter], required_bindings=execution.required_bindings,
                          clock=clock)
    messages = []
    def send(**kwargs):
        messages.append(deepcopy(kwargs))
        return {"MessageId": "test-message-" + str(len(messages))}
    relay = build_relay(RelaySettings(state_settings, "https://sqs.example.invalid/local-only", 30, 5, 10, 2),
                        dynamodb_client=dynamodb_client, sqs_client=SimpleNamespace(send_message=send), clock=clock)
    assert service.state is not worker.jobs.state and worker.jobs.state is not relay.jobs.state
    return SimpleNamespace(service=service, auth=service.auth, state=service.state, jobs=service.calculation.jobs,
                           storage=service.calculation.storage, objects=objects, adapter=adapter, worker=worker,
                           relay=relay, messages=messages, now=now, clock=clock, definitions=definitions,
                           settings=application_settings, worker_settings=worker_settings, bindings=bindings)


def deliver(journey, job_id):
    assert journey.relay.dispatch(job_id) is True
    message = journey.messages[-1]
    assert set(message) == {"QueueUrl", "MessageBody"}
    assert json.loads(message["MessageBody"]) == {"job_id": job_id}
    event = {"Records": [{"messageId": "test-local-delivery", "body": message["MessageBody"]}]}
    assert worker_handle(event, None, journey.worker) == {"batchItemFailures": []}
    assert worker_handle(event, None, journey.worker) == {"batchItemFailures": []}


@pytest.mark.parametrize("reset", [False, True])
def test_separate_assembled_roles_share_durable_result_and_epoch_state(assembled, reset):
    journey = assembled
    token, other = login(journey), login(journey)
    attempt = create(journey, token)
    accepted = submit(journey, token, attempt)
    assert accepted["statusCode"] == 202 and json.loads(accepted["body"])["wait_expired"] is False
    saved = stored_attempt(journey, token, attempt)
    if reset:
        assert api(journey, "DELETE", "session", token=other)["statusCode"] == 204
        journey.now[0] += 31
    deliver(journey, saved["job_id"])
    assert len(journey.adapter.calls) == 1
    result = api(journey, "GET", "attempts/" + attempt["attempt_id"] + "/calculation", token=token)
    assert result["statusCode"] == 200
    assert submit(journey, token, attempt) == result
    calculation = json.loads(result["body"])
    assert "evaluation" not in calculation
    assert [type(value) for value in calculation["type_preservation"]] == [int, float, type(None), bool, str]
    attempt_result = json.loads(api(journey, "GET", "attempts/" + attempt["attempt_id"], token=token)["body"])
    assert attempt_result["evaluation"]["program_completed"] is True
    assert attempt_result["progress_application"]["applied"] is (not reset)
    assert attempt_result["progress_application"]["reason"] == ("PROGRESS_RESET" if reset else "APPLIED")
    progress_token = token if reset else other
    programs = json.loads(api(journey, "GET", "programs", token=progress_token)["body"])
    assert programs["programs"][0]["progress_by_target"]["adult"] == ("not_completed" if reset else "completed")
    # A distinct next slot can be selected after the stored result is viewed.
    following = create(journey, token, program="mock-compression-only", target="infant", request_id="next")
    assert following["state"] == "created"
    assert api(journey, "POST", "attempts/" + following["attempt_id"] + "/cancel",
               {"reason": "manikin_disconnected"}, token)["statusCode"] == 204


def test_reassembled_api_and_worker_keep_expired_attempt_versions(assembled, dynamodb_client):
    journey = assembled
    old_token = login(journey)
    attempt = create(journey, old_token)
    journey.now[0] += 86400
    definitions = deepcopy(journey.definitions)
    for value in definitions.values():
        value.update(adapter_version="next-adapter", projection_version="next-projection")
    execution = ExecutionCatalog(definitions, {
        "test-projection": ProjectionSchema("test-projection", {}),
        "next-projection": ProjectionSchema("next-projection", {}),
    })
    journey.service = build_application(journey.settings, dynamodb_client=dynamodb_client, s3_client=journey.objects,
                                        legacy_bindings=journey.bindings, resume_keys={"v1": b"R" * 32}, current_key_version="v1",
                                        execution=execution, clock=journey.clock)
    journey.auth, journey.state = journey.service.auth, journey.service.state
    next_adapter = LocalCalculator()
    next_adapter.version, next_adapter.projection_version = "next-adapter", "next-projection"
    journey.worker = build_worker(journey.worker_settings, dynamodb_client=dynamodb_client, s3_client=journey.objects,
                                  legacy_bindings=journey.bindings, adapters=[journey.adapter, next_adapter],
                                  required_bindings=execution.required_bindings + (("test-adapter", "test-projection"),),
                                  clock=journey.clock)
    token = login(journey)
    rebound = api(journey, "POST", "attempts/" + attempt["attempt_id"] + "/reauthorize",
                  {"resume_credential": attempt["resume_credential"]}, token)
    assert rebound["statusCode"] == 200
    assert submit(journey, token, attempt)["statusCode"] == 202
    saved = stored_attempt(journey, token, attempt)
    assert saved["epoch"] == attempt["progress_epoch"]
    deliver(journey, saved["job_id"])
    assert len(journey.adapter.calls) == 1 and next_adapter.calls == []
    result = api(journey, "GET", "attempts/" + attempt["attempt_id"] + "/calculation", token=token)
    assert result["statusCode"] == 200

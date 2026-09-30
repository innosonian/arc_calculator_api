"""Independent role wiring against real local DDB; S3/SQS are test-only doubles.

The API (build_course_application), Worker (build_worker) and Relay
(build_relay) are assembled separately over one DynamoDB Local table through
tests/journey_support.V2Journey, and talk only through durable rows, private
objects and queue message bodies. The Worker runs the real internal calculator,
or TypedProbeCalculator where a known adapter value list must cross every role
with its JSON types intact. These tests prove composition/state behavior, not
deployment.
"""

from copy import deepcopy
import dataclasses
import json
from types import SimpleNamespace
import uuid

import pytest

from mock_journey.assembly import ExecutionCatalog
from mock_journey.catalog import PROGRAMS, TARGETS
from mock_journey.contracts import CURRENT_ADAPTER_VERSION, PENDING_GOAL_ADAPTER_VERSION
from mock_journey.dev_course import DummyDevCourseProvider
from mock_journey.execution_definitions import PROJECTION_VERSION
from mock_journey.projection import ProjectionSchema
from mock_journey.worker import handle as worker_handle
from tests.journey_support import MEASUREMENT, LocalCalculator, V2Journey, dummy_course, dynamodb_local_store
from tests.legacy_rows_support import legacy_resume_credential, seed_legacy_rows


class CountingAdapter:
    """Delegates to a real adapter and records only how often it calculated."""

    def __init__(self, adapter):
        self.adapter = adapter
        self.version = adapter.version
        self.projection_version = adapter.projection_version
        self.calls = []

    def calculate(self, *args):
        self.calls.append(args[1]["call_id"])
        return self.adapter.calculate(*args)

    def __getattr__(self, name):
        return getattr(self.adapter, name)


TYPE_PROBE = [80, 80.0, None, False, "80"]
TYPE_PROBE_TYPES = [int, float, type(None), bool, str]


class TypedProbeCalculator(LocalCalculator):
    """LocalCalculator bound to the Dummy definitions' adapter/projection versions.

    Its core result carries ``type_preservation`` = [80, 80.0, None, False, "80"]:
    values the real calculator never emits (integral float, bool), so a write
    path turning 80.0 into 80 or False into 0 is visible on the wire. The
    current adapter version requires an explicit goal status; every goal kind
    is "evaluated" under the D136 cycle rule (worker.evaluate).
    """
    version = CURRENT_ADAPTER_VERSION
    projection_version = PROJECTION_VERSION

    def validate_response(self, raw, projected, binding):
        verified = super().validate_response(raw, projected, binding)
        assert verified.core_result["type_preservation"] == TYPE_PROBE
        assert verified.goal_kind != "cycles"
        return dataclasses.replace(verified, goal_status="evaluated")


@pytest.fixture
def assembled(dynamodb_client, request):
    calculator = getattr(request, "param", "internal")
    with dynamodb_local_store(dynamodb_client) as store:
        h = V2Journey(store)
        adapter = CountingAdapter(TypedProbeCalculator() if calculator == "typed_probe" else h.default_adapters()[0])
        h.adapters = [adapter]
        h.worker = h.build_worker()
        relay = h.build_relay(page_size=10, max_pages=2)
        assert h.api.state is not h.worker.jobs.state and h.worker.jobs.state is not relay.jobs.state
        yield SimpleNamespace(h=h, store=store, adapter=adapter, relay=relay, calculator=calculator)


def deliver(journey, job_id):
    assert journey.relay.dispatch(job_id) is True
    body = journey.h.queue.messages[-1]  # QueueCapture accepts exactly QueueUrl + MessageBody.
    assert json.loads(body) == {"job_id": job_id}
    event = {"Records": [{"messageId": "test-local-delivery", "body": body}]}
    assert worker_handle(event, None, journey.h.worker) == {"batchItemFailures": []}
    assert worker_handle(event, None, journey.h.worker) == {"batchItemFailures": []}


def leaf_types(value):
    if isinstance(value, dict):
        return set().union(*(leaf_types(item) for item in value.values())) if value else set()
    if isinstance(value, list):
        return set().union(*(leaf_types(item) for item in value)) if value else set()
    return {type(value)}


def same_typed(left, right):
    """Equal values with identical JSON leaf types (80 is not 80.0, None is not 0)."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(same_typed(left[key], right[key]) for key in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(same_typed(a, b) for a, b in zip(left, right))
    return left == right


@pytest.mark.parametrize("assembled", ["internal", "typed_probe"], indirect=True)
@pytest.mark.parametrize("reset", [False, True])
def test_separate_assembled_roles_share_durable_result_and_epoch_state(assembled, reset):
    journey, h = assembled, assembled.h
    probe = journey.calculator == "typed_probe"
    session, other = h.login(), h.login()
    course = dummy_course("mock-compression-only", "adult")
    started = h.start(session.token, course, course.practice_link_id)
    attempt_id = started["attemptId"]
    # LocalCalculator accepts only the fixed dataset measurement.
    accepted = h.upload(session.token, attempt_id, started["condition"], data=MEASUREMENT if probe else None)
    assert accepted["calculationStatus"] == "pending" and accepted["calculation"] is None
    saved = journey.store.row(f"ATTEMPT#{attempt_id}", "META")
    if reset:
        h.logout(other.token)
        h.advance(31)
    deliver(journey, saved["job_id"])
    assert len(journey.adapter.calls) == 1
    result = h.result(session.token, attempt_id)
    assert result["calculationStatus"] == "succeeded"
    assert h.upload(session.token, attempt_id, started["condition"], expected=200,
                    data=MEASUREMENT if probe else None) == result
    calculation = result["calculation"]
    assert "evaluation" not in calculation
    # The durable result body reaches the wire with every JSON leaf type kept.
    status, stored = h.api.calculation.result(h.api.auth.authenticate(session.token), attempt_id)
    assert status == 200 and same_typed(calculation, json.loads(stored))
    if probe:
        # Adapter output -> Worker write -> private file -> API wire: known values, exact types.
        assert [type(value) for value in calculation["type_preservation"]] == TYPE_PROBE_TYPES
        assert same_typed(calculation["type_preservation"], TYPE_PROBE)
        assert same_typed(json.loads(stored)["type_preservation"], TYPE_PROBE)
    assert {int, float, type(None), str} <= leaf_types(calculation)
    assert result["evaluation"]["program_completed"] is True
    assert result["progressApplication"]["applied"] is (not reset)
    assert result["progressApplication"]["reason"] == ("PROGRESS_RESET" if reset else "APPLIED")
    assert h.attempt(session.token, attempt_id)["state"] == "evaluated"
    progress_token = session.token if reset else other.token
    if reset:
        # The reset epoch has no course inventory until the remaining session refreshes it.
        h.refresh(session.token)
    items = {item["courseItemLinkId"]: item for item in h.course(progress_token, course)["courseItems"]}
    assert items[course.practice_link_id]["isCompleted"] is (not reset)
    # A distinct next course can be started after the stored result is viewed.
    following_course = dummy_course("mock-compression-only", "infant")
    following = h.start(session.token, following_course, following_course.practice_link_id)
    assert following["state"] == "created"
    h.cancel(session.token, following["attemptId"], reason="connection_lost")
    assert journey.store.row(f"ATTEMPT#{following['attemptId']}", "META")["state"] == "cancelled"


def next_execution(current):
    """Every definition moves to a new adapter/projection; the old projection stays readable."""
    definitions = {}
    for program, *_ in PROGRAMS:
        for target in TARGETS:
            definition = deepcopy(current.get_definition(program, target))
            definition.update(adapter_version="next-adapter", projection_version="next-projection")
            definitions[f"{program}:{target}"] = definition
    return ExecutionCatalog(definitions, {
        PROJECTION_VERSION: ProjectionSchema(PROJECTION_VERSION, {}),
        "next-projection": ProjectionSchema("next-projection", {}),
    })


@pytest.mark.parametrize("origin", ["course", "legacy"])
def test_reassembled_api_and_worker_keep_expired_attempt_versions(assembled, origin):
    journey, h = assembled, assembled.h
    adapter = journey.adapter
    if origin == "course":
        old = h.login()
        course = dummy_course("mock-compression-only", "adult")
        started = h.start(old.token, course, course.practice_link_id)
        attempt_id, credential = started["attemptId"], started["resumeCredential"]
    else:
        # Stored by /mock/v1 before its removal (D103: re-authorize and finish).
        seeded = seed_legacy_rows(journey.store, objects=h.objects)
        attempt_id = seeded.attempts["created"]["attempt_id"]
        credential = legacy_resume_credential(seeded, "created")
        # The legacy fixture attempt was stored under the retained pending-v3 adapter; the
        # reassembled Worker keeps calculating it with that adapter's meaning (D136, 3A).
        adapter = CountingAdapter(next(a for a in h.default_adapters() if a.version == PENDING_GOAL_ADAPTER_VERSION))
    before = journey.store.row(f"ATTEMPT#{attempt_id}", "META")
    h.advance(86400 + 60)  # The creating session has expired.
    old_version = json.loads(before["definition_json"])
    assert (old_version["adapter_version"], old_version["projection_version"]) == (
        adapter.version, adapter.projection_version)
    h.execution = next_execution(h.execution)
    h.provider = DummyDevCourseProvider(settings=h.course_settings, execution=h.execution)
    h.api = h.build_api()
    next_adapter = LocalCalculator()
    next_adapter.version, next_adapter.projection_version = "next-adapter", "next-projection"
    h.worker = h.build_worker(
        [adapter, next_adapter],
        required_bindings=h.execution.required_bindings + ((adapter.version, adapter.projection_version),),
    )
    session = h.login()
    rebound = h.reauthorize(session.token, attempt_id, credential)
    assert rebound["attemptId"] == attempt_id and rebound["state"] == "created"
    condition = h.attempt(session.token, attempt_id)["condition"]
    assert condition == old_version["condition"]
    h.upload(session.token, attempt_id, condition)
    saved = journey.store.row(f"ATTEMPT#{attempt_id}", "META")
    assert saved["epoch"] == before["epoch"]
    assert saved["definition_json"] == before["definition_json"]
    job = journey.store.row(f"JOB#{saved['job_id']}", "STATE")
    assert (job["adapter_version"], job["projection_version"]) == (
        old_version["adapter_version"], old_version["projection_version"])
    deliver(journey, saved["job_id"])
    assert len(adapter.calls) == 1 and next_adapter.calls == []
    assert h.result(session.token, attempt_id)["calculationStatus"] == "succeeded"
    # Newly started work uses the reassembled definitions.
    fresh_course = dummy_course("mock-compression-only", "child")
    fresh = h.start(session.token, fresh_course, fresh_course.practice_link_id,
                    client_request_id=str(uuid.uuid4()))
    fresh_row = journey.store.row(f"ATTEMPT#{fresh['attemptId']}", "META")
    assert json.loads(fresh_row["definition_json"])["adapter_version"] == "next-adapter"

"""The /api/v2 harness in tests/journey_support.py stays on public, post-/mock/v1 surfaces."""

import inspect
import json

from tests import journey_support
from tests.journey_support import EventLog, JourneyStore, V2Journey, dummy_course


V1_ONLY = ("/mock/v1", "create_attempt", "slot_keys", "extract_bearer", "build_application(",
           "programs_view", "get_created_attempt", ".journey.", "JourneyService")


def test_harness_section_uses_no_v1_only_surface():
    source = "".join(inspect.getsource(item) for item in (
        V2Journey, JourneyStore, EventLog, journey_support.QueueCapture, journey_support.dummy_course,
        journey_support.create_journey_table, journey_support.dynamodb_local_store,
        journey_support.synthetic_measurement, journey_support.measurement_for, journey_support.multipart,
    ))
    assert [name for name in V1_ONLY if name in source] == []


def test_results_survive_restart_and_duplicate_wakes_are_harmless():
    events = EventLog()
    h = V2Journey(JourneyStore.memory(), events=events)
    course = dummy_course("mock-ventilation-only", "adult")
    token = h.login().token
    started = h.start(token, course, course.practice_link_id)
    h.upload(token, started["attemptId"], started["condition"])
    sent = h.relay()
    assert sent and set(sent) == {h.job_id(started["attemptId"])}
    h.queue.messages = h.queue.messages + h.queue.messages  # a redelivered wake
    assert h.deliver() == {"batchItemFailures": []}
    first = h.result(token, started["attemptId"])
    assert first["calculationStatus"] == "succeeded" and first["evaluation"]["program_completed"] is True
    stored = {key: value["Body"] for key, value in h.objects.objects.items()}
    h.restart()
    assert h.result(token, started["attemptId"]) == first
    assert h.work(started["attemptId"]) is True
    assert {key: value["Body"] for key, value in h.objects.objects.items()} == stored
    started_events = [r for r in events.operations() if r["event"] == "calculation_started"]
    assert len(started_events) == 1
    assert json.loads(json.dumps(events.records)) == events.records

"""Legacy (course_binding absent) USER slot helpers shared by the legacy DynamoDB tests.

Used by integration_tests/test_legacy_worker_recovery.py and
integration_tests/test_local_completion_state.py so neither test module imports
the other. Only rows seeded from the captured legacy fixture
(tests/fixtures/legacy_mock_v1_rows/) carry ``slots``; new USER rows are never
assumed to have them.
"""

from integration_tests.worker_journey_support import user_row
from tests.journey_support import encode_item


PREVIOUS_ATTEMPT = "previous-committed-attempt"


def complete_legacy_slot(h, slot_key):
    """Mark one legacy USER slot completed by an earlier attempt, as a committed legacy finalize left it.

    The captured fixture has at most one unfinished attempt per slot, so the
    earlier completion of the same slot is written directly: a raw put
    conditioned on the current USER revision (revision + 1, like any writer).
    The slot keeps its open count because the fixture attempt counted there is
    still active. Returns (completed slot, USER row after the write).
    """
    user = user_row(h)
    slot = {**user["slots"][slot_key], "completed": True, "completed_by_attempt": PREVIOUS_ATTEMPT,
            "completed_at": user["updated_at"]}
    assert slot["open_attempts"] == 1
    changed = {**user, "slots": {**user["slots"], slot_key: slot}, "revision": user["revision"] + 1}
    h.store.client.put_item(TableName=h.store.table, Item=encode_item(changed),
                            ConditionExpression="#r = :r", ExpressionAttributeNames={"#r": "revision"},
                            ExpressionAttributeValues={":r": {"N": str(user["revision"])}})
    assert user_row(h) == changed
    return slot, changed


def other_slots(user, slot_key):
    return {key: value for key, value in user["slots"].items() if key != slot_key}

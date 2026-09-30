"""U0 behavior-preservation baseline for the /mock/v1 removal (in-memory table).

These are characterization baselines, not calculation goldens. They were
captured from HEAD 337cf47 + base_after_phase1.patch (write-tree
80d99bc961e317c02460813ddf10b119378240d0), before /mock/v1 was removed, and the
same scripts must still match after the removal. The integration twin
(integration_tests/test_v2_baseline_dynamodb.py) matches the same fixtures on
DynamoDB Local, which also cross-checks tests/memory_dynamodb.py.

Fixtures (tests/fixtures/v2_baseline/):
  dummy_dev_definitions.json  DummyDevCourseProvider 15 bundles: definitionHash,
                              Catalog.definition() bytes, placement execution bytes.
  flow_responses.json, flow_events.json, flow_rows.json
                              golden_flow(): /api/v2 replies, operational
                              records (order and fields) and every stored row.
  user_slots.json             USER.slots kept apart from the row snapshot. The
                              only approved storage change (design Q10) is that
                              a new USER row has no slots: then set
                              user_slots.json["flow"]["USER#dummy-tester/STATE"]
                              to {"slots_present": false} (applied with the
                              /mock/v1 removal). Legacy entries keep their
                              slots (R1/R3).
  legacy_compat.json          legacy_active_session()/legacy_new_session():
                              /api/v2 on rows seeded from
                              tests/fixtures/legacy_mock_v1_rows (R1-R6).

Capture method (scripts/capture_baselines.py, D125): the scripts in
tests/v2_baseline_support.py ran twice in separate processes on
JourneyStore.memory(), with different PYTHONHASHSEED and with util.uploader's
wall clock shifted by three days in the second run. Values that differed
between the runs (UUIDs, 32/64-hex digests of random or wall-clock input,
token/credential/nonce tails, wall-clock S3 path parts) became placeholders
labelled by first appearance (responses, events, rows, slots, checkpoints);
diagnostic *_ms durations became <ms>. Both runs produced the identical
template before it was written. Row order and label numbering are not part
of the contract: the matcher binds labels one-to-one and matches rows by key,
and ``capture_baselines.py --check`` compares up to them. The legacy rows were
captured through the /mock/v1 HTTP routes on DynamoDB Local (see
tests/legacy_rows_support.py and its meta.json).
"""

import base64
from copy import deepcopy
import json

import pytest

from tests.journey_support import JourneyStore, decode_item
from tests.legacy_rows_support import (
    FIXTURE, legacy_resume_credential, load_legacy_fixture, seed_legacy_rows,
)
from tests.v2_baseline_support import (
    LEGACY_SECTIONS, assert_matches, definition_observation, flow_template, golden_flow, legacy_active_session,
    legacy_new_session, legacy_template, load,
)


DUE_INDEX_FIELDS = {"GSI1PK", "GSI1SK", "next_due_at"}
DEFAULT_SLOT = {"completed": False, "completed_by_attempt": None, "completed_at": None, "open_attempts": 0}


@pytest.fixture(scope="module")
def flow_observation():
    return golden_flow(JourneyStore.memory())


# -- 1. DummyDev definitions ----------------------------------------------------

def test_dummy_dev_definition_hashes_and_execution_bytes_match_baseline():
    observed = definition_observation()
    expected = load("dummy_dev_definitions.json")
    assert observed == expected
    # Independent shape checks, not derived from the stored baseline.
    assert len(observed["courses"]) == 15 == len(observed["catalog_pairs"])
    assert [course["course_id"] for course in observed["courses"]] == list(range(910001, 910016))
    for course in observed["courses"]:
        assert [placement["kind"] for placement in course["placements"]] == ["training", "assessment"]
        definition = json.loads(course["catalog_definition"])
        assert definition["condition"]["target"] == course["target"]
        assert all(json.loads(placement["execution_json"]) == definition for placement in course["placements"])


# -- 2/3. /api/v2 flow: replies, operational records, stored rows ----------------

def test_v2_flow_replies_events_and_rows_match_baseline(flow_observation):
    assert_matches(flow_template(), flow_observation)


def test_v2_flow_event_sequence_is_the_recorded_contract(flow_observation):
    names = [(record["role"], record["event"]) for record in flow_observation["events"]
             if record["category"] == "operation"]
    assert names == [(record["role"], record["event"]) for record in load("flow_events.json")
                     if record["category"] == "operation"]
    # v2 attempt creation and logout record no attempt_created/logout_succeeded today.
    assert ("api", "attempt_created") not in names and ("api", "logout_succeeded") not in names


def test_matcher_rejects_swapped_identifiers_and_changed_literals(flow_observation):
    template = flow_template()
    swapped = deepcopy(flow_observation)
    attempts = [row for row in swapped["rows"] if row["PK"].startswith("ATTEMPT#")]
    first, second = attempts[0]["attempt_id"], attempts[1]["attempt_id"]
    attempts[0]["attempt_id"], attempts[1]["attempt_id"] = second, first
    with pytest.raises(AssertionError):
        assert_matches(template, swapped)
    changed = deepcopy(flow_observation)
    head = next(row for row in changed["rows"] if row["SK"].endswith("#HEAD") and row["PK"].startswith("COURSE#"))
    head["definition_hash"] = "0" * 64
    with pytest.raises(AssertionError, match="definition_hash"):
        assert_matches(template, changed)
    reordered = deepcopy(flow_observation)
    reordered["events"][0], reordered["events"][1] = reordered["events"][1], reordered["events"][0]
    with pytest.raises(AssertionError):
        assert_matches(template, reordered)


def test_user_slots_are_the_only_separately_editable_difference(flow_observation):
    """Q10 applied: only user_slots.json['flow'] changed (a new USER row has no slots).

    Before the removal this simulated the edit from a template with slots. Now
    the template holds the edited value, so the check runs the other way: the
    other sections do not depend on USER.slots, and slots coming back on a new
    USER row are still detected by the user_slots section.
    """
    template = flow_template()
    assert template["user_slots"] == {"USER#dummy-tester/STATE": {"slots_present": False}}
    assert flow_observation["user_slots"] == template["user_slots"]
    with_slots = deepcopy(flow_observation)
    with_slots["user_slots"] = {key: {"slots_present": True, "slots": {"mock-cpr:adult": dict(DEFAULT_SLOT)}}
                                for key in with_slots["user_slots"]}
    assert_matches(template, with_slots, sections=("responses", "events", "rows"))
    with pytest.raises(AssertionError):
        assert_matches(template, with_slots)
    assert_matches(template, flow_observation)


# -- 4. Legacy /mock/v1 rows through /api/v2 (R1-R6) ------------------------------

def _slots(checkpoints, label):
    return next(point["user"] for point in checkpoints if point["label"] == label)


def _legacy_views(observation, attempt_ids):
    views = [reply["body"]["data"] for reply in observation["responses"]
             if reply["status"] == 200 and type(reply["body"]["data"]) is dict
             and reply["body"]["data"].get("attemptId") in attempt_ids and "courseId" in reply["body"]["data"]]
    assert views
    return views


def _assert_seeded_rows_kept(observation):
    """R2: no /mock/v1 field is removed from any seeded row; R5: CREATE receipts are unchanged."""
    _, rows, _ = load_legacy_fixture()
    stored = {(row["PK"], row["SK"]): row for row in observation["rows"]}
    for row in map(decode_item, rows):
        key = (row["PK"], row["SK"])
        current = set(stored[key]) | DUE_INDEX_FIELDS  # a finished job leaves the due index
        if key[0].startswith("USER#"):
            assert observation["user_slots"][f"{key[0]}/{key[1]}"]["slots_present"] is True
            current.add("slots")  # the observation keeps USER.slots apart
        assert set(row) <= current, (key, sorted(set(row) - current))
        if key[1].startswith("CREATE#"):
            assert stored[key] == row


def test_legacy_rows_with_active_legacy_session_match_baseline():
    store = JourneyStore.memory()
    observation = legacy_active_session(store)
    assert_matches(legacy_template("legacy_active_session"), observation, sections=LEGACY_SECTIONS)
    meta, _, _ = load_legacy_fixture()
    attempts = meta["attempts"]
    # R4: legacy attempts are served with course fields null and legacy state.
    for view in _legacy_views(observation, set(attempts.values())):
        assert (view["courseId"], view["enrollmentId"], view["courseItemLinkId"], view["definitionHash"],
                view["role"]) == (None, None, None, None, None)
    seeded, finished = (_slots(observation["checkpoints"], label) for label in ("seeded", "queued_finished"))
    cancelled, logged_out = (_slots(observation["checkpoints"], label) for label in ("created_cancelled", "logged_out"))
    # Legacy finalize applies slot progress; legacy cancel releases its open slot count.
    assert seeded["slots"]["mock-compression-only:adult"] == {**DEFAULT_SLOT, "open_attempts": 1}
    assert finished["slots"]["mock-compression-only:adult"] == {
        "completed": True, "completed_by_attempt": attempts["queued"], "completed_at": finished["updated_at"],
        "open_attempts": 0}
    assert seeded["slots"]["mock-cpr:adult"]["open_attempts"] == 1
    assert cancelled["slots"]["mock-cpr:adult"] == DEFAULT_SLOT
    # R3: logout resets the slots of a row that has them, with a new epoch.
    assert logged_out["epoch"] != seeded["epoch"]
    assert set(logged_out["slots"]) == set(seeded["slots"])
    assert all(slot == DEFAULT_SLOT for slot in logged_out["slots"].values())


def test_legacy_rows_after_legacy_session_expiry_match_baseline():
    store = JourneyStore.memory()
    observation = legacy_new_session(store)
    assert_matches(legacy_template("legacy_new_session"), observation, sections=LEGACY_SECTIONS)
    meta, _, _ = load_legacy_fixture()
    seeded, login = (_slots(observation["checkpoints"], label) for label in ("seeded", "v2_login"))
    # R1: a /api/v2 login keeps the existing USER row, slots included.
    assert login == seeded
    finished = _slots(observation["checkpoints"], "queued_finished")
    assert finished["slots"]["mock-compression-only:adult"]["completed_by_attempt"] == meta["attempts"]["queued"]
    reauthorized = [reply for reply in observation["responses"] if reply["path"].endswith("/reauthorize/")]
    assert [reply["status"] for reply in reauthorized] == [200, 200, 200]
    assert all(reply["body"]["data"]["courseId"] is None for reply in reauthorized)


@pytest.mark.parametrize("script", [legacy_active_session, legacy_new_session])
def test_seeded_legacy_rows_are_kept_field_for_field(script):
    _assert_seeded_rows_kept(script(JourneyStore.memory()))


def test_legacy_fixture_is_complete_and_holds_no_usable_secret():
    meta, rows, objects = load_legacy_fixture()
    seeded = seed_legacy_rows(JourneyStore.memory())
    kinds = {}
    for (pk, sk) in seeded.rows:
        kind = pk.split("#", 1)[0] + "/" + sk.split("#", 1)[0]
        kinds[kind] = kinds.get(kind, 0) + 1
    assert kinds == meta["row_counts"] == {"ATTEMPT/META": 3, "JOB/STATE": 2, "OUTBOX/DISPATCH": 2,
                                           "SESSION/AUTH": 1, "SESSION/CREATE": 3, "USER/STATE": 1}
    assert {label: seeded.attempts[label]["row"]["state"] for label in meta["attempts"]} == {
        "created": "created", "queued": "queued", "evaluated": "evaluated"}
    user = seeded.rows[("USER#dummy-tester", "STATE")]
    assert type(user["slots"]) is dict and len(user["slots"]) == 15
    queued_outbox = seeded.rows[(f'OUTBOX#{meta["jobs"]["queued"]}', "DISPATCH")]
    assert queued_outbox["state"] == "pending" and queued_outbox["GSI1PK"] == "DUE#OUTBOX"
    keys = {entry["key"] for entry in objects}
    for label in ("queued", "evaluated"):
        job = seeded.rows[(f'JOB#{meta["jobs"][label]}', "STATE")]
        assert job["input_manifest_ref"]["key"] in keys
        manifest = json.loads(next(base64.b64decode(entry["body"]) for entry in objects
                                   if entry["key"] == job["input_manifest_ref"]["key"]))
        assert manifest["raw_base"] + ".bin" in keys and manifest["raw_base"] + ".meta.json" in keys
    evaluated = seeded.rows[(f'JOB#{meta["jobs"]["evaluated"]}', "STATE")]
    assert evaluated["final_ref"]["key"] in keys and evaluated["chart_publication"]["key"] in keys
    for label in meta["attempts"]:
        legacy_resume_credential(seeded, label)
    raw = "".join(path.read_text() for path in FIXTURE.iterdir())
    assert "s1." not in raw and "r1.v1." not in raw
    assert "journey-harness-synthetic" not in raw


def test_operational_log_vocabulary_is_not_reduced():
    """R6: events and enum values used by /mock/v1-era records stay accepted."""
    from services.operational_logs import EVENTS, _ENUM_FIELDS
    assert EVENTS >= {
        "login_succeeded", "login_failed", "logout_succeeded", "progress_reset",
        "attempt_created", "attempt_create_replayed", "attempt_cancelled", "attempt_reauthorized",
        "calculation_accepted", "calculation_replayed", "calculation_started",
        "calculation_completed", "calculation_failed", "calculation_deferred", "progress_application",
        "request_rejected", "relay_reconciled",
    }
    assert _ENUM_FIELDS["program_id"] >= {"mock-cpr", "mock-compression-only", "mock-ventilation-only",
                                          "mock-two-rescuer-cpr", "mock-two-rescuer-aed"}
    assert _ENUM_FIELDS["target"] >= {"adult", "child", "infant"}
    assert _ENUM_FIELDS["reason"] >= {"user_stopped", "manikin_disconnected", "APPLIED", "PROGRESS_RESET",
                                      "REQUIREMENTS_NOT_MET", "ALREADY_COMPLETED", "GOAL_POLICY_UNRESOLVED"}

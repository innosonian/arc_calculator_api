"""D138/D139 journeys on the public /api/v2 harness (memory in tests/, DynamoDB Local in integration_tests/).

Inputs are real recordings:

* ``tests/dataset/cpr_eof_truncated_vent_1.bin`` -- the app tester's adult CPR
  recording of 2026-09-30 (61 compressions in three cycles of 20/21/20, six
  ventilations). The app stopped the moment it detected the sixth breath, so
  the file ends one packet after that breath's peak (480 mL -> 380 mL).
* ``tests/dataset/vo_1.bin`` cut one packet after the eighth breath's peak --
  the same auto-stop shape for the Ventilation Only goal of 8.

* a synthetic three-cycle session (30 compressions and 2 ventilations per
  cycle) cut one packet after the sixth breath's peak -- the same auto-stop
  shape for a session that meets the ARC minimum quantity (90 / 6).

Expected values follow docs/DECISIONS.md D138 (a breath cut at the end of the
file counts once its descent was observed), D139 (below the ARC minimum
quantity the scores are shown, not nulled, but the session does not pass:
decision "fail" with MINIMUM_QUANTITY_NOT_MET) and D136 (completion = goal met
AND pass).
"""

from pathlib import Path

from mock_journey.contracts import CURRENT_ADAPTER_VERSION, CYCLE_GOAL_ADAPTER_VERSION, CYCLE_GOAL_PROFILE_VERSION
from tests._synth import cpr_session


DATASET = Path(__file__).parent / "dataset"
TESTER_RECORDING = (DATASET / "cpr_eof_truncated_vent_1.bin").read_bytes()
TESTER_SHA256 = "f6b57655674d47757872e27586048b842af3dbf6fa82e31f967925b385951b86"
PACKET_BYTES = 28
# vo_1.bin (infant): the eighth breath rises from packet 321, peaks at 41 mL and
# its first low packet is 345 (41 -> 0). An app that stops on detecting the
# eighth breath records nothing after that packet.
EIGHTH_BREATH_FIRST_LOW_PACKET = 345
VO_STOPPED_AT_EIGHTH_BREATH = (DATASET / "vo_1.bin").read_bytes()[:(EIGHTH_BREATH_FIRST_LOW_PACKET + 1) * PACKET_BYTES]
# Three 30:2 cycles whose sixth breath ends peak, one low packet (the generator's
# breath ends peak, 0, 0; one packet is dropped). 90 compressions; the sixth
# ventilation exists only under D138, and with it the ARC minimum (90 / 6) is met.
CPR_STOPPED_AT_SIXTH_BREATH = cpr_session([(30, 2)] * 3)[:-PACKET_BYTES]


def v4_execution():
    """The 15 definitions as the previous deployment (arc-internal-detection-v4) issued them."""
    from mock_journey.assembly import ExecutionCatalog
    from mock_journey.catalog import PROGRAMS, TARGETS, definition_key
    from mock_journey.execution_definitions import execution_catalog

    current = execution_catalog()
    definitions = {}
    for program, *_ in PROGRAMS:
        for target in TARGETS:
            value = current.get_definition(program, target)
            value.update(adapter_version=CYCLE_GOAL_ADAPTER_VERSION, profile_version=CYCLE_GOAL_PROFILE_VERSION)
            definitions[definition_key(program, target)] = value
    return ExecutionCatalog(definitions, current.schemas)


def _adapter_of(h, attempt_id):
    import json
    return json.loads(h.store.row(f"ATTEMPT#{attempt_id}", "META")["definition_json"])["adapter_version"]


def _run(h, token, course, link_id, data):
    attempt = h.start(token, course, link_id)
    h.upload(token, attempt["attemptId"], attempt["condition"], data=data)
    assert h.work(attempt["attemptId"]) is True
    return attempt["attemptId"], h.result(token, attempt["attemptId"])


def _item(h, token, course, link_id):
    return next(row for row in h.course(token, course)["courseItems"] if row["courseItemLinkId"] == link_id)


def recorded_cpr_below_minimum_journey(store):
    """The tester's recording is scored but does not pass (61 compressions < 90); a 90/6 session completes."""
    from tests.journey_support import V2Journey, dummy_course

    h = V2Journey(store)
    course = dummy_course("mock-cpr", "adult")
    token = h.login().token
    attempt_id, result = _run(h, token, course, course.practice_link_id, TESTER_RECORDING)
    assert _adapter_of(h, attempt_id) == CURRENT_ADAPTER_VERSION == "arc-internal-detection-v5"
    calculation = result["calculation"]
    # D138: the sixth breath (cut one packet after its peak) is counted.
    assert calculation["action_count"] == {"comp": 61, "vent": 6}
    total = calculation["cpr_score"]["total_score"]
    # D139: 61 compressions are below the ARC minimum of 90. The chest and
    # ventilation groups are scored and shown (overall 83 >= 80) ...
    assert total["overall"] == 83
    assert (total["score_comp_depth"], total["score_recoil"], total["score_hand_position"],
            total["score_comp_no"], total["score_comp_count"]) == (99, 35, 81, 0, 0)
    assert (total["score_vent_vol"], total["score_vent_count"], total["score_vent_rate"]) == (100, 100, 0)
    # ... but the session does not pass: the minimum is the only reason.
    assert result["evaluation"] == {
        "goal": {"kind": "cycles", "required": 3, "observed": 3, "met": True, "status": "evaluated"},
        "score": {"decision": "fail"}, "program_completed": False, "reason_codes": ["MINIMUM_QUANTITY_NOT_MET"],
    }
    assert result["progressApplication"] == {"applied": False, "applied_epoch": None, "reason": "REQUIREMENTS_NOT_MET"}
    item = _item(h, token, course, course.practice_link_id)
    assert item["isCompleted"] is False and item["isPassed"] is False
    assert h.start(token, course, course.final_link_id, expected=409)["code"] == "PREREQUISITES_NOT_COMPLETED"

    # A session that meets the minimum (90 compressions, 6 ventilations; the sixth breath
    # is cut one packet after its peak and counts by D138) passes and completes the practice.
    attempt_id, result = _run(h, token, course, course.practice_link_id, CPR_STOPPED_AT_SIXTH_BREATH)
    assert result["calculation"]["action_count"] == {"comp": 90, "vent": 6}
    assert result["calculation"]["cpr_score"]["total_score"]["overall"] == 99
    assert result["evaluation"] == {
        "goal": {"kind": "cycles", "required": 3, "observed": 3, "met": True, "status": "evaluated"},
        "score": {"decision": "pass"}, "program_completed": True, "reason_codes": [],
    }
    assert result["progressApplication"]["reason"] == "APPLIED"
    item = _item(h, token, course, course.practice_link_id)
    assert item["isCompleted"] is True and item["isPassed"] is True
    return h


def auto_stopped_ventilation_only_journey(store):
    """A Ventilation Only recording that ends one packet after the eighth breath's peak meets the goal of 8."""
    from tests.journey_support import V2Journey, dummy_course

    h = V2Journey(store)
    course = dummy_course("mock-ventilation-only", "infant")
    token = h.login().token
    attempt_id, result = _run(h, token, course, course.practice_link_id, VO_STOPPED_AT_EIGHTH_BREATH)
    assert _adapter_of(h, attempt_id) == CURRENT_ADAPTER_VERSION
    assert result["calculation"]["action_count"] == {"comp": 0, "vent": 8}
    assert result["calculation"]["cpr_score"]["total_score"]["overall"] == 92
    assert result["evaluation"] == {
        "goal": {"kind": "ventilations", "required": 8, "observed": 8, "met": True, "status": "evaluated"},
        "score": {"decision": "pass"}, "program_completed": True, "reason_codes": [],
    }
    item = _item(h, token, course, course.practice_link_id)
    assert item["isCompleted"] is True and item["isPassed"] is True
    return h


def in_flight_v4_attempts_keep_their_meaning_after_the_upgrade(store):
    """Attempts accepted under v4 definitions finish with the v4 rules on the upgraded Worker; new attempts use v5."""
    from tests.journey_support import V2Journey, dummy_course

    before = V2Journey(store, execution=v4_execution())
    cpr, vo = dummy_course("mock-cpr", "adult"), dummy_course("mock-ventilation-only", "infant")
    token = before.login().token
    old_hash = before.course(token, cpr)["definitionHash"]
    accepted = {}
    for course, data in ((cpr, TESTER_RECORDING), (vo, VO_STOPPED_AT_EIGHTH_BREATH)):
        attempt = before.start(token, course, course.practice_link_id)
        before.upload(token, attempt["attemptId"], attempt["condition"], data=data)
        assert _adapter_of(before, attempt["attemptId"]) == "arc-internal-detection-v4"
        accepted[course.program_id] = attempt["attemptId"]

    # The upgrade: the same table and objects, the current catalog and the whole adapter registry.
    after = V2Journey(store, objects=before.objects, bindings=before.bindings)
    for attempt_id in accepted.values():
        assert after.work(attempt_id) is True

    result = after.result(token, accepted["mock-cpr"])
    # D42 (two confirmation packets) and D07/D08 (minimum-quantity null) still apply to this attempt.
    assert result["calculation"]["action_count"] == {"comp": 61, "vent": 5}
    total = result["calculation"]["cpr_score"]["total_score"]
    assert total["overall"] is None and total["score_comp_depth"] is None and total["score_vent_vol"] is None
    assert result["evaluation"] == {
        "goal": {"kind": "cycles", "required": 3, "observed": 3, "met": True, "status": "evaluated"},
        "score": {"decision": "fail"}, "program_completed": False, "reason_codes": ["SCORE_NOT_PASS"],
    }
    assert _item(after, token, cpr, cpr.practice_link_id)["isCompleted"] is False

    result = after.result(token, accepted["mock-ventilation-only"])
    assert result["calculation"]["action_count"] == {"comp": 0, "vent": 7}
    assert result["evaluation"] == {
        "goal": {"kind": "ventilations", "required": 8, "observed": 7, "met": False, "status": "evaluated"},
        "score": {"decision": "pass"}, "program_completed": False, "reason_codes": ["GOAL_NOT_MET"],
    }
    assert _item(after, token, vo, vo.practice_link_id)["isCompleted"] is False

    # A session keeps its stored course definitions until its next refresh (or a new login);
    # then the definition hash changes (D136/D138) and the old one is refused.
    assert after.course(token, cpr)["definitionHash"] == old_hash
    after.refresh(token)
    assert after.course(token, cpr)["definitionHash"] != old_hash
    assert after.start(token, cpr, cpr.practice_link_id, definition_hash=old_hash,
                       expected=409)["code"] == "DEFINITION_CHANGED"

    # The same recordings in new attempts are calculated by v5. The CPR recording now shows
    # its scores (83) and the sixth breath, and fails only by the ARC minimum (61 < 90).
    attempt_id, result = _run(after, token, cpr, cpr.practice_link_id, TESTER_RECORDING)
    assert _adapter_of(after, attempt_id) == "arc-internal-detection-v5"
    assert result["calculation"]["action_count"] == {"comp": 61, "vent": 6}
    assert result["calculation"]["cpr_score"]["total_score"]["overall"] == 83
    assert result["evaluation"]["score"] == {"decision": "fail"}
    assert result["evaluation"]["program_completed"] is False
    assert result["evaluation"]["reason_codes"] == ["MINIMUM_QUANTITY_NOT_MET"]
    assert _item(after, token, cpr, cpr.practice_link_id)["isCompleted"] is False
    # The Ventilation Only practice (no CPR minimum) completes with its eighth breath.
    attempt_id, result = _run(after, token, vo, vo.practice_link_id, VO_STOPPED_AT_EIGHTH_BREATH)
    assert result["evaluation"]["goal"]["observed"] == 8 and result["evaluation"]["program_completed"] is True
    assert result["evaluation"]["reason_codes"] == []
    return after

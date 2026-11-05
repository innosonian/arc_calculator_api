"""Recorded end-of-file inputs through current /api/v2 and retained job refusal.

Calculator assertions come from the guarded hstm 92f56dc legacy path on these
same bytes. The tester recording has 60 server compressions, six ventilations
and overall 82; local firmware counts are not the server's count authority.
"""

import json
from pathlib import Path

from calculators.gate_evaluator import valid_gate_results
from mock_journey.contracts import CURRENT_ADAPTER_VERSION, CYCLE_GOAL_ADAPTER_VERSION
from tests._synth import cpr_session


DATASET = Path(__file__).parent / "dataset"
TESTER_RECORDING = (DATASET / "cpr_eof_truncated_vent_1.bin").read_bytes()
TESTER_SHA256 = "f6b57655674d47757872e27586048b842af3dbf6fa82e31f967925b385951b86"
PACKET_BYTES = 28
EIGHTH_BREATH_FIRST_LOW_PACKET = 345
VO_STOPPED_AT_EIGHTH_BREATH = (DATASET / "vo_1.bin").read_bytes()[:(EIGHTH_BREATH_FIRST_LOW_PACKET + 1) * PACKET_BYTES]
CPR_STOPPED_AT_SIXTH_BREATH = cpr_session([(30, 2)] * 3)[:-PACKET_BYTES]


# Captured directly from hstm_v2 92f56dc with explicit usa prompt region.
REFERENCE_TOTALS = {'tester': {'score_comp_depth': 99,
            'score_comp_rate': 94,
            'score_comp_no': 0,
            'score_comp_count': 0,
            'score_recoil': 35,
            'score_hand_position': 55,
            'score_vent_vol': 100,
            'score_vent_rate': 0,
            'score_vent_count': 100,
            'score_vent_speed': None,
            'score_ccf': 100,
            'score_rescue_vent': 0,
            'overall': 82,
            'judg_result': 'N/A'},
 'vo': {'score_comp_depth': None,
        'score_comp_rate': None,
        'score_comp_no': None,
        'score_comp_count': None,
        'score_recoil': None,
        'score_hand_position': None,
        'score_vent_vol': 87,
        'score_vent_rate': 93,
        'score_vent_count': None,
        'score_vent_speed': 100,
        'score_ccf': None,
        'score_rescue_vent': 0,
        'overall': 91,
        'judg_result': 'N/A'},
 'full_cpr': {'score_comp_depth': 100,
              'score_comp_rate': 100,
              'score_comp_no': 100,
              'score_comp_count': 100,
              'score_recoil': 100,
              'score_hand_position': 100,
              'score_vent_vol': 100,
              'score_vent_rate': 0,
              'score_vent_count': 100,
              'score_vent_speed': None,
              'score_ccf': 100,
              'score_rescue_vent': 0,
              'overall': 100,
              'judg_result': 'N/A'}}

def v4_execution():
    """Definitions accepted before the current adapter became available."""
    from tests.legacy_dummy_catalog import legacy_execution_catalog
    return legacy_execution_catalog(CYCLE_GOAL_ADAPTER_VERSION)


def _definition(h, attempt_id):
    return json.loads(h.store.row(f"ATTEMPT#{attempt_id}", "META")["definition_json"])


def _run(h, token, course, link_id, data):
    attempt = h.start(token, course, link_id)
    h.upload(token, attempt["attemptId"], attempt["condition"], data=data)
    assert h.work(attempt["attemptId"]) is True
    assert _definition(h, attempt["attemptId"])["adapter_version"] == CURRENT_ADAPTER_VERSION
    return attempt["attemptId"], h.result(token, attempt["attemptId"])


def _item(h, token, course, link_id):
    return next(row for row in h.course(token, course)["courseItems"] if row["courseItemLinkId"] == link_id)


def _evaluation(h, attempt_id, result):
    assessment = result["evaluation"]
    assert valid_gate_results(assessment["gates"], _definition(h, attempt_id), assessment["goal"]["observed"])
    return {key: value for key, value in assessment.items() if key != "gates"}


def recorded_cpr_below_minimum_journey(store):
    """The recorded 60/6 session fails the quantity gate; a 90/6 session completes."""
    from tests.journey_support import V2Journey, dummy_course

    h = V2Journey(store)
    course = dummy_course("mock-cpr", "adult")
    token = h.login().token
    attempt_id, result = _run(h, token, course, course.practice_link_id, TESTER_RECORDING)
    assert result["calculation"]["action_count"] == {"comp": 60, "vent": 6}
    assert result["calculation"]["cpr_score"]["total_score"] == REFERENCE_TOTALS["tester"]
    assert _evaluation(h, attempt_id, result) == {
        "goal": {"kind": "cycles", "required": 3, "observed": 3, "met": True, "status": "evaluated"},
        "score": {"decision": "fail"}, "program_completed": False, "reason_codes": ["MINIMUM_QUANTITY_NOT_MET"],
    }
    assert result["progressApplication"] == {"applied": False, "applied_epoch": None, "reason": "REQUIREMENTS_NOT_MET"}
    item = _item(h, token, course, course.practice_link_id)
    assert item["isCompleted"] is False and item["isPassed"] is False
    assert h.start(token, course, course.final_link_id, expected=409)["code"] == "PREREQUISITES_NOT_COMPLETED"

    attempt_id, result = _run(h, token, course, course.practice_link_id, CPR_STOPPED_AT_SIXTH_BREATH)
    assert result["calculation"]["action_count"] == {"comp": 90, "vent": 6}
    assert result["calculation"]["cpr_score"]["total_score"] == REFERENCE_TOTALS["full_cpr"]
    assert _evaluation(h, attempt_id, result) == {
        "goal": {"kind": "cycles", "required": 3, "observed": 3, "met": True, "status": "evaluated"},
        "score": {"decision": "pass"}, "program_completed": True, "reason_codes": [],
    }
    assert result["progressApplication"]["reason"] == "APPLIED"
    item = _item(h, token, course, course.practice_link_id)
    assert item["isCompleted"] is True and item["isPassed"] is True
    return h


def auto_stopped_ventilation_only_journey(store):
    """The infant recording's eighth descending breath reaches the legacy practice goal."""
    from tests.journey_support import V2Journey, dummy_course

    h = V2Journey(store)
    course = dummy_course("mock-ventilation-only", "infant")
    token = h.login().token
    attempt_id, result = _run(h, token, course, course.practice_link_id, VO_STOPPED_AT_EIGHTH_BREATH)
    assert result["calculation"]["action_count"] == {"comp": 0, "vent": 8}
    assert result["calculation"]["cpr_score"]["total_score"] == REFERENCE_TOTALS["vo"]
    assert _evaluation(h, attempt_id, result) == {
        "goal": {"kind": "ventilations", "required": 8, "observed": 8, "met": True, "status": "evaluated"},
        "score": {"decision": "pass"}, "program_completed": True, "reason_codes": [],
    }
    assert _definition(h, attempt_id)["minimumQuantity"] == {"compressions": 0, "ventilations": 8}
    item = _item(h, token, course, course.practice_link_id)
    assert item["isCompleted"] is True and item["isPassed"] is True
    return h


def in_flight_v4_attempts_require_new_attempts_after_upgrade(store):
    """Uncalculated v4 jobs defer without a candidate; refreshed attempts use v6."""
    from tests.journey_support import V2Journey, dummy_course

    before = V2Journey(store, execution=v4_execution())
    cpr, vo = dummy_course("mock-cpr", "adult"), dummy_course("mock-ventilation-only", "infant")
    token = before.login().token
    old_hash = before.course(token, cpr)["definitionHash"]
    accepted = {}
    for course, data in ((cpr, TESTER_RECORDING), (vo, VO_STOPPED_AT_EIGHTH_BREATH)):
        attempt = before.start(token, course, course.practice_link_id)
        before.upload(token, attempt["attemptId"], attempt["condition"], data=data)
        assert _definition(before, attempt["attemptId"])["adapter_version"] == CYCLE_GOAL_ADAPTER_VERSION
        accepted[course.program_id] = attempt["attemptId"]

    after = V2Journey(store, objects=before.objects, bindings=before.bindings)
    objects_before = set(after.objects.objects)
    for attempt_id in accepted.values():
        assert after.work(attempt_id) is False
        attempt = store.row(f"ATTEMPT#{attempt_id}", "META")
        job = store.row(f"JOB#{attempt['job_id']}", "STATE")
        assert job["state"] == "running" and job["error_code"] == "TEMPORARILY_UNAVAILABLE"
        assert job["call_phase"] == "not_started" and job.get("candidate_ref") is None
        assert job["owner"] is None and job["lease_until"] == 0
        assert job["next_due_at"] > after.clock()
        assert attempt["state"] == "outcome_unknown" and attempt["evaluation"] is None
        assert attempt["progress_application"] is None and attempt.get("terminal_seal") is None
        assert after.result(token, attempt_id, expected=503)["code"] == "CALCULATION_OUTCOME_UNKNOWN"
    assert set(after.objects.objects) == objects_before
    assert _item(after, token, cpr, cpr.practice_link_id)["isCompleted"] is False
    assert _item(after, token, vo, vo.practice_link_id)["isCompleted"] is False

    assert after.course(token, cpr)["definitionHash"] == old_hash
    after.refresh(token)
    assert after.course(token, cpr)["definitionHash"] != old_hash
    assert after.start(token, cpr, cpr.practice_link_id, definition_hash=old_hash,
                       expected=409)["code"] == "DEFINITION_CHANGED"
    attempt_id, result = _run(after, token, cpr, cpr.practice_link_id, TESTER_RECORDING)
    assert result["calculation"]["action_count"] == {"comp": 60, "vent": 6}
    assert result["calculation"]["cpr_score"]["total_score"] == REFERENCE_TOTALS["tester"]
    assert result["evaluation"]["reason_codes"] == ["MINIMUM_QUANTITY_NOT_MET"]
    assert result["evaluation"]["program_completed"] is False
    _, result = _run(after, token, vo, vo.practice_link_id, VO_STOPPED_AT_EIGHTH_BREATH)
    assert result["evaluation"]["goal"]["observed"] == 8
    assert result["evaluation"]["program_completed"] is True
    assert result["evaluation"]["reason_codes"] == []
    return after

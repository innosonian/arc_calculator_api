"""D139: below the ARC minimum quantity the scores are shown, but the session does not pass.

The rule (arc-internal-detection-v5 evaluations only):

* Minimum (D07): ARC2020/ARC2025 CPR programs (two-rescuer ones included) need
  adult/child 90 and infant 45 compressions AND 6 ventilations. Other
  guidelines, Compression Only and Ventilation Only have no such minimum.
* If either group is below its minimum, ``score.decision`` is "fail" whatever
  the displayed overall is, so ``program_completed`` is false.
* ``reason_codes`` order: goal reason, ``MINIMUM_QUANTITY_NOT_MET``,
  ``SCORE_NOT_PASS``. ``SCORE_NOT_PASS`` appears only when the unchanged
  tester rule itself fails; "pass" carries neither code.
* The retained adapters (v4, pending-v3, pending-v2) and unversioned
  definitions are untouched: no gate, no new code (their null policy already
  keeps such a session from passing).

Thresholds and historical score/count facts below are written out by hand.
Retained v5 evaluations remain readable; no test executes its retired core.
"""

import json

import pytest

from tests.legacy_dummy_catalog import LEGACY_PROGRAMS as PROGRAMS
from mock_journey.contracts import VerifiedCalculation, expected_profile_version, minimum_quantity_policy
from mock_journey.errors import JourneyError
from mock_journey.jobs import DynamoJobRepository
from mock_journey.worker import evaluate


V5, V4 = "arc-internal-detection-v5", "arc-internal-detection-v4"
PENDING_V3, PENDING_V2 = "arc-internal-detection-pending-v3", "arc-local-calculator-pending-v2"
G, M, F = "GOAL_NOT_MET", "MINIMUM_QUANTITY_NOT_MET", "SCORE_NOT_PASS"
PROJECTION = "minimum-gate-test-projection-v1"
REQUIRED = {row[0]: (row[2], row[3]) for row in PROGRAMS}


def _condition(target="adult", training="cpr", guideline="ARC2025", two_rescuers=False):
    return {"mode": "training", "target": target, "training_type": training, "guideline": guideline,
            "cpr_cycle_type": "152" if target == "infant" else "302", "is_2rescuers": two_rescuers}


def _definition(adapter=V5, *, kind="cycles", required=3, **condition):
    definition = {"condition": _condition(**condition), "calculation_profile": {},
                  "goal": {"kind": kind, "required": required}, "catalog_version": "mock-catalog-v1",
                  "adapter_version": adapter, "projection_version": PROJECTION}
    profile = expected_profile_version(adapter)
    if profile is not None:
        definition["profile_version"] = profile
    return definition


def _assess(definition, comp, vent, *, overall=90, observed=None):
    """worker.evaluate on a verified result with these whole-session counts and displayed overall."""
    goal = definition["goal"]
    adapter = definition["adapter_version"]
    pending = adapter in (PENDING_V3, PENDING_V2) and goal["kind"] == "cycles"
    status = "pending_policy" if pending else "evaluated" if expected_profile_version(adapter) else None
    verified = VerifiedCalculation({"action_count": {"comp": comp, "vent": vent}}, goal["kind"],
                                   None if pending else goal["required"] if observed is None else observed,
                                   "no_chart", goal_status=status)
    assessment = evaluate({"cpr_score": {"total_score": {"overall": overall}}}, definition, verified)
    # Whatever evaluate produces is what the repository accepts for the same definition.
    assert DynamoJobRepository.check_evaluation(assessment, {"definition_json": json.dumps(definition)}) == assessment
    return assessment


def _outcome(assessment):
    return assessment["score"]["decision"], assessment["program_completed"], assessment["reason_codes"]


# ---------------------------------------------------------------------------
# 1. The rule in worker.evaluate
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("guideline", ["ARC2020", "ARC2025"])
@pytest.mark.parametrize("target,minimum", [("adult", 90), ("child", 90), ("infant", 45)])
def test_both_groups_must_reach_the_minimum_to_pass(target, minimum, guideline):
    definition = _definition(target=target, guideline=guideline)
    # At the minimum and above: the displayed score decides alone.
    for comp, vent in ((minimum, 6), (minimum + 1, 7), (300, 20)):
        assert _outcome(_assess(definition, comp, vent)) == ("pass", True, [])
        assert _outcome(_assess(definition, comp, vent, overall=79)) == ("fail", False, [F])
    # One compression or one ventilation short, or both: not a pass even at overall 90 or 100.
    for comp, vent in ((minimum - 1, 6), (minimum, 5), (minimum - 1, 5), (0, 6), (minimum, 0), (0, 0)):
        for overall in (80, 90, 100):
            assert _outcome(_assess(definition, comp, vent, overall=overall)) == ("fail", False, [M])
        for overall in (79, 0, None):
            assert _outcome(_assess(definition, comp, vent, overall=overall)) == ("fail", False, [M, F])


def test_adult_and_infant_minimums_differ_only_in_compressions():
    # 45..89 compressions with 6 ventilations: enough for an infant, not for an adult or a child.
    for comp in (45, 60, 89):
        assert _outcome(_assess(_definition(target="infant"), comp, 6)) == ("pass", True, [])
        for target in ("adult", "child"):
            assert _outcome(_assess(_definition(target=target), comp, 6)) == ("fail", False, [M])


def test_reason_order_is_goal_then_minimum_then_score():
    definition = _definition()
    assert _outcome(_assess(definition, 89, 6, observed=2)) == ("fail", False, [G, M])
    assert _outcome(_assess(definition, 89, 6, observed=2, overall=50)) == ("fail", False, [G, M, F])
    assert _outcome(_assess(definition, 90, 6, observed=2)) == ("pass", False, [G])
    assert _outcome(_assess(definition, 90, 6, observed=2, overall=50)) == ("fail", False, [G, F])
    # "pass" never carries the minimum or the score code.
    for comp, vent, overall, observed in ((90, 6, 90, 3), (90, 6, 90, 2), (200, 12, 80, 5)):
        reasons = _assess(definition, comp, vent, overall=overall, observed=observed)["reason_codes"]
        assert M not in reasons and F not in reasons


@pytest.mark.parametrize("program", ["mock-cpr", "mock-two-rescuer-cpr", "mock-two-rescuer-aed"])
def test_every_cpr_program_is_gated_the_same_way(program):
    kind, required = REQUIRED[program]
    definition = _definition(kind=kind, required=required, two_rescuers=program != "mock-cpr")
    assert _outcome(_assess(definition, 90, 6)) == ("pass", True, [])
    assert _outcome(_assess(definition, 89, 6)) == ("fail", False, [M])
    assert _outcome(_assess(definition, 90, 5)) == ("fail", False, [M])


@pytest.mark.parametrize("target", ["adult", "child", "infant"])
@pytest.mark.parametrize("guideline", ["AHA2020", "ERC2020", "STD2015"])
def test_other_guidelines_have_no_minimum_gate(guideline, target):
    definition = _definition(target=target, guideline=guideline)
    for comp, vent in ((0, 0), (30, 2), (44, 5), (89, 5)):
        assert _outcome(_assess(definition, comp, vent)) == ("pass", True, [])
        assert _outcome(_assess(definition, comp, vent, overall=79)) == ("fail", False, [F])


@pytest.mark.parametrize("target", ["adult", "child", "infant"])
@pytest.mark.parametrize("kind,training,required", [("compressions", "compression_only", 60),
                                                    ("ventilations", "ventilation_only", 8)])
def test_single_skill_programs_have_no_cpr_minimum(kind, training, required, target):
    definition = _definition(kind=kind, required=required, target=target, training=training)
    # A Compression Only session has no ventilation and fewer than 90 compressions; it still passes.
    comp, vent = (60, 0) if kind == "compressions" else (0, 8)
    assert _outcome(_assess(definition, comp, vent)) == ("pass", True, [])
    assert _outcome(_assess(definition, comp, vent, overall=79)) == ("fail", False, [F])


@pytest.mark.parametrize("adapter", [V4, PENDING_V3, PENDING_V2, "retained-v1"])
def test_older_adapters_have_no_gate_and_no_new_code(adapter):
    definition = _definition(adapter)
    pending = adapter in (PENDING_V3, PENDING_V2)
    goal_reasons = ["GOAL_POLICY_UNRESOLVED"] if pending else []
    for comp, vent in ((90, 6), (89, 6), (90, 5), (0, 0)):
        # Exactly the previous formula: the score decision alone, whatever the counts are.
        assert _outcome(_assess(definition, comp, vent)) == ("pass", not pending, goal_reasons)
        assert _outcome(_assess(definition, comp, vent, overall=79)) == ("fail", False, goal_reasons + [F])
    # They never read the counts at all (a core without them is still evaluated).
    status = "pending_policy" if pending else "evaluated" if expected_profile_version(adapter) else None
    bare = VerifiedCalculation({}, "cycles", None if pending else 3, "no_chart", goal_status=status)
    assert evaluate({"cpr_score": {"total_score": {"overall": 90}}}, definition, bare)["score"] == {"decision": "pass"}


@pytest.mark.parametrize("core", [
    {}, {"action_count": None}, {"action_count": {"comp": 90}}, {"action_count": {"comp": 90, "vent": 6, "x": 1}},
    {"action_count": {"comp": True, "vent": 6}}, {"action_count": {"comp": 90.0, "vent": 6}},
    {"action_count": {"comp": -1, "vent": 6}}, {"action_count": {"comp": "90", "vent": 6}},
])
def test_saved_v5_never_passes_a_result_whose_counts_cannot_be_read(core):
    verified = VerifiedCalculation(core, "cycles", 3, "no_chart", goal_status="evaluated")
    with pytest.raises(JourneyError) as error:
        evaluate({"cpr_score": {"total_score": {"overall": 100}}}, _definition(), verified)
    assert error.value.code == "CALCULATOR_CONTRACT_MISMATCH"


@pytest.mark.parametrize("damage", [
    lambda c: c.pop("guideline"), lambda c: c.pop("target"), lambda c: c.update(target="teen"),
    lambda c: c.update(guideline="ARC1999"),
])
def test_saved_v5_never_passes_under_a_condition_it_cannot_configure(damage):
    definition = _definition()
    damage(definition["condition"])
    verified = VerifiedCalculation({"action_count": {"comp": 300, "vent": 20}}, "cycles", 3, "no_chart",
                                   goal_status="evaluated")
    with pytest.raises((JourneyError, KeyError)) as error:
        evaluate({"cpr_score": {"total_score": {"overall": 100}}}, definition, verified)
    if isinstance(error.value, JourneyError):
        assert error.value.code == "CALCULATOR_CONTRACT_MISMATCH"


@pytest.mark.parametrize("target,training,guideline,comp,vent,active", [
    ("adult", "cpr", "ARC2025", 89, 6, True),
    ("adult", "cpr", "ARC2025", 90, 6, False),
    ("child", "cpr", "ARC2020", 89, 6, True),
    ("child", "cpr", "ARC2020", 90, 6, False),
    ("infant", "cpr", "ARC2020", 44, 6, True),
    ("infant", "cpr", "ARC2020", 45, 5, True),
    ("infant", "cpr", "ARC2020", 45, 6, False),
    ("adult", "cpr", "AHA2020", 0, 0, False),
    ("adult", "compression_only", "ARC2025", 0, 0, False),
    ("child", "ventilation_only", "ARC2025", 0, 0, False),
])
def test_d07_predicate_keeps_historical_scope_and_thresholds(target, training, guideline, comp, vent, active):
    policy = minimum_quantity_policy(_condition(target, training, guideline), comp, vent)
    assert policy.active is active


# ---------------------------------------------------------------------------
# 2. jobs.check_evaluation (the stored evaluation has no counts)
# ---------------------------------------------------------------------------

def _stored(decision, reasons, *, observed=3, required=3, kind="cycles", status="evaluated"):
    met = observed >= required
    goal = {"kind": kind, "required": required, "observed": observed, "met": met}
    if status is not None:
        goal["status"] = status
    return {"goal": goal, "score": {"decision": decision}, "program_completed": met and decision == "pass",
            "reason_codes": list(reasons)}


def _check(value, definition):
    return DynamoJobRepository.check_evaluation(value, {"definition_json": json.dumps(definition)})


def _refused(value, definition):
    with pytest.raises(JourneyError) as error:
        _check(value, definition)
    assert error.value.code == "CALCULATOR_CONTRACT_MISMATCH"


@pytest.mark.parametrize("observed,goal_reasons", [(3, []), (2, [G])])
def test_saved_v5_accepts_exactly_three_fail_explanations(observed, goal_reasons):
    definition = _definition()
    for tail in ([F], [M], [M, F]):
        value = _stored("fail", goal_reasons + tail, observed=observed)
        assert _check(value, definition) == value
    assert _check(_stored("pass", goal_reasons, observed=observed), definition) == _stored("pass", goal_reasons,
                                                                                         observed=observed)
    for tail in ([], [F, M], [M, M], [M, F, F], [F, F], ["OTHER"], [M, "OTHER"]):
        _refused(_stored("fail", goal_reasons + tail, observed=observed), definition)
    for tail in ([M], [F], [M, F]):
        _refused(_stored("pass", goal_reasons + tail, observed=observed), definition)
    # The minimum code never precedes the goal reason.
    if goal_reasons:
        _refused(_stored("fail", [M] + goal_reasons, observed=observed), definition)
        _refused(_stored("fail", [M, F], observed=observed), definition)  # the goal reason is missing
    # A completed program is never stored with a fail decision.
    completed = {**_stored("fail", [M]), "program_completed": True}
    _refused(completed, definition)
    for wrong_type in ((M,), M, None, {M: True}):
        _refused({**_stored("fail", [M]), "reason_codes": wrong_type}, definition)


@pytest.mark.parametrize("condition", [
    {"training": "compression_only"}, {"training": "ventilation_only"}, {"guideline": "AHA2020"},
    {"guideline": "ERC2020"}, {"guideline": "STD2015"},
])
def test_minimum_code_is_refused_where_the_minimum_cannot_apply(condition):
    kind = {"compression_only": "compressions", "ventilation_only": "ventilations"}.get(condition.get("training"),
                                                                                       "cycles")
    definition = _definition(kind=kind, required=3, **condition)
    assert _check(_stored("fail", [F], kind=kind), definition) == _stored("fail", [F], kind=kind)
    for tail in ([M], [M, F]):
        _refused(_stored("fail", tail, kind=kind), definition)


@pytest.mark.parametrize("adapter", [V4, PENDING_V3, PENDING_V2, "retained-v1"])
def test_older_adapters_refuse_the_minimum_code_and_keep_their_exact_rule(adapter):
    definition = _definition(adapter)
    versioned = expected_profile_version(adapter) is not None
    if adapter in (PENDING_V3, PENDING_V2):
        base = {"goal": {"kind": "cycles", "required": 3, "observed": None, "met": None, "status": "pending_policy"},
                "program_completed": False}
        accepted_fail = {**base, "score": {"decision": "fail"}, "reason_codes": ["GOAL_POLICY_UNRESOLVED", F]}
        assert _check(accepted_fail, definition) == accepted_fail
        for reasons in (["GOAL_POLICY_UNRESOLVED", M], ["GOAL_POLICY_UNRESOLVED", M, F], [M]):
            _refused({**base, "score": {"decision": "fail"}, "reason_codes": reasons}, definition)
        return
    status = "evaluated" if versioned else None
    assert _check(_stored("fail", [F], status=status), definition) == _stored("fail", [F], status=status)
    assert _check(_stored("pass", [], status=status), definition) == _stored("pass", [], status=status)
    for tail in ([M], [M, F], [F, M]):
        _refused(_stored("fail", tail, status=status), definition)
    _refused(_stored("pass", [M], status=status), definition)


# ---------------------------------------------------------------------------
# 3. Historical evaluation facts (no new computation under retained names)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,saved", [
    # Existing v5 facts: ((comp, vent), overall, observed, decision, completed, reasons).
    ("tester", ((61, 6), 83, 3, "fail", False, [M])),
    ("cpr_1", ((48, 2), 70, 1, "fail", False, [G, M, F])),
    ("cpr_2", ((99, 5), 86, 3, "fail", False, [M])),
    ("cpr_3", ((100, 3), 82, 2, "fail", False, [G, M])),
    ("cpr_4", ((69, 6), 80, 3, "fail", False, [M])),
    ("cpr_5", ((36, 4), 66, 4, "fail", False, [M, F])),
    ("3x(30+2)", ((90, 6), 99, 3, "pass", True, [])),
    ("3x(30+2) cut", ((90, 6), 99, 3, "pass", True, [])),
    ("89/6", ((89, 6), 98, 3, "fail", False, [M])),
    ("90/5", ((90, 5), 99, 3, "fail", False, [M])),
    ("3x(10+1)", ((30, 3), 81, 3, "fail", False, [M])),
    ("1x(30+2)", ((30, 2), 99, 1, "fail", False, [G, M])),
], ids=lambda value: value if type(value) is str else "")
def test_saved_v5_adult_cpr_evaluations_keep_their_exact_meaning(name, saved):
    counts, overall, observed, decision, completed, reasons = saved
    result = _assess(_definition(), *counts, overall=overall, observed=observed)
    assert _outcome(result) == (decision, completed, reasons), name


@pytest.mark.parametrize("adapter", [V4, PENDING_V3])
@pytest.mark.parametrize("counts,overall,observed,decision", [
    ((61, 5), None, 3, "fail"),
    ((99, 5), 89, 3, "pass"),
    ((90, 6), 99, 3, "pass"),
])
def test_saved_older_evaluations_do_not_gain_the_v5_minimum_gate(adapter, counts, overall, observed, decision):
    pending = adapter == PENDING_V3
    reasons = (["GOAL_POLICY_UNRESOLVED"] if pending else []) + ([F] if decision == "fail" else [])
    result = _assess(_definition(adapter), *counts, overall=overall, observed=observed)
    assert _outcome(result) == (decision, decision == "pass" and not pending, reasons)

"""Offline typed regression with independently derived D38-D46/D138 detection inputs.

The original reference observations and ARC exceptions remain unchanged. The
candidate runs in an isolated worker with side-effect guards; its output never
supplies expected values. Reused historical scoring sources are hash-pinned.

D139 (2026-09-30) abolished the ARC minimum-quantity null for new
calculations (the minimum stays a pass condition of the evaluation, which is
not part of the calculation result compared here). The current rules are therefore compared with the recorded
reference values alone; the approved ARC exceptions
(approved_expectations.json, unchanged) stay the historical expectation of the
retained adapters' options (arc-internal-detection-v4 / pending-v3), which a
second matrix below keeps proving.
"""

import json
import base64
from copy import deepcopy
from pathlib import Path

import pytest

from scripts import verify_reference_parity as parity
from tests.calculation_options_support import RETAINED_OPTIONS, run_with_options
from tests.detection_oracle import run_expected


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures/reference_parity"
REFERENCE = json.loads((FIXTURES / "reference_results.json").read_text(encoding="utf-8"))
APPROVED = json.loads((FIXTURES / "approved_expectations.json").read_text(encoding="utf-8"))
MANIFEST = json.loads((FIXTURES / "parity_manifest.json").read_text(encoding="utf-8"))
REFERENCE_BY_ID = {case["id"]: case for case in REFERENCE["cases"]}
APPROVED_BY_ID = {case["id"]: case for case in APPROVED["cases"]}


@pytest.fixture(scope="module")
def input_cases():
    return parity._build_cases(ROOT)


@pytest.fixture(scope="module")
def current_results(input_cases):
    # Only ROOT is executed; the reference is a versioned local JSON fixture.
    reply = parity._run_worker(ROOT, input_cases)
    assert not reply.get("unexpected_blocked_operations")
    assert all(value == "blocked" for value in reply["guard_self_test"].values())
    assert len(reply["cases"]) == len(input_cases)
    return {case["id"]: case for case in reply["cases"]}


def _record(case, core):
    import lambda_handler

    converted = lambda_handler._convert_result_to_legacy(deepcopy(core), deepcopy(case["condition"]))
    return json.loads(json.dumps({"id": case["id"], "status": "ok",
                                  "main_result": core, "http_calculation_result": converted}))


def _expectations(input_cases, options):
    # User-approved D38-D46/D138 change events and their measurement periods.
    # Do not copy the current worker output or overwrite the old JSON oracle.
    expected = {}
    for case in input_cases:
        core, _chart = run_expected(base64.b64decode(case["cpr_b64"]), base64.b64decode(case["aed_b64"]),
                                    case["condition"], case["vp_event_list"], options=options)
        expected[case["id"]] = _record(case, core)
    return expected


@pytest.fixture(scope="module")
def detection_expectations(input_cases):
    return _expectations(input_cases, None)


@pytest.fixture(scope="module")
def retained_expectations(input_cases):
    return _expectations(input_cases, RETAINED_OPTIONS)


@pytest.fixture(scope="module")
def retained_results(input_cases):
    # The retained adapters' path: the same core through an execution context
    # carrying their options (mock_journey.contracts.ADAPTER_FEATURES).
    results = {}
    for case in input_cases:
        core, _chart, _evidence = run_with_options(
            base64.b64decode(case["cpr_b64"]), base64.b64decode(case["aed_b64"]), case["condition"],
            RETAINED_OPTIONS, case["vp_event_list"])
        results[case["id"]] = _record(case, core)
    return results


def test_oracle_coverage_and_input_identity(input_cases):
    manifest = {case["id"]: case for case in MANIFEST["cases"]}
    assert set(manifest) == set(REFERENCE_BY_ID) == {case["id"] for case in input_cases}
    groups = {}
    for case in input_cases:
        groups[case["group"]] = groups.get(case["group"], 0) + 1
        for key in ("condition", "cpr_sha256", "aed_sha256", "vp_event_list"):
            assert not parity._differences(manifest[case["id"]][key], case[key]), (case["id"], key)
    assert groups == {"matrix": 45, "boundary": 60, "recorded": 8, "vp": 6, "erc_rescue": 4}
    assert set(APPROVED_BY_ID) <= set(REFERENCE_BY_ID)


def test_approved_expectations_retain_independently_checked_arithmetic(input_cases):
    active_ids = set()
    for case in input_cases:
        reference = REFERENCE_BY_ID[case["id"]]
        assert reference["status"] == "ok", case["id"]
        derived = parity._derive_arc_minimum_result(reference["main_result"], case)
        if derived is None:
            assert case["id"] not in APPROVED_BY_ID
            continue
        active_ids.add(case["id"])
        _, arithmetic = derived
        approved = APPROVED_BY_ID[case["id"]]
        assert not parity._differences(arithmetic, approved["arithmetic"]), case["id"]
        for key in ("main_result", "http_calculation_result"):
            expected, _ = parity._derive_arc_minimum_result(reference[key], case)
            # Exact strings came from the reference generator fed independently
            # derived totals, unchanged metrics, and its measured coaching signal.
            expected["guide_prompts"] = approved["expected_record"][key]["guide_prompts"]
            assert not parity._differences(expected, approved["expected_record"][key]), case["id"]
    assert active_ids == set(APPROVED_BY_ID)
    assert len(active_ids) == 27


@pytest.mark.parametrize("case_id", list(REFERENCE_BY_ID))
def test_calculator_matches_independent_detection_expectation(case_id, current_results, detection_expectations):
    # D139: the current rules have no ARC exception, so the historical value of
    # every case (the former 27 exceptions included) is the reference itself.
    historical = REFERENCE_BY_ID[case_id]
    expected = detection_expectations[case_id]
    current = current_results[case_id]
    assert current["status"] == "ok", (case_id, current)
    differences = parity._differences(expected, current)
    assert not differences, (case_id, differences[:10])
    # On paths for which the independent oracle says the rule makes no change,
    # the full typed comparison still enforces the original reference value.
    if not parity._differences(historical, expected):
        assert not parity._differences(historical, current)


@pytest.mark.parametrize("case_id", list(REFERENCE_BY_ID))
def test_retained_options_match_independent_detection_expectation(case_id, retained_results, retained_expectations):
    # The pre-D138/D139 matrix, unchanged: the approved ARC exception is still
    # the historical expectation of the retained adapters' options.
    historical = APPROVED_BY_ID.get(case_id, {}).get("expected_record", REFERENCE_BY_ID[case_id])
    expected = retained_expectations[case_id]
    current = retained_results[case_id]
    differences = parity._differences(expected, current)
    assert not differences, (case_id, differences[:10])
    if not parity._differences(historical, expected):
        assert not parity._differences(historical, current)


@pytest.mark.parametrize("case_id", list(APPROVED_BY_ID))
def test_abolished_arc_minimum_is_exactly_the_approved_exception_removed(case_id, input_cases, current_results,
                                                                         retained_results):
    # D139 removes the exception and nothing else: applying the independent
    # approved-exception arithmetic (nulled groups, reweighted overall) to the
    # current result reproduces the retained adapters' result. Coaching text is
    # derived from the totals by the pinned generator, so it is taken as is.
    case = next(case for case in input_cases if case["id"] == case_id)
    for key in ("main_result", "http_calculation_result"):
        derived, _audit = parity._derive_arc_minimum_result(current_results[case_id][key], case)
        derived["guide_prompts"] = retained_results[case_id][key]["guide_prompts"]
        assert not parity._differences(derived, retained_results[case_id][key]), (case_id, key)


# Former null-group scores that do not return to the recorded reference value
# under the current rules: (reference, current). Each is the ventilation-rate
# measurement interval of the detection revision (D45), not D139 -- the
# independent oracle expects the same value (test above) and cpr_4's 31 was
# already visible before D139 (its ventilation group was never null).
NULL_GROUP_REVISION_DIFFERENCES = {
    ("recorded/cpr_1", "score_vent_rate"): (100, 0),
    ("recorded/cpr_2", "score_vent_rate"): (15, 0),
    ("recorded/cpr_4", "score_vent_rate"): (65, 31),
}


def test_former_arc_minimum_null_groups_return_to_the_reference_scores(current_results):
    # The 27 approved exceptions nulled these group scores. Without the policy
    # they must be the recorded reference numbers again (not the current output
    # pinned as its own expectation). score_ccf/overall are outside the null
    # groups and follow the D45 timeline revision for every guideline alike.
    observed = {}
    for case_id in APPROVED_BY_ID:
        for key in ("main_result", "http_calculation_result"):
            reference = REFERENCE_BY_ID[case_id][key]["cpr_score"]["total_score"]
            current = current_results[case_id][key]["cpr_score"]["total_score"]
            assert current["overall"] is not None, case_id
            for name in parity.CHEST_NULL_KEYS + parity.VENT_NULL_KEYS:
                if parity._differences(reference[name], current[name]):
                    assert observed.setdefault((case_id, name), (reference[name], current[name])) == \
                        (reference[name], current[name])
        assert current_results[case_id]["main_result"]["action_count"] == \
            REFERENCE_BY_ID[case_id]["main_result"]["action_count"]
    assert observed == NULL_GROUP_REVISION_DIFFERENCES


@pytest.mark.parametrize("reference,current", [(1, True), (1, 1.0), (None, 0), (-0.0, 0.0), ({}, {"x": None})])
def test_parity_assertions_do_not_collapse_json_types(reference, current):
    assert parity._differences(reference, current)

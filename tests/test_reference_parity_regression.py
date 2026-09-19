"""Offline typed regression with independently derived D38-D46 detection inputs.

The original reference observations and ARC exceptions remain unchanged. The
candidate runs in an isolated worker with side-effect guards; its output never
supplies expected values. Reused historical scoring sources are hash-pinned.
"""

import json
import base64
from copy import deepcopy
from pathlib import Path

import pytest

from scripts import verify_reference_parity as parity
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


@pytest.fixture(scope="module")
def detection_expectations(input_cases):
    # User-approved D38-D46 change events and their measurement periods.
    # Do not copy the current worker output or overwrite the old JSON oracle.
    import lambda_handler

    expected = {}
    for case in input_cases:
        core, _chart = run_expected(base64.b64decode(case["cpr_b64"]), base64.b64decode(case["aed_b64"]),
                                    case["condition"], case["vp_event_list"])
        converted = lambda_handler._convert_result_to_legacy(deepcopy(core), deepcopy(case["condition"]))
        expected[case["id"]] = json.loads(json.dumps({"id": case["id"], "status": "ok",
                                                     "main_result": core, "http_calculation_result": converted}))
    return expected


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
    historical = APPROVED_BY_ID.get(case_id, {}).get("expected_record", REFERENCE_BY_ID[case_id])
    expected = detection_expectations[case_id]
    current = current_results[case_id]
    assert current["status"] == "ok", (case_id, current)
    differences = parity._differences(expected, current)
    assert not differences, (case_id, differences[:10])
    # On paths for which the independent oracle says the rule makes no change,
    # the full typed comparison still enforces the original reference value.
    if not parity._differences(historical, expected):
        assert not parity._differences(historical, current)


@pytest.mark.parametrize("reference,current", [(1, True), (1, 1.0), (None, 0), (-0.0, 0.0), ({}, {"x": None})])
def test_parity_assertions_do_not_collapse_json_types(reference, current):
    assert parity._differences(reference, current)

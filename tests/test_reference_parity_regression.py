"""Offline typed regression against reference observations and the approved ARC exception.

CI reads checked-in observations, never the external reference directory. The
current calculator runs in an isolated worker with side-effect guards.
"""

import json
from pathlib import Path

import pytest

from scripts import verify_reference_parity as parity


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
def test_calculator_matches_reference_or_explicit_arc_exception(case_id, current_results):
    expected = APPROVED_BY_ID.get(case_id, {}).get("expected_record", REFERENCE_BY_ID[case_id])
    current = current_results[case_id]
    assert current["status"] == "ok", (case_id, current)
    differences = parity._differences(expected, current)
    assert not differences, (case_id, differences[:10])


@pytest.mark.parametrize("reference,current", [(1, True), (1, 1.0), (None, 0), (-0.0, 0.0), ({}, {"x": None})])
def test_parity_assertions_do_not_collapse_json_types(reference, current):
    assert parity._differences(reference, current)

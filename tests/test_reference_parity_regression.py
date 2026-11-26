"""Offline regression against raw outputs from the pinned hstm legacy path.

Expected values are captured by the guarded reference worker, never derived
from ARC output. Types, key order, signed zero, charts, and serialization bytes
are all part of the comparison. Historical result_*.json files are separately
checked as source artifacts; they do not override live reference observations.
"""

import base64
from itertools import product
import json
from pathlib import Path

import pytest

from scripts import verify_reference_parity as parity


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures/reference_parity"
REFERENCE = json.loads((FIXTURES / "reference_results.json").read_text(encoding="utf-8"))
MANIFEST = json.loads((FIXTURES / "parity_manifest.json").read_text(encoding="utf-8"))
REFERENCE_BY_ID = {case["id"]: case for case in REFERENCE["cases"]}


@pytest.fixture(scope="module")
def input_cases():
    return parity._build_cases(ROOT)


@pytest.fixture(scope="module")
def current_results(input_cases):
    # Execute only ARC. The reference authority is a checked-in observation.
    reply = parity._run_worker(ROOT, input_cases)
    assert not reply.get("unexpected_blocked_operations")
    assert all(value == "blocked" for value in reply["guard_self_test"].values())
    assert reply["legacy_conversion_available"] is True
    assert len(reply["cases"]) == len(input_cases)
    return {case["id"]: case for case in reply["cases"]}


def test_reference_authority_and_input_identity(input_cases):
    assert MANIFEST["reference_root"] == str(parity.DEFAULT_REFERENCE)
    assert MANIFEST["reference_commit"] == parity.REFERENCE_COMMIT
    assert MANIFEST["authority"] == parity.REFERENCE_AUTHORITY
    assert MANIFEST["generator_sha256"]["before"] == MANIFEST["generator_sha256"]["after"]
    assert MANIFEST["generator_sha256"]["after"] == parity._sha((ROOT / "tests/_synth.py").read_bytes())
    manifest = {case["id"]: case for case in MANIFEST["cases"]}
    assert len(manifest) == len(MANIFEST["cases"])
    assert set(manifest) == set(REFERENCE_BY_ID) == {case["id"] for case in input_cases}
    for case in input_cases:
        metadata = {key: value for key, value in case.items() if key not in ("cpr_b64", "aed_b64")}
        assert not parity._differences(manifest[case["id"]], metadata), case["id"]
        for channel in ("cpr", "aed"):
            assert parity._sha(base64.b64decode(case[f"{channel}_b64"])) == case[f"{channel}_sha256"]


def test_recordings_cover_full_condition_grid_and_arc_regions(input_cases):
    recordings = {path.stem for path in (ROOT / "tests/dataset").glob("*.bin")
                  if not path.name.startswith("aed_")}
    assert {"infant_cco_idle_flag", "infant_cpr_idle_flag_pause", "cpr_eof_truncated_vent_1"} <= recordings
    expected = {
        (recording, guideline, target, training_type, region)
        for recording, guideline, target, training_type in product(
            recordings, parity.GUIDELINES, parity.TARGETS, parity.TRAINING_TYPES)
        for region in (parity.ARC_REGIONS if guideline.startswith("ARC") else ("british",))
    }
    actual = {
        (Path(case["source_files"][0]).stem, case["condition"]["guideline"],
         case["condition"]["target"], case["condition"]["training_type"], case["usage"]["Regional_Option"])
        for case in input_cases if case["group"] == "recorded"
    }
    assert actual == expected
    groups = {group: sum(case["group"] == group for case in input_cases)
              for group in ("matrix", "boundary", "recorded", "vp", "erc_rescue")}
    assert groups == {"matrix": 63, "boundary": 84, "recorded": len(expected), "vp": 12, "erc_rescue": 4}
    assert len({case["id"] for case in input_cases}) == len(input_cases)


def test_vp_first_compression_interval_matches_generated_timestamps(input_cases):
    for case in input_cases:
        if case["group"] != "vp" or "/first_compression_segment_virtual/" not in case["id"]:
            continue
        raw = base64.b64decode(case["cpr_b64"])
        count = 15 if case["condition"]["target"] == "infant" else 30
        vent_start = int.from_bytes(raw[(1 + count * 10) * 28 + 20:(1 + count * 10) * 28 + 28], "big")
        assert case["vp_event_list"][1]["timestamp"] == vent_start == 100 + count * 500


@pytest.mark.parametrize("case_id", list(REFERENCE_BY_ID))
def test_calculator_matches_raw_reference_bytes(case_id, current_results):
    expected = REFERENCE_BY_ID[case_id]
    assert case_id in current_results, "Reference fixture requires registration for the current case grid."
    current = current_results[case_id]
    assert expected["status"] == current["status"] == "ok", (case_id, current)
    differences = parity._differences(expected, current)
    assert not differences, (case_id, differences[:10])
    for field in ("main_result", "http_calculation_result", "chart_json"):
        expected_bytes = expected["serialized_json"][field].encode("utf-8")
        current_bytes = current["serialized_json"][field].encode("utf-8")
        assert expected_bytes == current_bytes == parity._json_bytes(current[field]), (case_id, field)
    assert parity._json_bytes(expected) == parity._json_bytes(current)


@pytest.mark.parametrize("reference,current", [
    (1, True), (1, 1.0), (None, 0), (-0.0, 0.0), ({}, {"x": None}),
    ({"a": 1, "b": 2}, {"b": 2, "a": 1}),
])
def test_parity_assertions_preserve_types_order_and_float_bits(reference, current):
    assert parity._differences(reference, current)
    assert parity._json_bytes(reference) != parity._json_bytes(current)


def test_serialization_retains_original_key_order():
    value = {"z": -0.0, "a": 1.0, "b": True, "c": None}
    assert parity._json_bytes(value) == b'{"z": -0.0, "a": 1.0, "b": true, "c": null}'

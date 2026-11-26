"""ACTION_VECTORS_V1: breath and cycle vectors the app replays from the same files (D142, S4).

Each tests/fixtures/action_vectors_v1/<case>/ holds raw.bin (and aed.bin) copied byte for byte from
tests/dataset, input.json, expected.json = replay() (json indent=2) and manifest.json with every
file's SHA-256. The field definitions for the app are in docs/APP_API.md section 5. The checks
below tie the pinned values to facts that do not come from replay itself. Never regenerate a
vector to make an implementation pass; investigate and correct it only with user approval.
"""

import hashlib
import json
from pathlib import Path

import pytest

from calculators.action_evaluator import ActionEvaluator
from config.constants import ACTION_TYPE_VENT, CALC_CASE_CPR
from services.calculation_context import CalculationOptions
from services.config import Config
from services.preparers import make_pre_action_list, parse_data, prepare_data
from tests.calculation_context_support import run_with_options


ROOT = Path(__file__).resolve().parents[1]
DESTINATION = ROOT / "tests/fixtures/action_vectors_v1"
SPEC = "ACTION_VECTORS_V1"
# case: (source CPR file, source AED file, closed cycles counted by hand from the recording)
CASES = {
    "adult_cpr_eof_truncated": ("cpr_eof_truncated_vent_1.bin", None, None),
    "adult_cpr_aed": ("cpr_2.bin", "aed_2.bin", 3),
    "adult_2r_cpr_vp": ("cpr_4.bin", None, 3),
    "infant_cpr": ("infant_cpr_idle_flag_pause.bin", None, None),
    "adult_ventilation_only": ("adult_vo_1.bin", None, 0),
    "infant_ventilation_only": ("vo_1.bin", None, 0),
}


def replay(raw, inputs, aed=b""):
    """The production boundary and cycle evidence in the vector schema; nothing is re-derived here."""
    condition, vp_events = inputs["condition"], inputs["vp_event_list"]
    infant = condition["target"] == "infant"
    config = Config(condition)
    parsed = parse_data(raw, aed, config)
    records = parsed["rtdata_list"]
    actions, aed_parts = make_pre_action_list(config, parsed, vp_events)
    prepare_data(actions, aed_parts, config)
    evaluator = ActionEvaluator(config)
    breaths, previous = [], None
    for action in actions:
        if action["action_type"] != ACTION_TYPE_VENT:
            continue
        score = evaluator._evaluate_vent(action, config.border, action["actor"]).score
        breaths.append({
            "id": len(breaths) + 1, "count_record": action["action_end"],
            "action_start": action["action_start"], "action_end": action["action_end"],
            "volume_ml": score["vent_vol"]["value"],
            # From the previous breath's confirming Record to the last Record of this window.
            "interval_ms": None if previous is None else action["last_timestamp"] - records[previous]["timestamp"],
            "inflation_ms": score["vent_speed"]["value"] if infant else None,
        })
        previous = action["action_end"]
    result, _, evidence = run_with_options(raw, aed, condition, CalculationOptions(collect_gates=True), vp_events)
    cycles = [{"part": cycle.part_number, "cycle": cycle.cycle_number, "comp": cycle.compression_action_count,
               "vent": cycle.ventilation_action_count, "calc_case": cycle.calc_case, "actor_type": cycle.actor_type}
              for cycle in evidence.cycles]
    return {
        "spec": SPEC, "records": len(records), "ventilations": breaths, "cycles": cycles,
        "closed_cycles": sum(cycle["calc_case"] == CALC_CASE_CPR for cycle in cycles),
        "learner_counts": dict(evidence.gate_evidence["learner_counts"]), "action_count": dict(result["action_count"]),
    }


def encode_json(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()


def load(name):
    directory = DESTINATION / name
    inputs = json.loads((directory / "input.json").read_text())
    aed = (directory / "aed.bin").read_bytes() if inputs.get("aed_file") else b""
    return directory, (directory / "raw.bin").read_bytes(), inputs, aed, json.loads((directory / "expected.json").read_text())


@pytest.mark.parametrize("name", CASES)
def test_replay_matches_every_expected_field_and_manifest(name):
    directory, raw, inputs, aed, expected = load(name)
    assert replay(raw, inputs, aed) == expected
    assert (directory / "expected.json").read_bytes() == encode_json(expected)
    manifest = json.loads((directory / "manifest.json").read_text())
    files = ["raw.bin", "input.json", "expected.json", *(["aed.bin"] if aed else [])]
    assert manifest["spec"] == SPEC and set(manifest["sha256"]) == set(files)
    for filename in files:
        assert manifest["sha256"][filename] == hashlib.sha256((directory / filename).read_bytes()).hexdigest()
    source, source_aed, _ = CASES[name]
    assert manifest["source"] == {"raw": f"tests/dataset/{source}", "aed": source_aed and f"tests/dataset/{source_aed}"}
    assert raw == (ROOT / "tests/dataset" / source).read_bytes()
    assert aed == ((ROOT / "tests/dataset" / source_aed).read_bytes() if source_aed else b"")


@pytest.mark.parametrize("name", CASES)
def test_vectors_agree_with_counts_and_each_other(name):
    _, raw, inputs, aed, expected = load(name)
    breaths, cycles, counts = expected["ventilations"], expected["cycles"], expected["action_count"]
    infant = inputs["condition"]["target"] == "infant"
    assert expected["records"] == len(raw) // 28
    assert len(breaths) == counts["vent"]
    assert [breath["id"] for breath in breaths] == list(range(1, len(breaths) + 1))
    assert all(type(breath["volume_ml"]) is int and breath["volume_ml"] >= 0 for breath in breaths)
    assert [breath["interval_ms"] is None for breath in breaths] == [index == 0 for index in range(len(breaths))]
    assert all((type(breath["inflation_ms"]) is int) is infant for breath in breaths)
    for before, after in zip(breaths, breaths[1:]):
        assert before["count_record"] <= after["action_start"] < after["action_end"] == after["count_record"]
    assert counts["comp"] >= sum(cycle["comp"] for cycle in cycles)
    assert counts["vent"] >= sum(cycle["vent"] for cycle in cycles)
    assert expected["closed_cycles"] == sum(cycle["calc_case"] == "cpr" for cycle in cycles)
    if CASES[name][2] is not None:
        assert expected["closed_cycles"] == CASES[name][2]
    assert all(expected["learner_counts"][key] <= counts[key] for key in counts)
    if not inputs["condition"]["is_2rescuers"]:
        assert expected["learner_counts"] == counts


@pytest.mark.parametrize("name", ["adult_ventilation_only", "infant_ventilation_only"])
def test_ventilation_only_interval_is_the_scored_window(name):
    # The server scores a breath of a ventilation-only session by its window length (total_action_ms).
    _, raw, inputs, aed, expected = load(name)
    config = Config(inputs["condition"])
    parsed = parse_data(raw, aed, config)
    actions, aed_parts = make_pre_action_list(config, parsed, inputs["vp_event_list"])
    prepare_data(actions, aed_parts, config)
    windows = [action["total_action_ms"] for action in actions if action["action_type"] == ACTION_TYPE_VENT]
    assert [breath["interval_ms"] for breath in expected["ventilations"][1:]] == windows[1:]


def test_eof_vector_keeps_the_end_of_file_breath():
    _, raw, _, _, expected = load("adult_cpr_eof_truncated")
    assert expected["ventilations"][-1]["count_record"] == expected["records"] == len(raw) // 28


def test_two_rescuer_vector_has_virtual_partner_work():
    _, _, inputs, _, expected = load("adult_2r_cpr_vp")
    assert inputs["condition"]["is_2rescuers"] is True and inputs["vp_event_list"]
    assert {cycle["actor_type"] for cycle in expected["cycles"]} - {"ONLY:RP"}
    assert expected["learner_counts"] != expected["action_count"]

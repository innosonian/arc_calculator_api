"""DEPTH_V1r3 shared vectors: immutable full-field replay and count propagation."""
import hashlib
import json
import shutil

import pytest

from calculators.cycle_evaluator import Cycle, CycleWithScore
from config.constants import ACTION_TYPE_COMP
from config.enums import Actor
from config.score_weight import ScoreWeightFactory
from data_handlers.chart_data import _get_calculated_cycles, make_chart_data
from services.legacy_document import _build_result_summary, _is_custom_exceed_pass_fail_condition, _is_not_enough_attempt
from main import run_calculator
from scripts import check_depth_boundary_golden as golden
from services.calculators import calculate_cpr
from services.config import Config
from services.preparers import make_pre_action_list, parse_data, prepare_data


SYNTHETIC = list(golden.synthetic_cases())
CASES = [row[0] for row in SYNTHETIC] + [f"real/{session_id}" for session_id in golden.REAL_COUNTS]
AED_PART_FIRST = {"real/19148": [10], "real/19986": [8], "real/19994": [27], "real/20016": [21]}


def load_case(name):
    directory = golden.DESTINATION / name
    return (
        (directory / "raw.bin").read_bytes(),
        json.loads((directory / "input.json").read_text()),
        json.loads((directory / "expected.json").read_text()),
    )


def load_aed(name, inputs):
    return (golden.DESTINATION / name / "aed.bin").read_bytes() if inputs.get("aed_file") else b""


@pytest.mark.parametrize("name", CASES)
def test_replay_matches_every_expected_field_and_manifest(name):
    raw, inputs, expected = load_case(name)
    actual, _ = golden.replay(raw, inputs, load_aed(name, inputs))
    assert actual == expected
    directory = golden.DESTINATION / name
    manifest = json.loads((directory / "manifest.json").read_text())
    assert manifest["spec"] == "DEPTH_V1r3"
    assert manifest["amp"] == 30
    assert manifest["boundary_amendment"] == golden.BOUNDARY_AMENDMENT
    assert len(manifest["generation_commit"]) == 40
    assert manifest["sha256"] == {
        filename: hashlib.sha256((directory / filename).read_bytes()).hexdigest()
        for filename in ("raw.bin", "input.json", "expected.json", *(["aed.bin"] if inputs.get("aed_file") else []))
    }


@pytest.mark.parametrize("name,raw,inputs,oracle", SYNTHETIC, ids=[row[0] for row in SYNTHETIC])
def test_synthetic_vectors_match_handwritten_oracle(name, raw, inputs, oracle):
    saved_raw, saved_inputs, saved_expected = load_case(name)
    assert (saved_raw, saved_inputs, saved_expected) == (raw, inputs, oracle)
    assert golden.replay(raw, inputs)[0] == oracle


@pytest.mark.parametrize("base", [row[0].rsplit("/", 1)[0] for row in SYNTHETIC if row[0].endswith("/zero")])
def test_byte12_variants_change_only_counter_and_preserve_all_fields(base):
    variants = [load_case(f"{base}/{variant}") for variant in golden.COUNTER_VARIANTS]
    assert variants[0][2] == variants[1][2] == variants[2][2]
    without_counter = [bytes(byte for i, byte in enumerate(raw) if i % 28 != 12) for raw, _, _ in variants]
    assert without_counter[0] == without_counter[1] == without_counter[2]
    assert all(variants[0][0][index] == 0 for index in range(12, len(variants[0][0]), 28))


@pytest.mark.parametrize("name", [row[0] for row in SYNTHETIC if row[0].endswith("/zero")])
def test_firmware_rate_cycle_and_sequence_do_not_change_golden_fields(name):
    raw, inputs, expected = load_case(name)
    changed = bytearray(raw)
    for record in range(len(raw) // 28):
        for byte in (11, 12, 18, 19):
            changed[record * 28 + byte] = 255 - changed[record * 28 + byte]
    assert golden.replay(bytes(changed), inputs)[0] == expected


@pytest.mark.parametrize("session_id,count", golden.REAL_COUNTS.items())
def test_real_session_action_cycle_chart_summary_and_gates_agree(session_id, count):
    name = f"real/{session_id}"
    raw, inputs, expected = load_case(name)
    aed = load_aed(name, inputs)
    config = Config(inputs["condition"])
    parsed = parse_data(raw, aed, config)
    actions, aed_parts = make_pre_action_list(config, parsed, inputs["vp_event_list"])
    prepared = prepare_data(actions, aed_parts, config)
    calculation = calculate_cpr(prepared, config)
    cycles = [cycle for part in calculation["part_with_scores"] for cycle in part["result"]["cycle_with_score_list"]]
    result = run_calculator(raw, aed, inputs["condition"], inputs["vp_event_list"], stage="test")
    chart = make_chart_data(prepared["whole_cpr_action_list"], prepared["prepared_aed_data"], calculation["part_with_scores"])
    assert expected["count"] == count
    assert prepared["comp_count"] == result["action_count"]["comp"] == count
    assert len([bar for bar in chart["cpr_data_set"] or [] if bar["action_type"] == ACTION_TYPE_COMP]) == count
    # 19994 retains the existing AED exclusion: global63 = cycle37 + excluded26.
    comps = [action for action in prepared["whole_cpr_action_list"] if action["action_type"] == ACTION_TYPE_COMP]
    assert len(comps) == count
    calculated = _get_calculated_cycles(calculation["part_with_scores"])
    aed_excluded = {action["peak_record"] for action in comps if action["overlap_type"] is not None}
    cycle_excluded = {action["peak_record"] for action in comps
                      if f'part:{action["part_num"]}_{action.get("cycle_cnt")}' not in calculated}
    excluded = aed_excluded | cycle_excluded
    assert len(aed_excluded) == (26 if session_id == "19994" else 0)
    assert len(excluded) == (26 if session_id == "19994" else 0)
    if session_id == "19994":
        assert {action["overlap_type"] for action in comps if action["peak_record"] in aed_excluded} == {"action_under_aed"}
    assert cycle_excluded - aed_excluded == set()
    cycle_peaks = [event.peak_packet_index for cycle in cycles for event in cycle._compression_events()]
    assert len(cycle_peaks) == len(set(cycle_peaks))
    assert set(cycle_peaks).isdisjoint(excluded)
    assert set(cycle_peaks) | excluded == {action["peak_record"] for action in comps}
    assert len(cycle_peaks) + len(excluded) == count
    if session_id in golden.AED_REAL_COUNTS:
        part_counts = [sum(len(cycle._compression_events()) for cycle in part["result"]["cycle_with_score_list"])
                       for part in calculation["part_with_scores"]]
        assert len(part_counts) == (1 if session_id == "19994" else 2)
        assert sum(part_counts) + len(excluded) == count
        assert len(prepared["prepared_aed_data"]) == 1
    # Approved clarification: metrics retain their existing RP/scored-cycle scope.
    assert result["metrics"]["CompressionActionNumber"] == sum(
        sum(action.action_type == ACTION_TYPE_COMP and action.actor == Actor.REAL_PERSON for action in cycle.cycle.actions)
        for cycle in cycles if cycle.can_calc_comp()
    )
    summary = _build_result_summary({}, result, {"Usage": {"Type": "Compression Only Training"}}, inputs["condition"])
    assert summary["CompressionNo"] == count
    assert _is_not_enough_attempt(summary, "compression_only") is (count < 60)
    custom = {"TrainCourse": {"StopCondition": {"finish_compression": 60}}}
    # Existing Custom policy uses 95% of the requested target; that policy is unchanged.
    assert _is_custom_exceed_pass_fail_condition(custom, 0, summary["CompressionNo"], 0) is (count >= 57)


@pytest.mark.parametrize("session_id", golden.AED_REAL_COUNTS)
def test_aed_pair_preserves_supplied_tail_and_condition(session_id):
    raw, inputs, expected = load_case(f"real/{session_id}")
    aed = load_aed(f"real/{session_id}", inputs)
    assert inputs == {"condition": dict(golden.DEFAULT_CONDITION), "vp_event_list": [], "aed_file": "aed.bin"}
    assert expected["records"] == len(raw) // 28
    assert raw[-28:] == bytes(28)
    assert len(aed) % 10 == 8 and aed[-8:] == bytes(8)


def test_aed_part_first_flag_exists_only_on_the_four_reviewed_compressions():
    flagged = {}
    for name in CASES:
        _, _, expected = load_case(name)
        for row in expected["compressions"]:
            if "aed_part_first" in row:
                assert row["aed_part_first"] is True
                flagged.setdefault(name, []).append(row["id"])
    assert flagged == AED_PART_FIRST


def test_replay_flags_aed_part_rate_reset_even_without_tail_split(monkeypatch):
    from data_handlers.action_data import ActionDataPrepare

    monkeypatch.setattr(ActionDataPrepare, "_split_aed_tail", lambda self, actions: actions)
    raw, inputs, _ = load_case("real/19148")
    aed = load_aed("real/19148", inputs)
    config = Config(inputs["condition"])
    parsed = parse_data(raw, aed, config)
    before, _ = make_pre_action_list(config, parsed, inputs["vp_event_list"])
    assert before[10]["prev_peak_sample"] == 11934
    actual, actions = golden.replay(raw, inputs, aed)
    assert all(action["action_type"] == ACTION_TYPE_COMP for action in actions)
    assert actions[9]["overlap_type"] == "action_cover_aed"
    assert actions[10]["overlap_type"] is None and actions[10]["part_num"] == 2
    assert actions[10]["prev_peak_sample"] is None
    assert [(row["id"], row["rate"]) for row in actual["compressions"]
            if row.get("aed_part_first")] == [(11, None)]


def test_two_rescuer_recoil_uses_next_global_peak_and_rp_counts_keep_scope():
    from calculators.action_evaluator import ActionEvaluator

    raw, inputs, expected = load_case("synthetic/two_rescuer_vp/zero")
    actual, actions = golden.replay(raw, inputs)
    assert actual == expected
    comps = [action for action in actions if action["action_type"] == ACTION_TYPE_COMP]
    assert [action["actor"] for action in comps] == [Actor.REAL_PERSON, Actor.VIRTUAL_PARTNER, Actor.REAL_PERSON]
    config = Config(inputs["condition"])
    scored = ActionEvaluator(config).evaluate_action(comps)
    cycle = CycleWithScore(Cycle(scored, 1), 1, ScoreWeightFactory.create("adult", "compression_only", "AHA2020"),
                           config.calculation_config)
    events = cycle._compression_events()
    assert len(events) == 2
    assert [event.recoil_depth for event in events] == [50, 0]


def test_checker_rejects_bad_oracle_without_writing(tmp_path, monkeypatch):
    original = golden.replay

    def wrong_count(raw, inputs):
        output, actions = original(raw, inputs)
        output["count"] += 1
        return output, actions

    destination = tmp_path / "never_written"
    monkeypatch.setattr(golden, "DESTINATION", destination)
    monkeypatch.setattr(golden, "replay", wrong_count)
    with pytest.raises(AssertionError, match="Handwritten oracle mismatch"):
        golden.main(["--check"])
    assert not destination.exists()


def test_checker_checks_all_54_vectors_without_writing(capsys):
    before = {path: path.read_bytes() for path in golden.DESTINATION.rglob("*") if path.is_file()}
    golden.main(["--check"])
    assert "Verified 54 DEPTH_V1r3 cases; no files written" in capsys.readouterr().out
    assert before == {path: path.read_bytes() for path in golden.DESTINATION.rglob("*") if path.is_file()}


@pytest.mark.parametrize("args", [[], ["--check", "--real-zip", "unused.zip"], ["--check", "--real-dir", "unused"],
                                  ["--amend-aed-flag"], ["--check", "--amend-aed-flag"]])
def test_checker_refuses_generation_and_amendment_options(args):
    before = {path: path.read_bytes() for path in golden.DESTINATION.rglob("*") if path.is_file()}
    with pytest.raises(SystemExit) as error:
        golden.main(args)
    assert error.value.code == 2
    assert before == {path: path.read_bytes() for path in golden.DESTINATION.rglob("*") if path.is_file()}


def test_checker_refuses_to_replace_changed_existing_expected(tmp_path, monkeypatch):
    destination = tmp_path / "golden"
    shutil.copytree(golden.DESTINATION, destination)
    changed = destination / "synthetic/hold_jitter/zero/expected.json"
    changed.write_text('{"count": 999}\n')
    before = {path: path.read_bytes() for path in destination.rglob("*") if path.is_file()}
    monkeypatch.setattr(golden, "DESTINATION", destination)
    with pytest.raises(AssertionError, match="Existing vector changed"):
        golden.main(["--check"])
    assert before == {path: path.read_bytes() for path in destination.rglob("*") if path.is_file()}


def test_checker_refuses_missing_aed_flags_without_amending(tmp_path, monkeypatch):
    destination = tmp_path / "golden"
    shutil.copytree(golden.DESTINATION, destination)
    for name in AED_PART_FIRST:
        path = destination / name / "expected.json"
        expected = json.loads(path.read_text())
        for row in expected["compressions"]:
            row.pop("aed_part_first", None)
        path.write_bytes(golden.encode_json(expected))
        manifest_path = path.with_name("manifest.json")
        manifest = json.loads(manifest_path.read_text())
        manifest["sha256"]["expected.json"] = hashlib.sha256(path.read_bytes()).hexdigest()
        manifest_path.write_bytes(golden.encode_json(manifest))
    monkeypatch.setattr(golden, "DESTINATION", destination)
    before = {path: path.read_bytes() for path in destination.rglob("*") if path.is_file()}
    with pytest.raises(AssertionError, match="Existing vector changed"):
        golden.main(["--check"])
    assert before == {path: path.read_bytes() for path in destination.rglob("*") if path.is_file()}


@pytest.mark.parametrize("mutation", ["other_field", "false_flag", "wrong_row", "raw", "manifest_hash"])
def test_checker_rejects_other_changes_without_writing(tmp_path, monkeypatch, mutation):
    destination = tmp_path / "golden"
    shutil.copytree(golden.DESTINATION, destination)
    directory = destination / "real/20016"
    path = directory / "expected.json"
    expected = json.loads(path.read_text())
    manifest_path = path.with_name("manifest.json")
    manifest = json.loads(manifest_path.read_text())
    if mutation == "other_field":
        expected["compressions"][20]["action_start"] += 1
    elif mutation == "false_flag":
        expected["compressions"][20]["aed_part_first"] = False
    elif mutation == "wrong_row":
        expected["compressions"][0]["aed_part_first"] = True
    elif mutation == "raw":
        raw_path = directory / "raw.bin"
        raw = bytearray(raw_path.read_bytes())
        raw[11] ^= 1  # Firmware rate is ignored by replay, but its source hash must still match.
        raw_path.write_bytes(raw)
    path.write_bytes(golden.encode_json(expected))
    manifest["sha256"]["expected.json"] = (
        "0" * 64 if mutation == "manifest_hash" else hashlib.sha256(path.read_bytes()).hexdigest()
    )
    manifest_path.write_bytes(golden.encode_json(manifest))
    monkeypatch.setattr(golden, "DESTINATION", destination)
    before = {path: path.read_bytes() for path in destination.rglob("*") if path.is_file()}
    with pytest.raises(AssertionError):
        golden.main(["--check"])
    assert before == {path: path.read_bytes() for path in destination.rglob("*") if path.is_file()}

"""Check the unchanged DEPTH_V1r3 vectors against handwritten oracles without writing."""
import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from calculators.action_evaluator import ActionEvaluator
from config.constants import ACTION_TYPE_AED, ACTION_TYPE_COMP
from services.config import Config
from services.http.schemas import DEFAULT_CONDITION
from services.preparers import make_pre_action_list, parse_data, prepare_data


DESTINATION = ROOT / "tests/fixtures/depth_boundary_v1"
SPEC = "DEPTH_V1r3"
BOUNDARY_AMENDMENT = "2026-10-28: count Record starts next action; action_end is exclusive"
TIMESTAMP = 1_700_000_000_000
CONDITION = {**DEFAULT_CONDITION, "training_type": "compression_only"}
COUNTER_VARIANTS = ("zero", "every_record", "hold_jitter")
# User-provided counts, independently checked against the supplied Chart JSON.
# 1793164827 deliberately loses four shallow represses when AMP changes 25 -> 30.
ZIP_REAL_COUNTS = {
    "1793164406": 4, "1793164539": 8, "1793164827": 48,
    "1793164870": 60, "1793165252": 61, "1793165300": 60,
    "1793165346": 61, "1793165416": 60, "1793165465": 59,
    "1793165506": 62, "1793165797": 69,
}
AED_REAL_COUNTS = {"19148": 23, "19986": 29, "19994": 63, "20016": 44}
REAL_COUNTS = {**ZIP_REAL_COUNTS, **AED_REAL_COUNTS}


def compression(number, count, peak, depth, recoil, rate, start, end, window=(), hp="center", offset=0):
    """Format an explicitly specified oracle row; do not detect or score anything."""
    return {
        "id": number, "count_record": count, "peak_record": peak,
        "peak_offset": offset, "depth_raw": depth, "recoil_raw": recoil,
        "rate": rate, "action_start": start, "action_end": end,
        "hp_window": list(window), "hp": hp,
    }


def expected(records, rows):
    return {"spec": SPEC, "amp": 30, "records": records, "compressions": rows, "count": len(rows)}


def raw_records(depths, codes, variant):
    records = []
    for index, depth in enumerate(depths):
        packet = bytearray(28)
        packet[0] = 0xA8
        packet[1:11] = bytes([depth] * 10 if isinstance(depth, int) else depth)
        packet[11] = 37 if index % 2 else 211
        packet[12] = {"zero": 0, "every_record": index % 256,
                      "hold_jitter": min(255, max(index - 1, 0) * 7)}[variant]
        packet[13] = codes[index] if codes else 0
        # Deliberate firmware cycle/sequence jumps must not alter the depth detector.
        packet[18] = (index * 13) % 256
        packet[19] = (index * 7) % 256
        packet[20:28] = (TIMESTAMP + index * 50).to_bytes(8, "big")
        records.append(packet)
    return b"".join(records)


def synthetic_cases():
    """§8 handwritten records, boundaries, rates, recoil and HP windows."""
    cases = [
        ("hold_jitter", [0, 100, 101, 100, 99, 100, 101, 100, 0, 0],
         [0, 1, 1, 1, 1, 1, 1, 1, 4, 0],
         [compression(1, 8, 2, 101, 0, None, 0, 8, [1] * 7 + [4], "left")]),
        # No falling Record: only finish confirms the held peak, with the first tied peak.
        ("hold_flat", [0, 100, 100, 100], [0, 1, 1, 1],
         [compression(1, 3, 1, 100, 0, None, 0, 3, [1, 1])]),
        ("partial_recoil_x3", [0, 100, 30, 100, 30, 100, 0], [0, 1, 8, 1, 4, 1, 0], [
            compression(1, 2, 1, 100, 30, None, 0, 2, [1]),
            compression(2, 4, 3, 100, 30, 600, 2, 4, [8, 1], "right"),
            compression(3, 6, 5, 100, 0, 600, 4, 6, [4, 1], "left"),
        ]),
        ("shallow_pump", [10, 30] * 5 + [10], None, []),
        ("shallow_30_15", [30, 60] * 5 + [30], None, [
            compression(1, 2, 1, 60, 30, None, 0, 2),
            compression(2, 4, 3, 60, 30, 600, 2, 4),
            compression(3, 6, 5, 60, 30, 600, 4, 6),
            compression(4, 8, 7, 60, 30, 600, 6, 8),
            compression(5, 10, 9, 60, 0, 600, 8, 10),
        ]),
        ("start_pressed", [80, 50], None,
         [compression(1, 1, 0, 80, 0, None, 0, 1)]),
        ("end_rising_30", [0, 30], None,
         [compression(1, 1, 1, 30, 0, None, 0, 1)]),
        ("end_rising_29", [0, 29], None, []),
        # One high sample is counted because pmax, not a trimmed sum, drives detection.
        ("spike", [0, [0, 0, 0, 0, 0, 120, 0, 0, 0, 0], 0], None,
         [compression(1, 2, 1, 120, 0, None, 0, 2, offset=5)]),
        ("amp_boundary_fall_29", [100, 71], None, []),
        ("amp_boundary_fall_30", [100, 70], None,
         [compression(1, 1, 0, 100, 0, None, 0, 1)]),
        # RP -> VP -> RP: first recoil stops at VP's peak, so it is 50, not 10.
        ("two_rescuer_vp", [0, 100, 50, 110, 10, 100, 0], None, [
            compression(1, 2, 1, 100, 50, None, 0, 2),
            compression(2, 4, 3, 110, 10, 600, 2, 4),
            compression(3, 6, 5, 100, 0, 600, 4, 6),
        ]),
        ("infant_mask", [0, 100, 0, 0], [0, 0x44, 0x44, 0],
         [compression(1, 2, 1, 100, 0, None, 0, 2, [0x44, 0x44], "left")]),
    ]
    for name, depths, codes, rows in cases:
        condition = dict(CONDITION)
        vp_events = []
        if name == "infant_mask":
            condition["target"] = "infant"
        if name == "two_rescuer_vp":
            condition["is_2rescuers"] = True
            vp_events = [
                {"event": 0, "timestamp": TIMESTAMP + 99},
                {"event": 1, "timestamp": TIMESTAMP + 201},
            ]
        for variant in COUNTER_VARIANTS:
            yield f"synthetic/{name}/{variant}", raw_records(depths, codes, variant), {
                "condition": condition, "vp_event_list": vp_events,
            }, expected(len(depths), rows)


def replay(raw, inputs, aed=b""):
    """Replay the production action boundary and expose precisely the §8 schema."""
    config = Config(inputs["condition"])
    parsed = parse_data(raw, aed, config)
    actions, aed_parts = make_pre_action_list(config, parsed, inputs["vp_event_list"])
    # _split_aed_tail copies the compression's peak into its preceding AED action.
    aed_tail_peaks = {action["peak_record"] for action in actions if action["action_type"] == ACTION_TYPE_AED}
    previous_peaks = {action["peak_record"]: action["prev_peak_sample"]
                      for action in actions if action["action_type"] == ACTION_TYPE_COMP}
    prepare_data(actions, aed_parts, config)  # CountMarker resets rate at each AED part's first compression.
    evaluator = ActionEvaluator(config)
    rows = []
    for action in actions:
        if action["action_type"] != ACTION_TYPE_COMP:
            continue
        score = evaluator._evaluate_comp(action, config.border, action["actor"]).score
        row = compression(
            len(rows) + 1, action["count_record"], action["peak_record"], action["depth_raw"],
            action["recoil_raw"], score["comp_rate"]["value"] or None,
            action["action_start"], action["action_end"], action["hand_position"],
            score["hand_position"]["value"], action["peak_offset"],
        )
        if action["peak_record"] in aed_tail_peaks or (
            previous_peaks[action["peak_record"]] is not None and action["prev_peak_sample"] is None
        ):
            row["aed_part_first"] = True
        rows.append(row)
    return expected(len(parsed["rtdata_list"]), rows), actions


def real_cases():
    for session_id, count in REAL_COUNTS.items():
        directory = DESTINATION / "real" / session_id
        raw = (directory / "raw.bin").read_bytes()
        inputs = json.loads((directory / "input.json").read_text())
        assert inputs.get("aed_file") in (None, "aed.bin"), session_id
        aed = (directory / "aed.bin").read_bytes() if inputs.get("aed_file") else b""
        yield f"real/{session_id}", raw, inputs, count, aed


def encode_json(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", required=True,
                        help="Verify all oracles and committed fixtures without writing")
    parser.parse_args(argv)
    pending = []
    for name, raw, inputs, oracle in synthetic_cases():
        actual, _ = replay(raw, inputs)
        assert actual == oracle, f"Handwritten oracle mismatch: {name}\n{actual}\n{oracle}"
        pending.append((name, raw, inputs, oracle, b""))
    # The entire synthetic set must pass before checking the real-session expectations.
    for name, raw, inputs, count, aed in real_cases():
        actual, _ = replay(raw, inputs, aed)
        assert actual["count"] == count, f"Reviewed real-session count mismatch: {name}"
        pending.append((name, raw, inputs, actual, aed))
    for name, raw, inputs, oracle, aed in pending:
        files = {"raw.bin": raw, "input.json": encode_json(inputs), "expected.json": encode_json(oracle)}
        if aed:
            files["aed.bin"] = aed
        directory = DESTINATION / name
        for filename, data in files.items():
            saved = (directory / filename).read_bytes()
            assert saved == data, f"Existing vector changed; investigate instead of regenerating: {name}/{filename}"
        sha256 = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
        manifest = json.loads((directory / "manifest.json").read_text())
        assert manifest["sha256"] == sha256, directory
        assert manifest["spec"] == SPEC and manifest["amp"] == 30, directory
    assert len(pending) == 54
    print(f"Verified {len(pending)} DEPTH_V1r3 cases; no files written; 15/15 real sessions available")


if __name__ == "__main__":
    main()

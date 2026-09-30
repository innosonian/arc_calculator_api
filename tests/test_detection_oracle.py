"""Fixed independently derived examples validating the change-specific oracle."""

import ast
import json
from pathlib import Path
import hashlib

import pytest

from config.enums import Actor
from tests.detection_oracle import event_trace, expected_actions, expected_cycles, expected_totals, run_expected


def samples(volumes, counts=None, depths=None):
    counts = counts or [0] * len(volumes)
    depths = depths or [0] * len(volumes)
    return [{"ventilation_volume": [v, v], "compression_count": counts[i], "compression_depth": [depths[i]] * 10,
             "timestamp": i * 50, "compression_rate": 110, "ventilation_count": 0, "ventilation_speed": 0,
             "hand_position": 1, "is_aed_overlapped": False, "actor": Actor.REAL_PERSON}
            for i, v in enumerate(volumes)]


def test_oracle_fixed_counter_and_breath_trace_has_independent_units_and_boundaries():
    stream = samples([0, 500, 490, 490, 0, 300, 290, 290], counts=[1, 1, 1, 2, 2, 2, 2, 3])
    trace = event_trace(stream, "adult", "cpr")
    assert [(e["kind"], e["at"]) for e in trace] == [("comp", 3), ("vent", 3), ("comp", 7), ("vent", 7)]
    assert [(e["start"], e["peak"], e["confirmations"]) for e in trace if e["kind"] == "vent"] == [
        (1, 500, [2, 3]), (5, 300, [6, 7])]
    assert event_trace(stream, "adult", "ventilation_only") == [e for e in trace if e["kind"] == "vent"]
    assert len(event_trace(samples([30, 25, 25]), "infant", "ventilation_only")) == 1


def test_oracle_resets_nonconsecutive_lows_and_never_infers_eof():
    # The retained adapters' D42 rule (eof_single_confirmation=False): unchanged expectations.
    assert event_trace(samples([100, 90, 95, 90]), "adult", "ventilation_only", False) == []
    for option in (False, True):
        trace = event_trace(samples([100, 90, 95, 90, 90]), "adult", "ventilation_only", option)
        assert len(trace) == 1 and trace[0]["confirmations"] == [3, 4]
    assert event_trace(samples([100] * 100 + [90]), "adult", "ventilation_only", False) == []
    assert event_trace(samples(([40] * 6 + [0]) * 10), "adult", "ventilation_only", False) == []


def test_oracle_recognizes_an_open_candidate_at_the_end_of_the_file():
    # D138 (the default): an open candidate counts once (a) when its last packet is one
    # observed confirmation, or (b) with no low packet, when it rose at least the drop
    # threshold above the packet before it. Length is never the evidence.
    def summary(volumes, target="adult"):
        return [(e["start"], e["at"], e["stop"], e["peak"], e["peak_at"], e["confirmations"])
                for e in event_trace(samples(volumes), target, "ventilation_only")]

    # (a)
    assert summary([100, 90, 95, 90]) == [(0, 3, 4, 100, 0, [3])]
    assert summary([100] * 100 + [90]) == [(0, 100, 101, 100, 0, [100])]
    assert summary(([40] * 6 + [0]) * 10) == [(0, 69, 70, 40, 0, [69])]
    assert summary([100, 90]) == [(0, 1, 2, 100, 0, [1])]
    assert summary([30, 25], "infant") == [(0, 1, 2, 30, 0, [1])]
    # (b): at the peak, on a plateau, still rising, after a recovery, one unit short of a confirmation
    assert summary([100]) == [(0, 0, 1, 100, 0, [])]
    assert summary([100] * 30) == [(0, 29, 30, 100, 0, [])]
    assert summary([100, 90, 95]) == [(0, 2, 3, 100, 0, [])]
    assert summary([50, 80, 100]) == [(0, 2, 3, 100, 2, [])]
    assert summary([0, 250, 380, 440, 480]) == [(1, 4, 5, 480, 4, [])]
    assert summary([100, 91]) == [(0, 1, 2, 100, 0, [])]
    assert summary([30, 26], "infant") == [(0, 1, 2, 30, 0, [])]
    # A breath already confirmed by two packets is not added again at the end.
    assert summary([100, 90, 90]) == summary([100, 90, 90, 80]) == [(0, 2, 3, 100, 0, [1, 2])]
    # A rise below the threshold is not a breath, with or without a return to zero.
    assert summary([9]) == summary([9, 0]) == summary([5, 9, 9]) == []
    assert summary([4], "infant") == summary([4, 0], "infant") == []
    assert summary([10]) == [(0, 0, 1, 10, 0, [])] and summary([5], "infant") == [(0, 0, 1, 5, 0, [])]
    # After a confirmed breath rearmed at a non-zero level the baseline is that level.
    depths = [0, 0, 0, 1, 1, 1]
    below = event_trace(samples([100, 90, 90, 80, 85, 89], depths=depths), "adult", "ventilation_only")
    at = event_trace(samples([100, 90, 90, 80, 85, 90], depths=depths), "adult", "ventilation_only")
    assert [(e["start"], e["at"]) for e in below] == [(0, 2)]
    assert [(e["start"], e["at"], e["peak"], e["confirmations"]) for e in at] == [(0, 2, 100, [1, 2]), (4, 5, 90, [])]


def test_oracle_does_not_borrow_subthreshold_or_prior_candidate_peak():
    stream = samples([2, 0, 30, 25, 25, 0, 10, 5, 5])
    assert [(e["start"], e["peak"], e["at"]) for e in event_trace(stream, "infant", "ventilation_only")] == [
        (2, 30, 4), (6, 10, 8)]


def test_oracle_time_union_uses_endpoint_integration_and_one_compression_credit():
    comp = {"action_type": "comp", "_elapsed_interval": (0, 2000), "total_action_ms": 2000, "handsoff_ms": 1000}
    vent = {"action_type": "vent", "_elapsed_interval": (1000, 2000), "total_action_ms": 1000, "handsoff_ms": 1000}
    assert expected_totals([comp, vent]) == (2000, 1000)
    assert expected_totals([comp, dict(vent, _elapsed_interval=(2000, 3000))]) == (3000, 2000)


def test_oracle_atomic_same_packet_cycles_are_fixed_before_scoring():
    stream = samples([0, 100, 90, 90, 0, 80, 70, 70], counts=[0, 0, 0, 1, 1, 1, 1, 2])
    actions = expected_actions(stream, "adult", "cpr")
    expected_cycles(None, [{"action_list": actions}])
    assert [(a["action_type"], a["cycle_cnt"], a["compression_count"], a["ventilation_count"]) for a in actions] == [
        ("comp", 1, 1, 0), ("vent", 1, 1, 1), ("comp", 2, 1, 0), ("vent", 2, 1, 1)]


def test_recorded_infant_tail_has_only_one_confirmation_and_twenty_complete_breaths():
    from data_handlers.data_parser import DataParser
    from services.config import Config
    from tests.request_support import condition

    source = Path(__file__).parent / "dataset/vo_1.bin"
    packets = DataParser().parse_cpr_bytes(source.read_bytes(), Config(condition("infant", "ventilation_only")))
    assert len(packets) == 1065
    volumes = [max(packet["ventilation_volume"]) for packet in packets]
    assert volumes[1035] == 0 and volumes[1036] == 2
    assert max(volumes[1036:1064]) == 43
    assert volumes[1058:] == [43, 43, 43, 43, 43, 43, 0]
    # Retained D42 rule: the cut 21st breath stays unconfirmed.
    events = event_trace(packets, "infant", "ventilation_only", False)
    assert len(events) == 20
    assert events[-1]["confirmations"] == [1005, 1006]
    # D138: its one recorded low packet (43 -> 0, the last packet) recognizes it.
    current = event_trace(packets, "infant", "ventilation_only")
    assert current[:20] == events and len(current) == 21
    assert (current[-1]["start"], current[-1]["at"], current[-1]["stop"], current[-1]["peak"],
            current[-1]["confirmations"]) == (1036, 1064, 1065, 43, [1064])


def test_recorded_fourth_breath_cannot_reuse_previous_confirmation_volume():
    from data_handlers.data_parser import DataParser
    from services.config import Config
    from tests.request_support import condition

    source = Path(__file__).parent / "dataset/cpr_4.bin"
    packets = DataParser().parse_cpr_bytes(source.read_bytes(), Config(condition("adult", "cpr")))
    volumes = [max(packet["ventilation_volume"]) for packet in packets]
    assert volumes[701] == 560  # belongs to the previous breath
    assert volumes[739:750] == [0, 100, 200, 240, 340, 380, 440, 440, 440, 380, 300]
    vents = [event for event in event_trace(packets, "adult", "cpr") if event["kind"] == "vent"]
    assert (vents[3]["start"], vents[3]["at"], vents[3]["peak"]) == (740, 749, 440)
    assert [event["peak"] for event in vents] == [700, 800, 700, 440, 660, 600]
    assert sum(event["peak"] for event in vents) // len(vents) == 650


def test_expected_pipeline_never_uses_candidate_detector_or_action_builder(monkeypatch):
    from data_handlers.detection import PacketActionDetector
    from data_handlers.action_data import ActionDataPrepare
    from tests._synth import vo_session
    from tests.request_support import condition

    def forbidden(*args, **kwargs):
        pytest.fail("Expected values must not call candidate detection/preparation.")
    monkeypatch.setattr(PacketActionDetector, "detect", forbidden)
    monkeypatch.setattr(ActionDataPrepare, "generate_action_rtdata_list", forbidden)
    result, chart = run_expected(vo_session(8), b"", condition("adult", "ventilation_only"))
    assert result["action_count"] == {"comp": 0, "vent": 8}
    assert len(chart["cpr_data_set"]) == 8


def test_legacy_inputs_goldens_and_unchanged_scoring_sources_keep_original_hashes():
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / "tests/fixtures/detection_revision/provenance.json").read_text())
    for path, expected in manifest["preserved_sha256"].items():
        assert hashlib.sha256((root / path).read_bytes()).hexdigest() == expected, path
    for path, expected in manifest["preserved_ast_sha256"].items():
        syntax = ast.dump(ast.parse((root / path).read_text()), include_attributes=False)
        assert hashlib.sha256(syntax.encode()).hexdigest() == expected, path
    # The oracle deliberately reuses historical scoring. Pin its unchanged
    # formulas independently of the modified time-source wrappers so it cannot
    # silently agree with an unintended scoring/coaching change in production.
    for key in ("preserved_function_ast_sha256", "preserved_function_tail_ast_sha256"):
        for path, expected_functions in manifest[key].items():
            tree = ast.parse((root / path).read_text())
            functions = {f"{cls.name}.{function.name}": function for cls in tree.body if isinstance(cls, ast.ClassDef)
                         for function in cls.body if isinstance(function, ast.FunctionDef)}
            functions.update({function.name: function for function in tree.body if isinstance(function, ast.FunctionDef)})
            for name, expected in expected_functions.items():
                function = functions[name]
                if isinstance(expected, dict):
                    function = ast.Module(body=function.body[expected["skip_statements"]:], type_ignores=[])
                    expected = expected["sha256"]
                syntax = ast.dump(function, include_attributes=False)
                assert hashlib.sha256(syntax.encode()).hexdigest() == expected, (path, name)

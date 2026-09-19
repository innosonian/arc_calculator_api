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
    assert event_trace(samples([100, 90, 95, 90]), "adult", "ventilation_only") == []
    trace = event_trace(samples([100, 90, 95, 90, 90]), "adult", "ventilation_only")
    assert trace[0]["confirmations"] == [3, 4]
    assert event_trace(samples([100] * 100 + [90]), "adult", "ventilation_only") == []
    assert event_trace(samples(([40] * 6 + [0]) * 10), "adult", "ventilation_only") == []


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
    from tests.test_detection_completion import condition

    source = Path(__file__).parent / "dataset/vo_1.bin"
    packets = DataParser().parse_cpr_bytes(source.read_bytes(), Config(condition("infant", "ventilation_only")))
    assert len(packets) == 1065
    volumes = [max(packet["ventilation_volume"]) for packet in packets]
    assert volumes[1035] == 0 and volumes[1036] == 2
    assert max(volumes[1036:1064]) == 43
    assert volumes[1058:] == [43, 43, 43, 43, 43, 43, 0]
    events = event_trace(packets, "infant", "ventilation_only")
    assert len(events) == 20
    assert events[-1]["confirmations"] == [1005, 1006]


def test_recorded_fourth_breath_cannot_reuse_previous_confirmation_volume():
    from data_handlers.data_parser import DataParser
    from services.config import Config
    from tests.test_detection_completion import condition

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
    from tests.test_detection_completion import condition

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

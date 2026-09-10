"""Bundled binary fixtures exercise the real core through the worker adapter."""

from copy import deepcopy
import hashlib
from pathlib import Path
import uuid

import pytest

from main import run_calculator
from mock_journey import typed
from mock_journey.errors import JourneyError
from mock_journey.internal_calculator import InternalCalculator, _validate_core
from mock_journey.projection import LoadedInput, ProjectionSchema, project_input, typed_identity
from services.http.schemas import DEFAULT_CONDITION
from util.uploader import build_key_stem, date_prefix


DATA = Path(__file__).parent / "dataset"


def real_input(filename="cco_1.bin", training="compression_only", target="adult", guideline="ARC2025"):
    condition = {**DEFAULT_CONDITION, "training_type": training, "target": target, "guideline": guideline}
    body = {"cpr_b64_data": (DATA / filename).read_bytes(), "aed_b64_data": b"",
            "condition": condition, "vp_event_list": []}
    kind, required = ("compressions", 60) if training == "compression_only" else ("ventilations", 8)
    definition = {"condition": condition, "calculation_profile": {},
                  "goal": {"kind": kind, "required": required}, "catalog_version": "mock-catalog-v1",
                  "profile_version": "tester-v1", "adapter_version": "internal-v1",
                  "projection_version": "internal-projection-v1"}
    projected = project_input(body, definition, ProjectionSchema("internal-projection-v1", {}))
    binding = {"attempt_id": str(uuid.uuid4()), "epoch": str(uuid.uuid4()),
               "input_digest": typed_identity(projected), "adapter_version": "internal-v1",
               "projection_version": "internal-projection-v1", "job_id": str(uuid.uuid4()),
               "call_id": str(uuid.uuid4())}
    stem = build_key_stem()
    loaded = LoadedInput(projected, f"calculator_result/interpreted_rtdata/arc/local-validation/_no_org/{date_prefix(stem)}/{stem}")
    adapter = InternalCalculator(version="internal-v1", projection_version="internal-projection-v1", stage="local-validation")
    return body, loaded, binding, adapter


@pytest.mark.parametrize("filename,training,target", [
    ("cco_1.bin", "compression_only", "adult"),
    ("cco_1.bin", "compression_only", "child"),
    ("cco_1.bin", "compression_only", "infant"),
    ("adult_vo_1.bin", "ventilation_only", "adult"),
    ("adult_vo_1.bin", "ventilation_only", "child"),
    ("vo_1.bin", "ventilation_only", "infant"),
])
@pytest.mark.parametrize("guideline", ["ARC2020", "ARC2025", "AHA2020", "ERC2020", "STD2015"])
def test_real_bundled_calculation_preserves_every_core_value_type_and_chart(filename, training, target, guideline, monkeypatch):
    body, loaded, binding, adapter = real_input(filename, training, target, guideline)
    from data_handlers import chart_data
    charts = []
    make_chart = chart_data.make_chart_data

    def capture_chart(*args, **kwargs):
        result = make_chart(*args, **kwargs)
        charts.append(deepcopy(result))
        return result

    monkeypatch.setattr(chart_data, "make_chart_data", capture_chart)
    expected = run_calculator(body["cpr_b64_data"], b"", body["condition"], [], stage="test")
    # Default calls above skip test-stage storage. The new path must never
    # invoke even those helpers: accepted input and chart publication are owned.
    def forbidden(*args, **kwargs):
        pytest.fail("Duplicate global raw/chart storage was invoked.")
    import main
    monkeypatch.setattr(main, "upload_raw_input", forbidden)
    monkeypatch.setattr(main, "build_key_stem", forbidden)
    monkeypatch.setattr(chart_data, "upload_json_file", forbidden)
    monkeypatch.setattr(chart_data, "create_signed_url", forbidden)
    before = typed_identity(loaded.projected)
    heartbeats = []
    raw = adapter.calculate(loaded, binding, lambda: heartbeats.append(True))
    verified = adapter.validate_response(raw, loaded.projected, binding)
    chart = adapter.get_chart(verified, binding, lambda: heartbeats.append(True))
    assert typed.canonical_bytes(verified.core_result) == typed.canonical_bytes(expected)
    assert typed.canonical_bytes(chart.data) == typed.canonical_bytes(charts[0]) == typed.canonical_bytes(charts[1])
    assert chart.source_sha256 == hashlib.sha256(typed.json_bytes(chart.data)).hexdigest()
    assert typed_identity(loaded.projected) == before
    key = "comp" if training == "compression_only" else "vent"
    assert type(verified.observed) is int and verified.observed == expected["action_count"][key]
    assert len(heartbeats) == 3
    assert "submit_arc" not in verified.core_result and "submit_hstm" not in verified.core_result


def test_reusing_adapter_does_not_share_chart_or_count_capture_between_calls():
    _, first, first_binding, adapter = real_input()
    _, second, second_binding, _ = real_input("vo_1.bin", "ventilation_only", "infant")
    first_raw = adapter.calculate(first, first_binding, lambda: None)
    second_raw = adapter.calculate(second, second_binding, lambda: None)
    one = adapter.validate_response(first_raw, first.projected, first_binding)
    two = adapter.validate_response(second_raw, second.projected, second_binding)
    assert one.goal_kind == "compressions" and two.goal_kind == "ventilations"
    one.chart_reference["data"].clear()
    with pytest.raises(JourneyError) as error:
        adapter.get_chart(one, first_binding, lambda: None)
    assert error.value.code == "CALCULATOR_CONTRACT_MISMATCH"
    assert adapter.get_chart(two, second_binding, lambda: None).data
    assert adapter.validate_response(first_raw, first.projected, first_binding).chart_reference["data"]


@pytest.mark.parametrize("guideline", ["ARC2020", "ARC2025"])
@pytest.mark.parametrize("target", ["adult", "child", "infant"])
def test_core_shape_validation_accepts_real_cpr_aed_without_choosing_completion_policy(guideline, target):
    condition = {**DEFAULT_CONDITION, "guideline": guideline, "target": target}
    result = run_calculator((DATA / "cpr_1.bin").read_bytes(), (DATA / "aed_1.bin").read_bytes(),
                            condition, [], stage="test")
    before = typed.canonical_bytes(result)
    _validate_core(result)
    assert typed.canonical_bytes(result) == before

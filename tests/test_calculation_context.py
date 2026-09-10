"""Real-binary parity and isolated side effects for internal execution hooks."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import FrozenInstanceError, asdict, replace
import hashlib
from pathlib import Path
from unittest.mock import Mock
import uuid

import pytest

import data_handlers.chart_data as charts
import main
import services.calculate_cpr as calculation
from services.calculation_context import (
    AcceptedRaw, CalculationContextError, CalculationEvidence,
    CalculationExecutionContext, CycleEvidence,
)
from scripts.verify_reference_parity import _differences
from tests._synth import cpr_session


DATASET = Path(__file__).parent / "dataset"
STEM = "CPR-ACTION-1788912000-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
CASES = [
    ("cpr_1.bin", "aed_1.bin", "adult", "cpr"),
    ("cpr_2.bin", "aed_2.bin", "adult", "cpr"),
    ("cpr_3.bin", "aed_3.bin", "adult", "cpr"),
    ("cpr_4.bin", "aed_4.bin", "adult", "cpr"),
    ("cpr_5.bin", "aed_5.bin", "adult", "cpr"),
    ("cco_1.bin", None, "adult", "compression_only"),
    ("vo_1.bin", None, "infant", "ventilation_only"),
    ("adult_vo_1.bin", None, "adult", "ventilation_only"),
]


def condition(target="adult", training="cpr", guideline="ARC2025"):
    return {"mode": "training", "target": target, "training_type": training,
            "guideline": guideline, "cpr_cycle_type": "152" if target == "infant" else "302",
            "is_2rescuers": False}


def receipt(cpr, aed=b"", **updates):
    result = AcceptedRaw(hashlib.sha256(cpr).hexdigest(), len(cpr),
                         hashlib.sha256(aed).hexdigest(), len(aed), STEM, "_no_org")
    return replace(result, **updates) if updates else result


def context(cpr, aed=b"", *, observer=None, publisher=None, raw=None):
    return CalculationExecutionContext(
        accepted_raw=raw or receipt(cpr, aed),
        observe=observer if observer is not None else lambda _: None,
        publish_chart=publisher if publisher is not None else lambda _: None,
    )


def forbid(*args, **kwargs):
    pytest.fail("Internal context must not use the legacy upload/sign path")


def no_uploads(monkeypatch):
    monkeypatch.setattr(main, "build_key_stem", forbid)
    monkeypatch.setattr(main, "upload_raw_input", forbid)
    monkeypatch.setattr(charts, "upload_json_file", forbid)
    monkeypatch.setattr(charts, "create_signed_url", forbid)


@pytest.mark.parametrize("cpr_name,aed_name,target,training", CASES)
def test_recorded_binary_result_type_chart_and_count_parity(monkeypatch, cpr_name, aed_name, target, training):
    cpr = (DATASET / cpr_name).read_bytes()
    aed = (DATASET / aed_name).read_bytes() if aed_name else b""
    chart_baseline = []
    default_upload = Mock(side_effect=lambda chart, **_: chart_baseline.append(deepcopy(chart)))
    default_raw = Mock()
    monkeypatch.setattr(charts, "upload_json_file", default_upload)
    monkeypatch.setattr(charts, "create_signed_url", lambda key, **_: None)
    monkeypatch.setattr(main, "upload_raw_input", default_raw)
    kwargs = {"condition": condition(target, training), "stage": "prod"}
    baseline = main.run_calculator(cpr, aed, **kwargs)
    assert default_raw.call_count == default_upload.call_count == 1

    no_uploads(monkeypatch)
    observed, published = [], []
    actual = main.run_calculator(cpr, aed, **kwargs, execution_context=context(
        cpr, aed, observer=observed.append, publisher=published.append))
    assert not _differences(baseline, actual)
    assert not _differences(chart_baseline, published)
    assert len(observed) == len(published) == 1
    evidence = observed[0]
    assert type(evidence) is CalculationEvidence
    assert (evidence.comp_count, evidence.vent_count) == (
        actual["action_count"]["comp"], actual["action_count"]["vent"])
    assert evidence.cycle_group_count == len(evidence.cycles)
    assert evidence.cycle_group_count == actual["training_stats"]["cycle_count"]
    assert type(evidence.cycles) is tuple
    assert all(type(cycle) is CycleEvidence for cycle in evidence.cycles)
    assert "score_vent_rate_measured" not in actual["cpr_score"]["total_score"]


@pytest.mark.parametrize("target,comp", [("adult", 90), ("child", 90), ("infant", 45)])
@pytest.mark.parametrize("delta", [-1, 0])
@pytest.mark.parametrize("vent", [5, 6])
def test_existing_arc_minimum_policy_is_unchanged(monkeypatch, target, comp, delta, vent):
    data = cpr_session([(comp + delta, vent)])
    kwargs = {"condition": condition(target), "stage": "test"}
    expected = main.run_calculator(data, b"", **kwargs)
    no_uploads(monkeypatch)
    actual = main.run_calculator(data, b"", **kwargs, execution_context=context(data))
    assert not _differences(expected, actual)


@pytest.mark.parametrize("guideline", ["ARC2020", "AHA2020", "ERC2020", "STD2015"])
def test_other_guideline_calculation_compatibility(monkeypatch, guideline):
    data = cpr_session([(30, 2), (30, 2), (30, 2)])
    kwargs = {"condition": condition(guideline=guideline), "stage": "test"}
    expected = main.run_calculator(data, b"", **kwargs)
    no_uploads(monkeypatch)
    actual = main.run_calculator(data, b"", **kwargs, execution_context=context(data))
    assert not _differences(expected, actual)


def test_observation_precedes_mutating_serializer_and_is_immutable(monkeypatch):
    data = (DATASET / "cpr_1.bin").read_bytes()
    saved, order = [], []
    original_serializer = calculation.serialize_result

    def observe(snapshot):
        order.append("observe")
        saved.append(snapshot)
        with pytest.raises(FrozenInstanceError):
            snapshot.comp_count = -1
        with pytest.raises(FrozenInstanceError):
            snapshot.cycles[0].cycle_number = -1

    def serialize(result, **kwargs):
        assert order == ["observe"]
        order.append("serialize")
        before = asdict(saved[0])
        response = original_serializer(result, **kwargs)
        assert asdict(saved[0]) == before
        return response

    monkeypatch.setattr(calculation, "serialize_result", serialize)
    no_uploads(monkeypatch)
    main.run_calculator(data, b"", condition(), stage="prod",
                        execution_context=context(data, observer=observe))
    assert order == ["observe", "serialize"]


def test_publisher_receives_detached_chart_and_controls_only_url(monkeypatch):
    data = (DATASET / "cco_1.bin").read_bytes()
    baseline = main.run_calculator(data, b"", condition(training="compression_only"), stage="test")
    source, source_before = [], []
    original = charts.make_chart_data

    def chart(*args):
        generated = original(*args)
        source.append(generated)
        source_before.append(deepcopy(generated))
        return generated

    def publish(detached):
        detached.clear()
        return "https://example.test/signed-chart"

    no_uploads(monkeypatch)
    monkeypatch.setattr(charts, "make_chart_data", chart)
    actual = main.run_calculator(data, b"", condition(training="compression_only"), stage="prod",
                                execution_context=context(data, publisher=publish))
    baseline["chart_dataset_url"] = "https://example.test/signed-chart"
    assert not _differences(baseline, actual)
    assert source == source_before


@pytest.mark.parametrize("changes", [
    {"cpr_size": True}, {"cpr_size": 0}, {"aed_size": -1}, {"aed_size": False},
    {"cpr_sha256": "secret"}, {"aed_sha256": "A" * 64},
    {"key_stem": "../../secret"}, {"key_stem": STEM + "/secret"},
    {"org": "../secret"}, {"org": "secret@example.test"}, {"org": None},
])
def test_invalid_receipt_fails_without_echo(changes):
    with pytest.raises(CalculationContextError) as caught:
        receipt(b"a", **changes)
    assert str(caught.value) == "Invalid accepted raw receipt"


@pytest.mark.parametrize("changes", [
    {"cpr_sha256": "0" * 64}, {"aed_sha256": "0" * 64}, {"cpr_size": 2}, {"aed_size": 1},
])
def test_raw_mismatch_fails_before_parsing_and_no_fallback(monkeypatch, changes):
    no_uploads(monkeypatch)
    monkeypatch.setattr(main, "parse_data", forbid)
    with pytest.raises(CalculationContextError, match="Accepted raw input mismatch"):
        main.run_calculator(b"a", b"", condition(), execution_context=context(
            b"a", raw=receipt(b"a", **changes)))


@pytest.mark.parametrize("step", ["observer", "publisher"])
def test_callback_failure_is_fixed_and_has_no_fallback(monkeypatch, capsys, step):
    data = (DATASET / "cco_1.bin").read_bytes()
    no_uploads(monkeypatch)

    def fail(_):
        raise RuntimeError("secret-token-and-private-raw-marker")

    ctx = context(data, **{step: fail})
    with pytest.raises(CalculationContextError) as caught:
        main.run_calculator(data, b"", condition(training="compression_only"), execution_context=ctx)
    assert "secret" not in str(caught.value)
    assert "secret" not in capsys.readouterr().out
    with pytest.raises(CalculationContextError, match="lifecycle"):
        ctx.begin(data, b"")


@pytest.mark.parametrize("step,value", [("observer", True), ("publisher", {}), ("publisher", 1)])
def test_wrong_callback_result_aborts(monkeypatch, step, value):
    data = (DATASET / "cco_1.bin").read_bytes()
    no_uploads(monkeypatch)
    with pytest.raises(CalculationContextError):
        main.run_calculator(data, b"", condition(training="compression_only"),
                            execution_context=context(data, **{step: lambda _: value}))


def test_single_context_claim_is_atomic():
    ctx = context(b"a")

    def begin(_):
        try:
            ctx.begin(b"a", b"")
        except CalculationContextError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(begin, range(16))) == 1


def test_parallel_contexts_keep_raw_counts_and_charts_separate(monkeypatch):
    no_uploads(monkeypatch)

    def calculate(case):
        name, target, training = case
        data = (DATASET / name).read_bytes()
        snapshots, published = [], []
        raw = receipt(data, key_stem="CPR-ACTION-1788912000-" + str(uuid.uuid4()))
        ctx = context(data, raw=raw, observer=snapshots.append, publisher=published.append)
        result = main.run_calculator(data, b"", condition(target, training), execution_context=ctx)
        with pytest.raises(CalculationContextError):
            main.run_calculator(data, b"", condition(target, training), execution_context=ctx)
        assert len(snapshots) == len(published) == 1
        assert snapshots[0].comp_count == result["action_count"]["comp"]
        assert snapshots[0].vent_count == result["action_count"]["vent"]
        return snapshots[0], published[0]

    with ThreadPoolExecutor(max_workers=2) as pool:
        comp, vent = list(pool.map(calculate, [("cco_1.bin", "adult", "compression_only"),
                                             ("vo_1.bin", "infant", "ventilation_only")]))
    assert comp[0].comp_count > 0 and comp[0].vent_count == 0
    assert vent[0].comp_count == 0 and vent[0].vent_count > 0
    assert comp[1] != vent[1]


def test_none_context_keeps_legacy_downstream_keyword_contract(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "parse_data", lambda *args: {})
    monkeypatch.setattr(main, "make_pre_action_list", lambda *args: ([], []))
    monkeypatch.setattr(main, "prepare_data", lambda *args: {})
    monkeypatch.setattr(main, "_save_raw_input", lambda *args: calls.append("raw"))

    def legacy(config, prepared, stage, *, usage, key_stem, org):
        calls.append("calculate")
        return {"old": True}

    monkeypatch.setattr(main, "make_calculate_result", legacy)
    assert main.run_calculator(b"a", b"", condition(), execution_context=None) == {"old": True}
    assert calls == ["raw", "calculate"]


def test_legacy_raw_and_chart_failures_keep_best_effort_behavior(monkeypatch):
    data = (DATASET / "cco_1.bin").read_bytes()

    def fail(*args, **kwargs):
        raise RuntimeError("legacy diagnostic failure")

    monkeypatch.setattr(main, "upload_raw_input", fail)
    monkeypatch.setattr(charts, "upload_json_file", fail)
    result = main.run_calculator(data, b"", condition(training="compression_only"), stage="prod")
    assert result["action_count"]["comp"] > 0
    assert result["chart_dataset_url"] is None

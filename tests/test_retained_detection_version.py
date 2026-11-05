"""Recover captured v2 bytes without running the new detector under an old name."""

import base64
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from mock_journey import typed
from mock_journey.contracts import (
    CURRENT_ADAPTER_VERSION, CYCLE_GOAL_ADAPTER_VERSION, EOF_VENT_ADAPTER_VERSION, PENDING_GOAL_ADAPTER_VERSION,
    RETAINED_ADAPTER_VERSIONS, RETAINED_PENDING_GOAL_ADAPTER_VERSION, ROUND2_ADAPTER_VERSION,
    adapter_features, expected_profile_version,
)
from mock_journey.errors import JourneyError
from mock_journey.assembly import internal_calculator
from mock_journey.execution_definitions import execution_catalog
from mock_journey.internal_calculator import InternalCalculator
from mock_journey.jobs import DynamoJobRepository
from mock_journey.projection import ProjectedInput, LoadedInput, typed_identity
from mock_journey.worker import evaluate


FIXTURE = json.loads((Path(__file__).parent / "fixtures/retained_v2_candidates.json").read_text())


def historical(row):
    projected = ProjectedInput(base64.b64decode(row["cpr_base64"]), base64.b64decode(row["aed_base64"]),
                               deepcopy(row["payload"]))
    raw = base64.b64decode(row["candidate_base64"])
    assert hashlib.sha256(raw).hexdigest() == row["candidate_sha256"]
    assert typed_identity(projected) == row["binding"]["input_digest"]
    adapter = InternalCalculator(version=RETAINED_PENDING_GOAL_ADAPTER_VERSION,
                                 projection_version=row["binding"]["projection_version"],
                                 stage="local-validation", allow_pending_cycle_goal=True)
    return projected, raw, adapter


@pytest.mark.parametrize("row", FIXTURE["cases"], ids=lambda row: row["id"])
def test_old_candidate_recovers_identical_score_count_chart_and_goal(row, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("An old candidate called the current calculator.")
    monkeypatch.setattr("main.run_calculator", forbidden)
    projected, raw, adapter = historical(row)
    before = typed.canonical_bytes(projected.payload)
    verified = adapter.validate_response(raw, projected, row["binding"])
    saved = typed.parse_json(raw)
    assert saved["counts"] == row["old_counts"]
    assert saved["counts"]["comp"] == 2  # Captured old first-positive recovery.
    assert typed.canonical_bytes(verified.core_result) == typed.canonical_bytes(saved["core_result"])
    chart = adapter.get_chart(verified, row["binding"], lambda: None)
    assert typed.canonical_bytes(chart.data) == typed.canonical_bytes(saved["chart"]["data"])
    assert chart.source_sha256 == saved["chart"]["sha256"]
    definition = projected.payload["definition"]
    assessment = evaluate(verified.core_result, definition, verified)
    checked = DynamoJobRepository.check_evaluation(assessment, {"definition_json": json.dumps(definition)})
    # The repository validates the historical pending-policy shape as well.
    assert checked == assessment and checked is not assessment
    if row["id"] == "mock-cpr":
        assert assessment["goal"]["status"] == "pending_policy"
        assert assessment["goal"]["observed"] is None
        assert assessment["program_completed"] is False
    else:
        assert assessment["goal"]["observed"] == 2
    assert typed.canonical_bytes(projected.payload) == before
    assert hashlib.sha256(raw).hexdigest() == row["candidate_sha256"]


@pytest.mark.parametrize("row", FIXTURE["cases"], ids=lambda row: row["id"])
def test_old_input_never_executes_current_core_or_changes_binding(row, monkeypatch):
    projected, raw, adapter = historical(row)
    def forbidden(*args, **kwargs):
        pytest.fail("Retained calculate must not call the core or renew a lease.")
    monkeypatch.setattr("main.run_calculator", forbidden)
    before = typed.canonical_bytes(row)
    assert adapter.can_calculate is False
    with pytest.raises(JourneyError) as rejected:
        adapter.calculate(LoadedInput(projected, row["raw_base"]), row["binding"], forbidden)
    assert rejected.value.code == "TEMPORARILY_UNAVAILABLE"
    assert typed.canonical_bytes(row) == before


def test_new_definitions_select_new_detection_version():
    execution = execution_catalog()
    assert len({CURRENT_ADAPTER_VERSION, CYCLE_GOAL_ADAPTER_VERSION, PENDING_GOAL_ADAPTER_VERSION,
                RETAINED_PENDING_GOAL_ADAPTER_VERSION}) == 4
    assert CURRENT_ADAPTER_VERSION == ROUND2_ADAPTER_VERSION == "arc-internal-detection-v6"
    assert EOF_VENT_ADAPTER_VERSION in RETAINED_ADAPTER_VERSIONS
    assert all(pair[0] == CURRENT_ADAPTER_VERSION for pair in execution.required_bindings)
    assert internal_calculator(CURRENT_ADAPTER_VERSION, projection="test-projection",
                               stage="test-stage").can_calculate is True


@pytest.mark.parametrize("version", RETAINED_ADAPTER_VERSIONS)
def test_every_retained_adapter_refuses_new_calculation_before_input_or_heartbeat(version, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("A retained adapter began new calculation work.")

    monkeypatch.setattr("main.run_calculator", forbidden)
    adapter = internal_calculator(version, projection="test-projection", stage="test-stage")
    assert adapter.can_calculate is False
    assert adapter.calculation_options.collect_gates is False
    with pytest.raises(JourneyError) as rejected:
        adapter.calculate(None, None, forbidden)
    assert rejected.value.code == "TEMPORARILY_UNAVAILABLE"


@pytest.mark.parametrize("version", RETAINED_ADAPTER_VERSIONS)
def test_retained_readers_preserve_payloads_and_reject_cross_version_bindings(version, monkeypatch):
    monkeypatch.setattr("main.run_calculator", lambda *a, **k: pytest.fail("Stored candidate reader ran the core."))
    row = next(row for row in FIXTURE["cases"] if row["id"] == "mock-compression-only")
    projected, raw, _ = historical(row)
    # Synthetic version envelopes exercise reader schemas; captured score/count/chart
    # bytes are unchanged. This is not a newly captured calculation of these versions.
    projected.payload["definition"].update(adapter_version=version, profile_version=expected_profile_version(version))
    binding = {**row["binding"], "adapter_version": version, "input_digest": typed_identity(projected)}
    candidate = typed.parse_json(raw)
    candidate.update(schema=adapter_features(version).candidate_schema, binding=binding)
    raw = typed.json_bytes(candidate)
    adapter = internal_calculator(version, projection=binding["projection_version"], stage="local-validation")
    verified = adapter.validate_response(raw, projected, binding)
    chart = adapter.get_chart(verified, binding, lambda: None)
    assert typed.canonical_bytes(verified.core_result) == typed.canonical_bytes(candidate["core_result"])
    assert verified.observed == candidate["counts"]["comp"] == 2
    assert typed.canonical_bytes(chart.data) == typed.canonical_bytes(candidate["chart"]["data"])
    assert chart.source_sha256 == candidate["chart"]["sha256"]
    for other_version in RETAINED_ADAPTER_VERSIONS:
        if other_version == version:
            continue
        other = internal_calculator(other_version, projection=binding["projection_version"], stage="local-validation")
        with pytest.raises(JourneyError) as rejected:
            other.validate_response(raw, projected, binding)
        assert rejected.value.code == "CALCULATOR_CONTRACT_MISMATCH"

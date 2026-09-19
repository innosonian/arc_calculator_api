"""Recover captured v2 bytes without running the new detector under an old name."""

import base64
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from mock_journey import typed
from mock_journey.contracts import PENDING_GOAL_ADAPTER_VERSION, RETAINED_PENDING_GOAL_ADAPTER_VERSION
from mock_journey.errors import JourneyError
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
    checked = DynamoJobRepository._evaluation(assessment, {"definition_json": json.dumps(definition)})
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
    assert PENDING_GOAL_ADAPTER_VERSION != RETAINED_PENDING_GOAL_ADAPTER_VERSION
    assert all(pair[0] == PENDING_GOAL_ADAPTER_VERSION for pair in execution.required_bindings)
    assert InternalCalculator(version=PENDING_GOAL_ADAPTER_VERSION,
                              projection_version="test-projection", stage="test-stage",
                              allow_pending_cycle_goal=True).can_calculate is True

"""Configured Relay work admission arithmetic, not AWS latency prediction."""

import json

import pytest

from mock_journey.aws_runtime import build_runtime
from mock_journey.aws_settings import AwsSettings
from mock_journey.errors import JourneyError
from tests.test_aws_runtime import configuration, environment


@pytest.mark.parametrize("mode,attempts,expected", [
    ("standard", 1, 200), ("legacy", 1, 200),
    ("standard", 2, 1400), ("legacy", 2, 1400),
    ("standard", 8, 72600), ("legacy", 8, 128600),
])
def test_sdk_retry_allowance_and_conflict_work_are_explicit(mode, attempts, expected):
    config = configuration("relay")
    config["sdk"].update(retry_mode=mode, total_max_attempts=attempts)
    config["relay"]["lease_seconds"] = 10000  # Synthetic arithmetic fixture only.
    parsed = AwsSettings.parse(json.dumps(config), "relay")
    budget = parsed.relay_budget
    assert budget.call_ms == expected
    assert budget.step_ms == 30 * expected
    assert budget.acquire_ms == 8 * expected
    assert budget.reserve_ms == 560


@pytest.mark.parametrize("lease_seconds,valid", [(7, False), (8, False), (9, True)])
def test_lease_must_exceed_whole_step_return_reserve_and_epoch_rounding(lease_seconds, valid):
    config = configuration("relay")
    # 6000ms step + (940 + 40 + 20)ms return reserve +1000ms UTC rounding =8000ms.
    config["relay"].update(lease_seconds=lease_seconds, processing_reserve_ms=940)
    if valid:
        assert AwsSettings.parse(json.dumps(config), "relay").relay_budget.step_ms == 6000
    else:
        with pytest.raises(ValueError, match="Invalid explicit AWS journey configuration"):
            AwsSettings.parse(json.dumps(config), "relay")


@pytest.mark.parametrize("mode,attempts", [("standard", 10**400), ("legacy", 10**400),
                                          ("legacy", 10**20)])
def test_unrepresentable_retry_allowance_fails_before_sdk_creation(mode, attempts):
    config = configuration("relay")
    config["sdk"].update(retry_mode=mode, total_max_attempts=attempts)
    calls = []
    with pytest.raises(JourneyError) as error:
        build_runtime("relay", environment("relay", config), client_factory=lambda *args, **kwargs: calls.append(args))
    assert error.value.code == "TEMPORARILY_UNAVAILABLE" and calls == []


def test_relay_budget_rejection_does_not_strengthen_unrelated_role_constraints():
    config = configuration("worker")
    assert AwsSettings.parse(json.dumps(config), "worker").role_settings.lease_seconds == 1
    config = configuration("relay")
    config["relay"]["lease_seconds"] = 1
    with pytest.raises(ValueError):
        AwsSettings.parse(json.dumps(config), "relay")

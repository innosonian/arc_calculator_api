"""D129 userName and D130/D131 repeat-after-pass on DynamoDB Local (real conditions and transactions)."""

import pytest

from integration_tests.worker_journey_support import store  # noqa: F401 (fixture)
from tests.repeat_after_pass_support import SCENARIOS


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_repeat_after_pass_scenario(store, name):
    SCENARIOS[name](store)

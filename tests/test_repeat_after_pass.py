"""D129 userName and D130/D131 repeat-after-pass on the in-memory journey table.

The same scenarios run on DynamoDB Local in
integration_tests/test_repeat_after_pass_dynamodb.py.
"""

import pytest

from tests.journey_support import JourneyStore
from tests.repeat_after_pass_support import SCENARIOS


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_repeat_after_pass_scenario(name):
    SCENARIOS[name](JourneyStore.memory())

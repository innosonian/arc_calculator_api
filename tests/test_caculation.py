from unittest import TestCase

import pytest

from services.http.schemas import ConditionType


@pytest.fixture(scope="module")
def condition() -> ConditionType:
    return ConditionType(
        mode="training",
        target="adult",
        training_type="cpr",
        guideline="AHA2020",
        cpr_cycle_type="302",
        is_2rescuers=False,
    )


class TestCalculation(TestCase):
    def setUp(self):
        self.condition = ConditionType(
            mode="training",
            target="adult",
            training_type="cpr",
            guideline="AHA2020",
            cpr_cycle_type="302",
            is_2rescuers=False,
        )

    def test_multi_cycle_per_part(self):
        pass

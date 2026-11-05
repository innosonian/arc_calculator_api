"""Capture the core result, chart, and immutable ARC gate evidence."""
from copy import deepcopy
import hashlib

from services.calculation_context import AcceptedRaw, CalculationExecutionContext, CalculationOptions


STEM = "CPR-ACTION-1700000000-12345678-1234-4234-9234-123456789abc"


def run_with_options(cpr, aed, condition, options, vp_events=()):
    """The core as an adapter with these options runs it: (result, chart, evidence)."""
    from main import run_calculator

    charts, observations = [], []
    context = CalculationExecutionContext(
        accepted_raw=AcceptedRaw(hashlib.sha256(cpr).hexdigest(), len(cpr), hashlib.sha256(aed).hexdigest(),
                                 len(aed), STEM, "_no_org"),
        publish_chart=lambda chart: charts.append(deepcopy(chart)), observe=observations.append,
        options=options,
    )
    result = run_calculator(cpr, aed, deepcopy(condition), deepcopy(list(vp_events)), stage="test",
                            execution_context=context)
    assert len(charts) == len(observations) == 1
    return result, charts[0], observations[0]

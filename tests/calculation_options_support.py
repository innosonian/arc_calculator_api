"""Adapter-version calculation options for tests (docs/DECISIONS.md D138, D139).

``CURRENT_OPTIONS`` is what arc-internal-detection-v5 and every context-free
direct call run with. ``RETAINED_OPTIONS`` is what the retained adapters
(arc-internal-detection-v4, arc-internal-detection-pending-v3) keep running
with: the D42 end-of-file rule and the D07/D08 minimum-quantity nulls. The
values are written out here, not read from mock_journey.contracts, so a test
using them does not silently follow a registry change.
"""

from copy import deepcopy
from functools import partial
import hashlib

from services.calculation_context import AcceptedRaw, CalculationExecutionContext, CalculationOptions


CURRENT_OPTIONS = CalculationOptions(eof_single_confirmation=True, minimum_quantity_null=False)
RETAINED_OPTIONS = CalculationOptions(eof_single_confirmation=False, minimum_quantity_null=True)
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


def use_retained_minimum_quantity_null(monkeypatch):
    """Run context-free helper paths (lambda_handler._run_trusted_calculation) under the D07/D08 policy.

    Only the documented calculate_cpr seam of services.calculate_cpr is
    replaced; the policy itself (NullPolicy) is the production one. A
    context-free call has no end-of-file option, so this selects the
    minimum-quantity policy only.
    """
    import services.calculate_cpr as module

    monkeypatch.setattr(module, "calculate_cpr", partial(module.calculate_cpr, minimum_quantity_null=True))

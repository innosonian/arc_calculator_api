"""Calculator adapter doubles shared by the journey harnesses. Never imported by runtime code.

Two kinds replace the four copies the test suites carried (E-03):

* ``ScriptedCalculator``: a test-only adapter whose values test state
  handling, not scoring. It is registered under the current adapter and
  projection versions so the Dummy Dev execution definitions resolve it; the
  candidate carries the exact call binding and validation rejects any other
  binding. Its goal status follows its ``version`` through
  contracts.expected_goal_status (the current cycle-goal adapter reports every
  goal evaluated; a subclass registered under a pending adapter version
  reports a cycles goal ``observed`` None with ``goal_status``
  "pending_policy"; an unversioned test version reports no status). The class
  attribute ``expected_measurement`` requires every call to carry exactly
  these CPR bytes. tests/journey_support.LocalCalculator is the variant
  registered under the LocalDefinitions test versions.
* ``TrackedCalculator``: the bundled InternalCalculator (real scoring) with
  call recording and explicit hooks around the actual calls. No score is
  replaced; a hook may raise to interrupt a call before it runs
  (write-request baseline, worker call-order traces) or count bindings
  (HTTP pipeline).

``ProcessKilled`` (a BaseException: nothing in the worker runs after the
call) and ``InterruptedCalculation`` are the interruption signals the flows
raise from a ``before_calculate`` hook.
"""

from copy import deepcopy
import hashlib
import json

from mock_journey.contracts import (
    CURRENT_ADAPTER_VERSION, CYCLE_GOAL_ADAPTER_VERSION, PENDING_GOAL_ADAPTER_VERSIONS, PROJECTION_VERSION,
    VerifiedCalculation, VerifiedChart, expected_goal_status,
)
from mock_journey.errors import JourneyError
from mock_journey.internal_calculator import InternalCalculator


def internal_calculator_options(version):
    """InternalCalculator keyword arguments that give ``version`` its registered meaning.

    The same selection as mock_journey.assembly.internal_calculator: pending
    adapters allow the pending cycle goal, the cycle-goal adapter is bound to
    the D136 closed-cycle resolver, any other (test) version gets neither.
    """
    if version in PENDING_GOAL_ADAPTER_VERSIONS:
        return {"allow_pending_cycle_goal": True}
    if version == CYCLE_GOAL_ADAPTER_VERSION:
        from mock_journey.cycle_goal import closed_cycle_count
        return {"cycle_goal_resolver": closed_cycle_count}
    return {}


TYPE_MARKER = [80, 80.0, None, False, "80"]


class ProcessKilled(BaseException):
    """A killed process: no worker handler or write runs after the call."""


class InterruptedCalculation(Exception):
    """A calculator error on the interrupted calls (the worker defers the job)."""


class ScriptedCalculator:
    """Only tests inject this adapter; values test state handling, not actual scoring."""

    version = CURRENT_ADAPTER_VERSION
    projection_version = PROJECTION_VERSION
    expected_measurement = None

    def __init__(self, version=None):
        if version is not None:
            self.version = version  # e.g. a retained version for a legacy fixture attempt
        self.calls = []
        self.responses = {}
        self.inputs = []
        self.overall = 90
        self.observed = None
        self.chart_kind = "no_chart"
        self.chart_calls = 0
        self.timeout = False
        self.on_calculate = lambda: None

    def calculate(self, loaded, binding, heartbeat):
        projected = loaded.projected
        if self.expected_measurement is not None:
            assert projected.cpr_bytes == self.expected_measurement
        self.inputs.append(projected.cpr_bytes)
        goal = projected.payload["definition"]["goal"]
        if expected_goal_status(goal["kind"], self.version) == "pending_policy":
            observed = None
        else:
            observed = goal["required"] if self.observed is None else self.observed
        self.calls.append(deepcopy(binding))
        raw = json.dumps({"binding": binding, "score": self.overall, "observed": observed}).encode()
        self.responses[binding["call_id"]] = raw
        self.on_calculate()
        if self.timeout:
            raise TimeoutError("PRIVATE-CALCULATION-MARKER")
        return raw

    def validate_response(self, raw, projected, binding):
        value = json.loads(raw)
        if value["binding"] != binding:
            raise JourneyError("CALCULATOR_CONTRACT_MISMATCH")
        kind = projected.payload["definition"]["goal"]["kind"]
        core = {"cpr_score": {"total_score": {"overall": value["score"], "score_rescue_vent": None}},
                "metrics": {"VentilationSpeed": {"%_Good": 100}}, "action_count": {"comp": 90, "vent": 6},
                "training_stats": {"cycle_count": 3, "elapsed_seconds": 90}, "guide_prompts": ["test-only coaching"],
                "type_preservation": list(TYPE_MARKER)}
        goal_status = expected_goal_status(kind, self.version)
        return VerifiedCalculation(core, kind, value["observed"], self.chart_kind,
                                   "test-bound-chart" if self.chart_kind == "snapshot" else None,
                                   goal_status=goal_status)

    def get_chart(self, verified, binding, heartbeat):
        self.chart_calls += 1
        data = {"generation": self.chart_calls, "values": [1, 1.0, None]}
        return VerifiedChart(data, hashlib.sha256(json.dumps(data).encode()).hexdigest())


def _nothing(*args):
    return None


class TrackedCalculator(InternalCalculator):
    """Real bundled scoring. Hooks surround the actual calls; no score is replaced.

    ``before_calculate(loaded, binding)`` runs before the real calculation (it
    may raise to interrupt the call), ``after_calculate()`` after it,
    ``on_validate(raw, projected, binding)`` before the real validation and
    ``on_chart(verified, binding)`` before the real chart. ``calls`` records
    ``(binding, raw)`` of every completed calculation; ``chart_calls`` counts
    chart requests. The hooks are plain attributes, so a test may replace one
    after construction.
    """

    def __init__(self, *, stage="development", version=CURRENT_ADAPTER_VERSION,
                 projection_version=PROJECTION_VERSION, before_calculate=None, after_calculate=None,
                 on_validate=None, on_chart=None):
        super().__init__(version=version, projection_version=projection_version, stage=stage,
                         **internal_calculator_options(version))
        self.calls = []
        self.chart_calls = 0
        self.before_calculate = before_calculate or _nothing
        self.after_calculate = after_calculate or _nothing
        self.on_validate = on_validate or _nothing
        self.on_chart = on_chart or _nothing

    def calculate(self, loaded, binding, heartbeat):
        self.before_calculate(loaded, binding)
        raw = super().calculate(loaded, binding, heartbeat)
        self.calls.append((deepcopy(binding), raw))
        self.after_calculate()
        return raw

    def validate_response(self, raw, projected, binding):
        self.on_validate(raw, projected, binding)
        return super().validate_response(raw, projected, binding)

    def get_chart(self, verified, binding, heartbeat):
        self.chart_calls += 1
        self.on_chart(verified, binding)
        return super().get_chart(verified, binding, heartbeat)

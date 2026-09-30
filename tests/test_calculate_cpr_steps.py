"""Characterization of make_calculate_result's step logging and error flow.

Expected sequences are written from the existing diagnostic contract
(calc_start → calc_complete → serialize_complete → chart_complete →
calc_response_complete; one calc_failed with the failing step, the same
exception re-raised). The module attributes calculate_cpr, serialize_result,
add_chart_data and _log are seams that tests and scripts/viewer_common.py
replace, so they are patched on the module after import.
"""

import pytest

import services.calculate_cpr as module


CONDITION = {"guideline": "ARC2020", "target": "adult"}
PREPARED = {
    "prepared_cpr_data": [1, 2], "prepared_aed_data": [3], "whole_cpr_action_list": [4, 5, 6],
    "comp_count": 7, "vent_count": 8,
}
# start, calc start/end, serialize start/end, chart start/end, response end
CLOCK = [10.0, 10.5, 11.0, 12.0, 12.25, 13.0, 13.125, 14.0]


class Config:
    condition = CONDITION


class Context:
    def __init__(self, calls, fail=None):
        self.calls, self.fail = calls, fail

    def observe_calculation(self, result):
        self.calls.append(("observe", result))
        if self.fail:
            raise self.fail


@pytest.fixture
def harness(monkeypatch):
    calls, logs, clock = [], [], iter(CLOCK)
    failures = {}

    def step(name, value):
        def run(*args, **kwargs):
            calls.append((name, args, kwargs))
            if name in failures:
                raise failures[name]
            return value
        return run

    monkeypatch.setattr(module, "time", type("Clock", (), {"time": staticmethod(lambda: next(clock))}))
    monkeypatch.setattr(module, "calculate_cpr", step("calculate_cpr", {"calculated": True}))
    monkeypatch.setattr(module, "serialize_result", step("serialize_result", {"serialized": True}))
    monkeypatch.setattr(module, "add_chart_data", step("add_chart_data", {"charted": True}))
    monkeypatch.setattr(module, "_log", lambda level, message, **fields: logs.append((level, message, list(fields.items()))))
    return calls, logs, failures


START_LOG = ("info", "calc_start", [
    ("stage", "test"), ("prepared_cpr_count", 2), ("prepared_aed_count", 1), ("whole_action_count", 3),
    ("comp_count", 7), ("vent_count", 8),
])


def test_success_sequence_fields_and_elapsed_windows(harness):
    calls, logs, _ = harness
    context, config = Context(calls), Config()
    result = module.make_calculate_result(config, PREPARED, "test", usage={"u": 1}, key_stem="k", org="o",
                                          execution_context=context)
    assert result == {"charted": True}
    assert calls == [
        ("calculate_cpr", (PREPARED, config), {}),
        ("observe", {"calculated": True}),
        ("serialize_result", ({"calculated": True},), {"condition": CONDITION, "usage": {"u": 1}}),
        ("add_chart_data", ({"serialized": True}, PREPARED, {"calculated": True}, "test"),
         {"key_stem": "k", "org": "o", "execution_context": context}),
    ]
    assert logs == [
        START_LOG,
        ("info", "calc_complete", [("elapsed_ms", 500)]),
        ("info", "serialize_complete", [("elapsed_ms", 250)]),
        ("info", "chart_complete", [("elapsed_ms", 125)]),
        ("info", "calc_response_complete", [("elapsed_ms", 4000)]),
    ]


def test_without_context_chart_call_has_no_context_keyword(harness):
    calls, _, _ = harness
    module.make_calculate_result(Config(), PREPARED, "test")
    assert [name for name, *_ in calls] == ["calculate_cpr", "serialize_result", "add_chart_data"]
    assert calls[1][2] == {"condition": CONDITION, "usage": None}
    assert calls[2][2] == {"key_stem": None, "org": None}


@pytest.mark.parametrize("failing,completed", [
    ("calculate_cpr", []),
    ("serialize_result", ["calc_complete"]),
    ("add_chart_data", ["calc_complete", "serialize_complete"]),
])
def test_step_failure_logs_once_and_reraises_the_same_exception(harness, failing, completed):
    calls, logs, failures = harness
    failure = failures[failing] = LookupError("private detail")
    with pytest.raises(LookupError) as raised:
        module.make_calculate_result(Config(), PREPARED, "test")
    assert raised.value is failure
    assert calls[-1][0] == failing
    assert [message for _, message, _ in logs] == ["calc_start", *completed, "calc_failed"]
    assert logs[-1] == ("error", "calc_failed", [
        ("step", failing), ("error_type", "LookupError"), ("exception", failure),
    ])


def test_observer_failure_is_reported_as_the_calculation_step(harness):
    calls, logs, _ = harness
    failure = RuntimeError("observer")
    with pytest.raises(RuntimeError) as raised:
        module.make_calculate_result(Config(), PREPARED, "test", execution_context=Context(calls, failure))
    assert raised.value is failure
    assert [name for name, *_ in calls] == ["calculate_cpr", "observe"]
    assert logs[1:] == [("error", "calc_failed", [
        ("step", "calculate_cpr"), ("error_type", "RuntimeError"), ("exception", failure),
    ])]


def test_default_stage_and_missing_prepared_counts():
    import inspect

    signature = inspect.signature(module.make_calculate_result)
    assert signature.parameters["stage"].default == "prod"
    assert signature.parameters["execution_context"].kind is inspect.Parameter.KEYWORD_ONLY


def test_missing_prepared_lists_are_logged_as_zero(harness):
    _, logs, _ = harness
    module.make_calculate_result(Config(), {}, "dev")
    assert logs[0] == ("info", "calc_start", [
        ("stage", "dev"), ("prepared_cpr_count", 0), ("prepared_aed_count", 0), ("whole_action_count", 0),
        ("comp_count", None), ("vent_count", None),
    ])


def test_calc_failed_diagnostic_stacktrace_starts_at_make_calculate_result(monkeypatch):
    """Real write_diagnostic path (no _log seam): the stored frames are the ones the
    original per-step try/except blocks stored - make_calculate_result first, then the
    failing callee - with no helper or context-manager frame in between."""
    from services.operational_logs import _STACK_FRAME, log_context

    def boom(prepared, config):
        raise LookupError("private detail")

    records = []

    class Recorder:
        def record(self, category, name, fields):
            records.append((category, name, fields))

    monkeypatch.setattr(module, "calculate_cpr", boom)
    with log_context(Recorder()):
        with pytest.raises(LookupError):
            module.make_calculate_result(Config(), PREPARED, "test")

    assert [(category, name) for category, name, _ in records] == [
        ("diagnostic", "calc_start"), ("diagnostic", "calc_failed"),
    ]
    failed = records[1][2]
    assert failed["level"] == "error" and failed["step"] == "calculate_cpr"
    assert failed["error_type"] == "LookupError"
    assert failed["error_message"] == "Exception details redacted."
    frames = failed["stacktrace"]
    assert all(_STACK_FRAME.fullmatch(frame) for frame in frames), frames
    assert [frame.split(" in ")[1] for frame in frames] == ["make_calculate_result", "boom"]
    assert frames[0].startswith("services/calculate_cpr.py:")
    assert frames[1] == f"tests/test_calculate_cpr_steps.py:{boom.__code__.co_firstlineno + 1} in boom"

"""S5-04 behavior-preservation baseline: every external call of JourneyWorker.process, in order.

tests/fixtures/worker_call_order/ was captured before ``JourneyWorker._process``
was split into step methods (HEAD 337cf47 + base_after_q1.patch, write-tree
f63d0540dea8af6f8ebd8fbc71285b6c68e1920f). It is a characterization baseline,
not a calculation golden: the worker must keep issuing the same jobs/storage/
adapter/recovery/lease-guard calls with the same arguments and in the same
order, and the same operational records.

  spy_traces.json     tests/worker_trace_support.py SPY_SCENARIOS: scripted
                      doubles, faults injected by call name and ordinal
                      (legacy and course jobs, candidate resume, restarts, the
                      Q4 restart-limit closure, every JourneyError/CourseError/
                      unexpected-error branch, lease loss, guard exit/final
                      renew, guard suppression).
  memory_traces.json  MEMORY_SCENARIOS: the real repositories, storage and
                      internal calculator on tests/memory_dynamodb.py and
                      MemoryS3; every DynamoDB request (reads included) merged
                      with S3 operations, adapter calls and records.

Capture method (the capture script is not kept in the repository): both
harnesses ran twice in separate processes, with PYTHONHASHSEED 1 and 987 and
with util.uploader's wall clock shifted by three days in the second run; the
canonical outputs were byte-identical before they were written. Canonical
form: tests/worker_trace_support.canonical (UUID/hex/nonce labels by first
appearance, wall-clock key parts, diagnostic durations; worker stack frames
collapse to one entry because moving code inside the worker changes them).
"""

from copy import deepcopy
import json
from pathlib import Path

import pytest

from tests.worker_trace_support import (
    CLOCK, MEMORY_SCENARIOS, RETRY_SECONDS, SPY_SCENARIOS, WORKER_FILE, canonical, first_difference,
    run_memory, run_spy, stack_shape,
)


FIXTURES = Path(__file__).parent / "fixtures" / "worker_call_order"
SPY = json.loads((FIXTURES / "spy_traces.json").read_text())
MEMORY = json.loads((FIXTURES / "memory_traces.json").read_text())


def _names(outcome):
    return [entry["call"] if "call" in entry else "log:" + entry["log"][1] for entry in outcome["trace"]]


def _entries(outcome, name):
    return [entry for entry in outcome["trace"] if entry.get("call") == name]


@pytest.fixture(scope="module")
def spy():
    return {}


def _spy(cache, name):
    if name not in cache:
        cache[name] = run_spy(name)
    return cache[name]


# -- 1. Baseline comparison -------------------------------------------------------

def test_baselines_cover_every_scenario():
    assert list(SPY) == list(SPY_SCENARIOS)
    assert list(MEMORY) == list(MEMORY_SCENARIOS)
    assert all(SPY[name]["trace"] or "raised" in SPY[name] for name in SPY)
    assert all(run["trace"] for runs in MEMORY.values() for run in runs)


@pytest.mark.parametrize("name", list(SPY_SCENARIOS))
def test_spy_call_order_matches_baseline(spy, name):
    observed = _spy(spy, name)
    expected = SPY[name]
    assert {key: value for key, value in observed.items() if key != "trace"} == \
        {key: value for key, value in expected.items() if key != "trace"}
    assert observed["trace"] == expected["trace"], first_difference(expected["trace"], observed["trace"])


@pytest.mark.parametrize("name", list(MEMORY_SCENARIOS))
def test_memory_call_order_matches_baseline(name):
    observed = run_memory(name)
    expected = MEMORY[name]
    assert [(run["run"], run.get("returned"), run.get("raised")) for run in observed] == \
        [(run["run"], run.get("returned"), run.get("raised")) for run in expected]
    for left, right in zip(expected, observed):
        assert right["trace"] == left["trace"], left["run"] + ": " + first_difference(left["trace"], right["trace"])


# -- 2. Independent expectations (hand-written, not read from the baseline) -------

@pytest.mark.parametrize("name,error", [
    ("legacy_probe_raises", "JourneyError"), ("legacy_claim_raises", "OSError"),
    ("course_failed_get_job_raises", "OSError"),
])
def test_course_probe_pre_recovery_and_claim_errors_are_not_classified(spy, name, error):
    """is_course_job, the course pre-recovery and claim stay outside the classified block."""
    outcome = _spy(spy, name)
    assert outcome.get("raised") == error and "returned" not in outcome
    assert not [entry for entry in outcome["trace"] if "log" in entry]


def test_legacy_restart_limit_closes_without_heartbeat_begin_or_calculation(spy):
    outcome = _spy(spy, "legacy_restart_limit")
    assert outcome["returned"] is True
    assert _names(outcome) == [
        "jobs.is_course_job", "jobs.claim", "log:calculation_started", "adapters.resolve", "storage.load_input",
        "storage.load_calculation", "adapter.can_calculate", "jobs.close_restart_limit", "log:calculation_failed",
    ]
    failed = outcome["trace"][-1]["log"][2]
    assert (failed["error_code"], failed["state"], failed["failure_basis"], failed["calculation_restarts"]) == (
        "CALCULATION_FAILED", "failed", "calculation_restart_limit", 5)


def test_restart_limit_closure_runs_after_the_lease_guard_exits(spy):
    for name, closure in (("legacy_restart_limit_guard", "jobs.close_restart_limit"),
                          ("course_restart_limit", "jobs.close_course_restart_limit")):
        names = _names(_spy(spy, name))
        exit_index = names.index("guard.exit")
        assert _spy(spy, name)["trace"][exit_index]["args"] == ["_RestartLimitReached"]
        assert exit_index < names.index(closure)
        assert "guard.final_renew" not in names and "jobs.finalize" not in names


def test_course_restart_limit_seals_with_fresh_inspection_evidence(spy):
    outcome = _spy(spy, "course_restart_limit")
    names = _names(outcome)
    assert names.count("recovery.inspect") == 2
    second = [index for index, name in enumerate(names) if name == "recovery.inspect"][1]
    assert names[second + 1] == "jobs.close_course_restart_limit"
    assert outcome["trace"][second + 1]["args"][3] == {"evidence": "retry_local_call", "serial": 2}


def test_refused_begin_returns_inside_the_guard_without_final_renew_or_finalize(spy):
    outcome = _spy(spy, "legacy_begin_refused")
    names = _names(outcome)
    assert outcome["returned"] is False
    assert names[-1] == "guard.exit" and outcome["trace"][-1]["args"] == [None]
    assert names.count("jobs.renew_lease") == 1 and "jobs.finalize" not in names
    assert not any(name.startswith("log:calculation_") and name != "log:calculation_started" for name in names)


def test_final_renew_uses_the_original_renewer_after_the_guard_exits(spy):
    guarded = _names(_spy(spy, "legacy_execute_snapshot_guard_final_renew"))
    at = guarded.index("guard.final_renew")
    assert guarded.index("guard.exit") == at - 1
    assert guarded[at + 1:at + 3] == ["jobs.renew_lease", "jobs.finalize"]  # not guard.heartbeat
    plain = _names(_spy(spy, "legacy_execute_snapshot_guard_plain"))
    at = plain.index("guard.exit")
    assert plain[at + 1:at + 3] == ["jobs.renew_lease", "jobs.finalize"]
    unguarded = _names(_spy(spy, "legacy_execute_no_chart"))
    assert unguarded[unguarded.index("storage.save_final") + 1] == "jobs.finalize"


def test_calculator_and_chart_receive_the_guard_heartbeat(spy):
    names = _names(_spy(spy, "course_execute_snapshot"))
    for callee in ("adapter.calculate", "adapter.get_chart"):
        at = names.index(callee)
        assert names[at + 1:at + 3] == ["guard.heartbeat", "jobs.renew_lease"]


def test_finalize_passes_completion_plan_only_when_configured(spy):
    (plain,) = _entries(_spy(spy, "legacy_execute_no_chart"), "jobs.finalize")
    (planned,) = _entries(_spy(spy, "legacy_execute_completion_plan"), "jobs.finalize")
    assert "kwargs" not in plain and len(plain["args"]) == 6
    assert planned["kwargs"] == {"completion_plan": "<completion_plan>"} and len(planned["args"]) == 6


def test_unexpected_error_writes_the_diagnostic_before_the_course_deferral(spy):
    outcome = _spy(spy, "course_unexpected_error")
    names = _names(outcome)
    assert outcome["returned"] is False
    assert names[-4:] == ["guard.exit", "log:request_failed", "jobs.defer_course_recovery",
                          "log:calculation_deferred"]
    (defer,) = _entries(outcome, "jobs.defer_course_recovery")
    assert defer["args"][3] == "TEMPORARILY_UNAVAILABLE"
    assert defer["kwargs"] == {"next_due_at": CLOCK + RETRY_SECONDS}  # no proven_local_error keyword
    diagnostic = outcome["trace"][-3]["log"][2]
    assert diagnostic["error_type"] == "TimeoutError" and diagnostic["stacktrace"][0] == WORKER_FILE
    assert "tests/worker_trace_support.py in calculate" in diagnostic["stacktrace"]


def test_proven_local_failure_is_deferred_then_closed_with_fresh_evidence(spy):
    outcome = _spy(spy, "course_proven_failure_closed")
    names = _names(outcome)
    assert outcome["returned"] is True and "log:calculation_deferred" not in names
    assert names[-3:] == ["jobs.defer_course_recovery", "recovery.inspect", "jobs.close_course_terminal"]
    (defer,) = _entries(outcome, "jobs.defer_course_recovery")
    assert defer["args"][3] == "CALCULATION_FAILED"
    assert defer["kwargs"] == {"next_due_at": CLOCK + RETRY_SECONDS, "proven_local_error": True}
    assert outcome["trace"][-1]["args"][3] == {"evidence": "close_terminal", "serial": 1}


def test_unproven_course_error_defers_without_inspection(spy):
    for name, code in (("course_unproven_after_calculation", "CALCULATOR_CONTRACT_MISMATCH"),
                       ("course_unproven_before_calculation", "STORED_INPUT_INVALID")):
        outcome = _spy(spy, name)
        (defer,) = _entries(outcome, "jobs.defer_course_recovery")
        assert defer["args"][3] == code and defer["kwargs"]["proven_local_error"] is False
        assert _names(outcome)[-2:] == ["jobs.defer_course_recovery", "log:calculation_deferred"]
        assert outcome["trace"][-1]["log"][2]["error_code"] == code


def test_legacy_local_failures_are_marked_failed_and_others_defer(spy):
    for name, code in (("legacy_load_input_invalid", "STORED_INPUT_INVALID"),
                       ("legacy_validate_not_verified", "CALCULATOR_CONTRACT_MISMATCH"),
                       ("legacy_response_assembly_fails", "CALCULATION_FAILED")):
        outcome = _spy(spy, name)
        (failed,) = _entries(outcome, "jobs.mark_failed")
        assert outcome["returned"] is True and failed["args"][3] == code
    outcome = _spy(spy, "legacy_cannot_calculate")
    assert outcome["returned"] is False and not _entries(outcome, "jobs.mark_failed")
    assert "adapter.calculate" not in _names(outcome) and "jobs.begin_calculation" not in _names(outcome)
    category, event, fields = outcome["trace"][-1]["log"]
    assert (category, event, fields["error_code"]) == ("operation", "calculation_deferred", "TEMPORARILY_UNAVAILABLE")


def test_course_pre_recovery_reopens_before_claim(spy):
    names = _names(_spy(spy, "course_failed_reopened"))
    assert names[:5] == ["jobs.is_course_job", "jobs.get_job", "recovery.inspect", "jobs.reopen_course_recovery",
                         "jobs.claim"]
    assert _spy(spy, "course_failed_reopened")["trace"][2]["args"][1] is None  # inspected without an owner


def test_memory_restart_limit_seals_once_and_then_only_reads():
    runs = {run["run"]: run for run in MEMORY["course_restart_limit_error"]}
    sealed = [entry for entry in runs["restart_limit"]["trace"] if entry.get("ddb") == "TransactWriteItems" and any(
        "terminal_seal" in action.get("Put", {}).get("Item", {}) for action in entry["request"]["TransactItems"])]
    assert len(sealed) == 1
    after = runs["after_limit"]
    assert after["returned"] is True
    assert {entry["ddb"] for entry in after["trace"]} <= {"GetItem", "TransactGetItems"}
    killed = MEMORY["course_restart_limit_killed"]
    assert all(run.get("raised") == "ProcessKilled" for run in killed if run["run"].startswith("interrupted_"))
    assert not any("log" in entry and entry["log"][2] == "calculation_deferred"
                   for run in killed if run["run"].startswith("interrupted_") for entry in run["trace"])


# -- 3. The comparison rejects changes (so a pass is evidence) ------------------

def test_comparison_rejects_a_missing_moved_or_rebound_call():
    expected = SPY["legacy_execute_no_chart"]["trace"]
    names = [entry.get("call") for entry in expected]
    first_renew = names.index("jobs.renew_lease")
    missing = expected[:first_renew] + expected[first_renew + 1:]
    assert missing != expected
    moved = deepcopy(expected)
    moved[first_renew], moved[first_renew + 1] = moved[first_renew + 1], moved[first_renew]
    assert moved != expected
    rebound = deepcopy(expected)
    finalize = next(entry for entry in rebound if entry.get("call") == "jobs.finalize")
    finalize["args"][1] = "<uuid:99>"  # another owner than the one that claimed
    assert rebound != expected
    assert "entry" in first_difference(expected, moved)


def test_a_behaviour_variant_does_not_match_the_baseline(spy):
    # Same scripted run with a completion plan: only the finalize keyword differs.
    variant = _spy(spy, "legacy_execute_completion_plan")
    baseline = SPY["legacy_execute_no_chart"]
    assert variant["trace"] != baseline["trace"]
    memory = deepcopy(MEMORY["course_practice"][0]["trace"])
    reads = [index for index, entry in enumerate(memory) if entry.get("ddb") == "GetItem"]
    del memory[reads[-1]]
    assert memory != MEMORY["course_practice"][0]["trace"]


def test_canonical_labels_follow_first_appearance_and_keep_equalities():
    first, second = "11111111-2222-4333-8444-555555555555", "66666666-7777-4888-9999-000000000000"
    digest = "ab" * 32
    value = {"b": [f"JOB#{second}", first], "a": {"owner": second, "sha256": digest, "again": digest},
             "resume_nonce": {"S": "nonce-A"}, "key": "x/2026-09-28/CPR-ACTION-1800000000-" + first}
    assert canonical(value) == {
        "b": ["JOB#<uuid:1>", "<uuid:2>"], "a": {"owner": "<uuid:1>", "sha256": "<h:1>", "again": "<h:1>"},
        "resume_nonce": {"S": "<secret:1>"}, "key": "x/<date>/CPR-ACTION-<wallclock>-<uuid:2>",
    }
    assert stack_shape(["mock_journey/worker.py:10 in _process", "mock_journey/worker.py:20 in _step",
                        "mock_journey/jobs.py:30 in claim"]) == [WORKER_FILE, "mock_journey/jobs.py in claim"]

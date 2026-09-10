"""Offline execution wiring/supervision checks; no real SDK operations."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from local_server.execution import LocalJobRunner, build_local_execution
from mock_journey.assembly import ExecutionCatalog
from mock_journey.errors import JourneyError
from mock_journey.internal_calculator import InternalCalculator
from mock_journey.jobs import JobLeaseLost
from mock_journey.settings import WorkerSettings
from tests.test_mock_assembly import NoClientCalls, configuration, definitions, schemas


def components(**overrides):
    _, bindings, settings = configuration()
    arguments = dict(
        dynamodb_client=NoClientCalls(), s3_client=NoClientCalls(), legacy_bindings=bindings,
        resume_keys={"v1": b"synthetic-test-only-key-material!!"}, current_key_version="v1",
        execution=ExecutionCatalog(definitions(), schemas()),
        adapters=[InternalCalculator(version="test-adapter", projection_version="test-projection",
                                     stage=settings.storage.stage)],
        relay_lease_seconds=60, relay_retry_seconds=5, page_size=2, max_pages=1, clock=lambda: 1000,
    )
    arguments.update(overrides)
    return settings, WorkerSettings(settings.state, settings.storage, 60, 5), arguments


def test_explicit_factory_builds_same_scope_without_sdk_calls_or_starting_worker():
    api, worker_settings, arguments = components()
    built = build_local_execution(api, worker_settings, **arguments)
    assert built.service.calculation.payload_limit == api.payload_limit
    assert built.service.state.client is built.worker.jobs.state.client is arguments["dynamodb_client"]
    assert built.service.state.table_name == built.worker.jobs.state.table_name
    assert built.service.calculation.storage.bucket == built.worker.storage.bucket
    assert built.service.calculation.storage.prefix == built.worker.storage.prefix
    assert type(built.runner) is LocalJobRunner
    assert not hasattr(built.worker, "auth")
    assert built.worker.adapters.resolve("test-adapter", "test-projection") is arguments["adapters"][0]
    assert arguments["adapters"][0].cycle_goal_resolver is None


@pytest.mark.parametrize("change", ["state", "storage", "unsupported_adapter", "stage", "duplicate", "missing_adapter", "keys"])
def test_factory_rejects_mismatched_state_storage_or_noninternal_calculator(change):
    api, worker_settings, arguments = components()
    if change == "state":
        worker_settings = replace(worker_settings, state=replace(worker_settings.state, table_name="different"))
    elif change == "storage":
        worker_settings = replace(worker_settings, storage=replace(worker_settings.storage, bucket="different"))
    elif change == "unsupported_adapter":
        arguments["adapters"] = [SimpleNamespace(version="test-adapter", projection_version="test-projection")]
    elif change == "stage":
        arguments["adapters"][0].stage = "different"
    elif change == "duplicate":
        arguments["adapters"] *= 2
    elif change == "missing_adapter":
        arguments["adapters"] = []
    elif change == "keys":
        arguments["resume_keys"] = {"v1": b"secret-marker"}
    with pytest.raises(ValueError, match="Invalid explicit local execution configuration") as error:
        build_local_execution(api, worker_settings, **arguments)
    assert "secret-marker" not in str(error.value)


def test_factory_retains_explicit_old_internal_adapter_versions():
    api, worker_settings, arguments = components()
    old = InternalCalculator(version="old", projection_version="old-projection", stage=api.storage.stage)
    arguments["adapters"].append(old)
    built = build_local_execution(api, worker_settings, **arguments)
    assert built.worker.adapters.resolve("old", "old-projection") is old


class Jobs:
    """Records dispatch ordering only; real CAS/durability is integration-tested."""

    def __init__(self, *, pending=("outbox-job",), due=("due-job",)):
        self.pending, self.due = list(pending), list(due)
        self.calls = []

    def due_outbox(self, *, limit, cursor):
        self.calls.append(("scan_outbox", limit, cursor))
        return [{"job_id": job} for job in self.pending[:limit]], None

    def due_jobs(self, *, limit, cursor):
        self.calls.append(("scan_jobs", limit, cursor))
        return [{"job_id": job} for job in self.due[:limit]], None

    def claim_outbox(self, job_id, owner, lease):
        self.calls.append(("claim", job_id))
        return "send", {"fence": 7}

    def mark_outbox_sent(self, job_id, owner, fence):
        assert fence == 7
        self.calls.append(("ack", job_id))
        self.pending.remove(job_id)

    def release_outbox(self, job_id, owner, fence, *, next_due_at, error_code):
        self.calls.append(("retry", job_id, next_due_at, error_code))


def runner(jobs, process, **overrides):
    arguments = dict(lease_seconds=60, retry_seconds=5, page_size=2, max_pages=1, clock=lambda: 1000)
    arguments.update(overrides)
    return LocalJobRunner(jobs, SimpleNamespace(process=process), **arguments)


def test_runner_uses_both_durable_wake_sources_and_acknowledges_after_worker_success():
    jobs = Jobs()

    def process(job_id):
        jobs.calls.append(("process", job_id))
        return True

    counts = runner(jobs, process).run_once()
    assert counts == {"outbox_wakes": 1, "job_wakes": 1, "failures": 0}
    assert jobs.calls.index(("process", "outbox-job")) < jobs.calls.index(("ack", "outbox-job"))
    assert ("process", "due-job") in jobs.calls


@pytest.mark.parametrize("outcome", [False, None, "true", 1, RuntimeError("secret-marker")])
def test_nonterminal_or_failed_worker_does_not_acknowledge_outbox(outcome, capsys):
    jobs = Jobs(due=())

    def process(job_id):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    assert runner(jobs, process).run_once() == {"outbox_wakes": 0, "job_wakes": 0, "failures": 1}
    assert not any(call[0] == "ack" for call in jobs.calls)
    assert ("retry", "outbox-job", 1005, "TEMPORARILY_UNAVAILABLE") in jobs.calls
    assert "secret-marker" not in capsys.readouterr().out


def test_lost_outbox_ack_lease_leaves_next_runner_able_to_reconcile_terminal_job():
    jobs = Jobs(due=())
    original_ack = jobs.mark_outbox_sent
    jobs.mark_outbox_sent = lambda *args: (_ for _ in ()).throw(JobLeaseLost())
    assert runner(jobs, lambda _: True).run_once()["failures"] == 1
    assert jobs.pending == ["outbox-job"]
    jobs.mark_outbox_sent = original_ack
    assert runner(jobs, lambda _: True).run_once()["outbox_wakes"] == 1
    assert jobs.pending == []


def test_due_job_can_resume_when_outbox_is_already_sent():
    jobs = Jobs(pending=(), due=("persisted-job",))
    called = []
    second_process = runner(jobs, lambda job: called.append(job) or True)
    assert second_process.run_once() == {"outbox_wakes": 0, "job_wakes": 1, "failures": 0}
    assert called == ["persisted-job"]


def test_repository_unavailable_exits_to_owner_without_silent_background_loop():
    jobs = Jobs()
    jobs.due_outbox = lambda **_: (_ for _ in ()).throw(JourneyError("TEMPORARILY_UNAVAILABLE"))
    stop = SimpleNamespace(is_set=lambda: False, wait=lambda _: pytest.fail("must not hide a query failure"))
    with pytest.raises(JourneyError, match="TEMPORARILY_UNAVAILABLE"):
        runner(jobs, lambda _: True).run(stop, poll_interval=0.1)


def test_runner_observes_stop_before_any_work_or_after_current_reconciliation():
    jobs = Jobs()
    calls = []
    active = runner(jobs, lambda job: calls.append(job) or True)
    active.run(SimpleNamespace(is_set=lambda: True, wait=lambda _: False), poll_interval=0.25)
    assert not calls and not jobs.calls
    waits = []
    active.run(SimpleNamespace(is_set=lambda: False, wait=lambda seconds: waits.append(seconds) or True),
               poll_interval=0.25)
    assert calls == ["outbox-job", "due-job"]
    assert waits == [0.25]


@pytest.mark.parametrize("interval", [True, False, 0, -1, "1", None, float("inf"), float("nan")])
def test_poll_interval_has_no_coercion_or_implicit_default(interval):
    with pytest.raises(ValueError):
        runner(Jobs(), lambda _: True).run(SimpleNamespace(is_set=lambda: True, wait=lambda _: False),
                                          poll_interval=interval)


@pytest.mark.parametrize("field", ["lease_seconds", "retry_seconds", "page_size", "max_pages"])
@pytest.mark.parametrize("value", [0, True])
def test_runner_requires_explicit_positive_integer_operating_limits(field, value):
    with pytest.raises(ValueError):
        runner(Jobs(), lambda _: True, **{field: value})

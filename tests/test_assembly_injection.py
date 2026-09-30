"""Characterization of the role composition wiring (S6-04, X3-14, S9-05, S7-20, S5-18, S6-11).

What each assembled role object carries after composition and before its first
invocation is written out here with independent expectations (the synthetic
AWS configuration's literal 500 ms reserve, 500 + 40 + 20 relay reserve,
adapter versions), so moving the wiring from post-construction assignment to
explicit factory arguments cannot silently change a value, an identity or the
moment it is applied.
"""

import json
from types import SimpleNamespace
import uuid

import pytest

from mock_journey import assembly
from mock_journey.assembly import ExecutionCatalog, build_course_application, build_relay, build_worker
from mock_journey.aws_runtime import AwsRoleRuntime, build_runtime
from mock_journey.contracts import (
    CURRENT_ADAPTER_VERSION, CYCLE_GOAL_ADAPTER_VERSION, PENDING_GOAL_ADAPTER_VERSION,
    RETAINED_ADAPTER_VERSIONS, RETAINED_PENDING_GOAL_ADAPTER_VERSION,
)
from mock_journey.cycle_goal import closed_cycle_count
from mock_journey.course_provider import UnavailableCourseProvider
from mock_journey.course_settings import fixture_course_settings
from mock_journey.course_storage import CourseBlobStore
from mock_journey.dispatch import OutboxRelay
from mock_journey.errors import JourneyError
from mock_journey.execution_definitions import PROJECTION_VERSION
from mock_journey.internal_calculator import InternalCalculator
from mock_journey.settings import RelaySettings, WorkerSettings
from tests.test_aws_runtime import FakeSdk, configuration as aws_configuration, context, environment
from tests.test_vcc_wiring import CLOCK, NoClientCalls, adapter, configuration, definitions, schemas
from tests.vcc_support import mapping_document


APPLICATION_ATTRIBUTES = {"course_http", "course_service", "provider", "repository", "gateway",
                          "auth", "state", "calculation", "operations"}


def course_application(**kwargs):
    objects, bindings, settings = configuration()
    return build_course_application(
        settings, dynamodb_client=NoClientCalls(), s3_client=objects, legacy_bindings=bindings,
        resume_keys={"v1": b"K" * 32}, current_key_version="v1",
        execution=ExecutionCatalog(definitions(), schemas()),
        provider=UnavailableCourseProvider(), course_settings=fixture_course_settings(),
        clock=CLOCK, mapping_document=mapping_document(), **kwargs,
    )


def local_worker(**kwargs):
    objects, bindings, settings = configuration()
    execution = ExecutionCatalog(definitions(), schemas())
    return build_worker(
        WorkerSettings(settings.state, settings.storage, 60, 5),
        dynamodb_client=NoClientCalls(), s3_client=objects, legacy_bindings=bindings,
        adapters=[adapter()], required_bindings=execution.required_bindings, clock=CLOCK, **kwargs,
    )


def local_relay(**kwargs):
    _, _, settings = configuration()
    return build_relay(RelaySettings(settings.state, "https://sqs.example.invalid/local", 30, 5, 10, 2),
                       dynamodb_client=NoClientCalls(), sqs_client=NoClientCalls(), clock=CLOCK, **kwargs)


# -- API role: one course blob store and one state, shared by construction --------------

def test_course_application_shares_one_blob_store_between_jobs_and_course_repository():
    app = course_application()
    jobs = app.calculation.jobs
    assert type(jobs.course_blobs) is CourseBlobStore
    assert jobs.course_blobs is app.repository.blob_store
    assert jobs.course_blobs.storage is app.calculation.storage
    assert set(vars(app)) == APPLICATION_ATTRIBUTES


def test_supplied_blob_store_is_the_one_jobs_and_repository_use():
    supplied = CourseBlobStore(SimpleNamespace())
    app = course_application(blob_store=supplied)
    assert app.calculation.jobs.course_blobs is supplied
    assert app.repository.blob_store is supplied


def test_course_application_auth_state_jobs_and_calculation_share_one_state():
    app = course_application()
    assert app.auth.state is app.state
    assert app.calculation.state is app.state
    assert app.calculation.jobs.state is app.state
    assert app.auth.keys == {"v1": b"K" * 32} and app.auth.current_key_version == "v1"
    assert app.auth.environment == "local-assembly"


@pytest.mark.parametrize("keys,version", [({"v1": b"short"}, "v1"), ({"v1": b"K" * 32}, "v2"), ({}, "v1")])
def test_invalid_resume_keys_still_fail_as_one_sanitized_composition_error(keys, version):
    objects, bindings, settings = configuration()
    with pytest.raises(ValueError) as error:
        build_course_application(
            settings, dynamodb_client=NoClientCalls(), s3_client=objects, legacy_bindings=bindings,
            resume_keys=keys, current_key_version=version,
            execution=ExecutionCatalog(definitions(), schemas()),
            provider=UnavailableCourseProvider(), course_settings=fixture_course_settings(), clock=CLOCK)
    assert str(error.value) == "Invalid explicit journey composition."
    assert error.value.__cause__ is None and error.value.__suppress_context__


def test_missing_state_client_with_valid_keys_is_the_same_composition_error():
    objects, bindings, settings = configuration()
    with pytest.raises(ValueError) as error:
        build_course_application(
            settings, dynamodb_client=None, s3_client=objects, legacy_bindings=bindings,
            resume_keys={"v1": b"K" * 32}, current_key_version="v1",
            execution=ExecutionCatalog(definitions(), schemas()),
            provider=UnavailableCourseProvider(), course_settings=fixture_course_settings(), clock=CLOCK)
    assert str(error.value) == "Invalid explicit journey composition."


# -- Worker and relay: local composition has no time admission --------------------------

def test_local_worker_has_no_reserve_and_keeps_its_course_recovery_and_blob_store():
    worker = local_worker()
    assert getattr(worker, "processing_reserve_ms", None) is None
    assert getattr(worker, "relay_budget", None) is None
    assert getattr(worker, "aws_runtime", None) is None
    assert worker.course_recovery is not None
    assert worker.jobs.course_recovery is worker.course_recovery
    assert type(worker.jobs.course_blobs) is CourseBlobStore
    assert worker.jobs.course_blobs.storage is worker.storage


def test_local_relay_has_no_reserve_budget_or_runtime():
    relay = local_relay()
    assert getattr(relay, "processing_reserve_ms", None) is None
    assert getattr(relay, "relay_budget", None) is None
    assert getattr(relay, "aws_runtime", None) is None
    assert relay.jobs.course_blobs is None and relay.jobs.course_recovery is None


class _EmptyJobs:
    def __init__(self):
        self.reads = []

    def due_outbox(self, *, limit, cursor):
        self.reads.append(("outbox", limit, cursor))
        return [], None

    def due_jobs(self, *, limit, cursor):
        self.reads.append(("jobs", limit, cursor))
        return [], None


def test_relay_without_reserve_never_reads_the_invocation_context():
    jobs = _EmptyJobs()
    relay = OutboxRelay(jobs, SimpleNamespace(send=lambda job_id: pytest.fail("send")),
                        lease_seconds=30, retry_seconds=5, page_size=10, max_pages=2, clock=CLOCK)
    # context=None would fail any remaining-time read; no reserve means none happens.
    assert relay.reconcile(context=None) == {"outbox_wakes": 0, "job_wakes": 0, "failures": 0}
    assert jobs.reads == [("outbox", 10, None), ("jobs", 10, None)]


def test_relay_reserve_assigned_after_construction_still_stops_the_scan():
    # Test doubles and older callers still assign the reserve after construction.
    jobs = _EmptyJobs()
    relay = OutboxRelay(jobs, SimpleNamespace(send=lambda job_id: pytest.fail("send")),
                        lease_seconds=30, retry_seconds=5, page_size=10, max_pages=2, clock=CLOCK)
    relay.processing_reserve_ms = 500
    assert relay.reconcile(context=context(remaining=500)) == {"outbox_wakes": 0, "job_wakes": 0, "failures": 0}
    assert jobs.reads == []
    assert relay.reconcile(context=context(remaining=501)) == {"outbox_wakes": 0, "job_wakes": 0, "failures": 0}
    assert jobs.reads == [("outbox", 10, None), ("jobs", 10, None)]


# -- AWS roles: values and runtime back-reference present before the first invocation ----

@pytest.mark.parametrize("role", ["api", "worker", "relay"])
def test_aws_target_is_bound_to_its_runtime_before_first_invocation(role):
    runtime = build_runtime(role, environment(role), client_factory=FakeSdk())
    target = runtime.target
    assert type(runtime) is AwsRoleRuntime
    assert target.aws_runtime is runtime
    # The invocation wrapper is the runtime's own bound method.
    assert target.invocation.__self__ is runtime
    assert target.invocation.__func__ is AwsRoleRuntime.invocation
    if role == "api":
        assert set(vars(target)) == APPLICATION_ATTRIBUTES | {"aws_runtime", "invocation"}
        assert getattr(target, "processing_reserve_ms", None) is None
        assert getattr(target, "relay_budget", None) is None
    elif role == "worker":
        assert target.processing_reserve_ms == 500 and type(target.processing_reserve_ms) is int
        assert getattr(target, "relay_budget", None) is None
    else:
        assert target.processing_reserve_ms == 500 and type(target.processing_reserve_ms) is int
        budget = target.relay_budget
        assert budget == runtime.settings.relay_budget
        assert budget.reserve_ms == 500 + 40 + 20


def test_aws_mock_invocation_wrapper_goes_through_the_bound_runtime():
    from mock_journey.aws_runtime import invocation
    runtime = build_runtime("relay", environment("relay"), client_factory=FakeSdk())
    wrong = context()
    wrong.invoked_function_arn = "arn:aws:lambda:us-east-1:111111111111:function:other"
    with pytest.raises(Exception) as error:
        with invocation(runtime.target, wrong):
            pytest.fail("Mismatched invocation ran")
    assert getattr(error.value, "code", None) == "TEMPORARILY_UNAVAILABLE"
    with invocation(SimpleNamespace(), None) as value:
        assert value is None


def test_aws_worker_reserve_defers_a_record_without_processing_it():
    from mock_journey.worker import handle
    runtime = build_runtime("worker", environment("worker"), client_factory=FakeSdk())
    event = {"Records": [{"messageId": "m1", "body": json.dumps({"job_id": str(uuid.uuid4())})}]}
    # remaining <= reserve (500): deferred before any repository read.
    assert handle(event, context(remaining=500), runtime.target) == {"batchItemFailures": [{"itemIdentifier": "m1"}]}


def test_aws_relay_budget_stops_before_acquiring_progress():
    runtime = build_runtime("relay", environment("relay"), client_factory=FakeSdk())
    # The synthetic FakeSdk DynamoDB client has no transaction methods, so any
    # progress acquisition would raise; the budget check stops first.
    assert runtime.target.reconcile(context=context(remaining=100)) == {
        "outbox_wakes": 0, "job_wakes": 0, "failures": 0}


# -- Worker adapters and required bindings ----------------------------------------------

def _captured_worker(monkeypatch, config):
    captured = {}
    original = assembly.build_worker

    def spy(settings, **kwargs):
        captured.update(kwargs)
        return original(settings, **kwargs)

    monkeypatch.setattr(assembly, "build_worker", spy)
    runtime = build_runtime("worker", environment("worker", config), client_factory=FakeSdk())
    return runtime, captured


def _adapter_rows(adapters):
    return [(type(a), a.version, a.projection_version, a.stage, a.allow_pending_cycle_goal, a.cycle_goal_resolver)
            for a in adapters]


def test_aws_worker_adapters_and_required_bindings_include_retained(monkeypatch):
    runtime, captured = _captured_worker(monkeypatch, aws_configuration("worker"))
    assert _adapter_rows(captured["adapters"]) == [
        (InternalCalculator, CURRENT_ADAPTER_VERSION, PROJECTION_VERSION, "dev", False, closed_cycle_count),
        (InternalCalculator, RETAINED_PENDING_GOAL_ADAPTER_VERSION, PROJECTION_VERSION, "dev", True, None),
        (InternalCalculator, PENDING_GOAL_ADAPTER_VERSION, PROJECTION_VERSION, "dev", True, None),
        # D138: the former current adapter is retained and still bound to the cycle rule.
        (InternalCalculator, CYCLE_GOAL_ADAPTER_VERSION, PROJECTION_VERSION, "dev", False, closed_cycle_count),
    ]
    assert tuple(captured["required_bindings"]) == (
        (CURRENT_ADAPTER_VERSION, PROJECTION_VERSION),
        (RETAINED_PENDING_GOAL_ADAPTER_VERSION, PROJECTION_VERSION),
        (PENDING_GOAL_ADAPTER_VERSION, PROJECTION_VERSION),
        (CYCLE_GOAL_ADAPTER_VERSION, PROJECTION_VERSION),
    )
    # D138/D139: each registered calculator carries its version's own options.
    assert [(a.version, a.calculation_options.eof_single_confirmation, a.calculation_options.minimum_quantity_null)
            for a in captured["adapters"]] == [
        ("arc-internal-detection-v5", True, False),
        ("arc-local-calculator-pending-v2", False, True),
        ("arc-internal-detection-pending-v3", False, True),
        ("arc-internal-detection-v4", False, True),
    ]
    assert set(runtime.target.adapters._registered) == set(captured["required_bindings"])


@pytest.mark.parametrize("execution", [
    {"current_adapter_version": "arc-internal-detection-v4", "projection_version": "arc-local-projection-v1",
     "retained_adapter_versions": ["arc-local-calculator-pending-v2", "arc-internal-detection-pending-v3"]},
    {"retained_adapter_versions": []}, None, "v4",
], ids=["v4_document", "empty_retained", "null", "string"])
def test_aws_worker_registers_the_code_registry_whatever_the_document_says(monkeypatch, execution):
    # D141 (correcting D127): the retained list of the document is ignored; the Worker
    # composes the current adapter and the registry's retained adapters, in registry order.
    config = aws_configuration("worker")
    config["execution"] = execution
    runtime, captured = _captured_worker(monkeypatch, config)
    assert [adapter.version for adapter in captured["adapters"]] == [CURRENT_ADAPTER_VERSION, *RETAINED_ADAPTER_VERSIONS]
    assert set(runtime.target.adapters._registered) == set(captured["required_bindings"])
    assert runtime.settings.execution_block_ignored is True
    assert [adapter.version for adapter in captured["adapters"]] == [
        "arc-internal-detection-v5", "arc-local-calculator-pending-v2", "arc-internal-detection-pending-v3",
        "arc-internal-detection-v4"]


def test_required_binding_for_an_unregistered_adapter_fails_worker_composition():
    objects, bindings, settings = configuration()
    with pytest.raises(ValueError) as error:
        build_worker(WorkerSettings(settings.state, settings.storage, 60, 5),
                     dynamodb_client=NoClientCalls(), s3_client=objects, legacy_bindings=bindings,
                     adapters=[adapter()], required_bindings=(("test-adapter", "test-projection"),
                                                              ("old-adapter", "test-projection")), clock=CLOCK)
    assert str(error.value) == "Invalid explicit journey composition."


# -- Explicit factory arguments (the wiring the post-assignment moved into) -------------

def test_build_relay_passes_time_admission_to_the_relay_constructor():
    budget = SimpleNamespace(call_ms=10, step_ms=1000, acquire_ms=20, reserve_ms=100)
    relay = local_relay(processing_reserve_ms=500, relay_budget=budget)
    assert relay.processing_reserve_ms == 500 and relay.relay_budget is budget


def test_outbox_relay_constructor_defaults_match_the_absent_attribute_fallback():
    relay = OutboxRelay(_EmptyJobs(), SimpleNamespace(), lease_seconds=30, retry_seconds=5, page_size=10,
                        max_pages=2, clock=CLOCK)
    assert relay.processing_reserve_ms is None and relay.relay_budget is None


def test_build_worker_applies_the_reserve_only_when_supplied():
    assert local_worker(processing_reserve_ms=500).processing_reserve_ms == 500
    assert "processing_reserve_ms" not in vars(local_worker())
    assert "processing_reserve_ms" not in vars(local_worker(processing_reserve_ms=None))


def test_worker_adapter_helpers_follow_the_retained_order():
    execution = SimpleNamespace(required_bindings=(("current", "projection"),))
    assert assembly.worker_required_bindings(execution, (), projection="projection") == (("current", "projection"),)
    assert assembly.worker_required_bindings(execution, ("old-a", "old-b"), projection="projection") == (
        ("current", "projection"), ("old-a", "projection"), ("old-b", "projection"))
    adapters = assembly.worker_adapters(
        CURRENT_ADAPTER_VERSION, (RETAINED_PENDING_GOAL_ADAPTER_VERSION, PENDING_GOAL_ADAPTER_VERSION),
        projection=PROJECTION_VERSION, stage="local")
    assert _adapter_rows(adapters) == [
        (InternalCalculator, CURRENT_ADAPTER_VERSION, PROJECTION_VERSION, "local", False, closed_cycle_count),
        (InternalCalculator, RETAINED_PENDING_GOAL_ADAPTER_VERSION, PROJECTION_VERSION, "local", True, None),
        (InternalCalculator, PENDING_GOAL_ADAPTER_VERSION, PROJECTION_VERSION, "local", True, None),
    ]
    # An unregistered version is a composition error, never a guessed meaning.
    with pytest.raises(ValueError):
        assembly.worker_adapters("arc-unregistered-v9", (), projection=PROJECTION_VERSION, stage="local")
    # The helpers are not role builders (the public build_* set is pinned elsewhere).
    assert {name for name in vars(assembly) if name.startswith("build_")} == {
        "build_course_application", "build_worker", "build_relay"}


def test_composition_modules_do_not_assign_wiring_attributes_after_construction():
    import ast
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    wiring = {"course_blobs", "course_recovery", "processing_reserve_ms", "relay_budget", "state", "aws_runtime"}
    found = []
    for name in ("mock_journey/assembly.py", "mock_journey/aws_runtime.py", "mock_journey/course_wiring.py",
                 "local_server/runtime.py"):
        for node in ast.walk(ast.parse((root / name).read_text(encoding="utf-8"))):
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                targets = [node.target]
            else:
                continue
            for target in targets:
                if (isinstance(target, ast.Attribute) and target.attr in wiring
                        and not (isinstance(target.value, ast.Name) and target.value.id == "self")):
                    found.append((name, ast.unparse(target)))
    # Remaining, explicit cases: JourneyWorker has no reserve constructor
    # argument yet, and the runtime back-reference is set by bind_target().
    assert found == [("mock_journey/assembly.py", "worker.processing_reserve_ms"),
                     ("mock_journey/aws_runtime.py", "self.target.aws_runtime")]


def test_course_mode_marker_lives_in_an_import_free_module():
    import ast
    from pathlib import Path
    from mock_journey import course_mode, course_wiring, handler
    from local_server import http as local_http
    assert course_mode.COURSE_MODE == "course_v2"
    # course_wiring re-exports the same object; the entry points compare the same value.
    assert course_wiring.COURSE_MODE is course_mode.COURSE_MODE
    assert course_wiring.CourseApplication.course_mode == "course_v2"
    assert handler._COURSE_MODE == local_http._COURSE_MODE == course_mode.COURSE_MODE
    tree = ast.parse(Path(course_mode.__file__).read_text(encoding="utf-8"))
    assert not [node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))]

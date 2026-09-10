"""Composition counterexamples; these definitions/adapters exist only in tests."""

from copy import deepcopy
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from mock_journey.assembly import ExecutionCatalog, build_application, build_worker, build_relay
from mock_journey.catalog import Catalog, PROGRAMS, TARGETS, slot_key
from mock_journey.projection import ProjectionSchema
from mock_journey.settings import StateSettings, StorageSettings, ApiSettings, WorkerSettings, RelaySettings
from mock_journey import typed
from tests.mock_storage_support import MemoryLegacyBindings, MemoryS3


def definitions():
    # Merely constructor fixtures, not a supplied execution registry.
    return {slot_key(program[0], target): {
        "condition": {"target": target}, "calculation_profile": {
            "Custom": {"PassThreshold": 80.0, "CertificateAdult": False}},
        "profile_version": "test-profile", "adapter_version": "test-adapter", "projection_version": "test-projection",
    } for program in PROGRAMS for target in TARGETS}


def schemas():
    return {"test-projection": ProjectionSchema("test-projection", {"CompressionDepth": {"value": "scalar"}})}


def adapter(version="test-adapter", projection="test-projection"):
    def forbidden(*args, **kwargs):
        pytest.fail("Role construction called the internal calculator.")
    return SimpleNamespace(version=version, projection_version=projection,
                           calculate=forbidden, validate_response=forbidden, get_chart=forbidden)


def configuration():
    objects = MemoryS3()
    bindings = MemoryLegacyBindings(objects)
    state = StateSettings("local-table", 8)
    storage = StorageSettings("development", bindings.bucket, bindings.directory, 1000000, 2000000)
    return objects, bindings, ApiSettings(state, storage, "local-assembly", 2000000)


class NoClientCalls:
    def __getattr__(self, name):
        pytest.fail("Construction accessed an SDK client operation.")


def test_roles_construct_without_sdk_calls_or_sharing_user_keys():
    objects, bindings, settings = configuration()
    client = NoClientCalls()
    execution = ExecutionCatalog(definitions(), schemas())
    keys = {"v1": b"test-only-key-material-32-bytes!!" + b"!"}
    service = build_application(settings, dynamodb_client=client, s3_client=client, legacy_bindings=bindings,
                                resume_keys=keys, current_key_version="v1", execution=execution, clock=lambda: 1000)
    worker = build_worker(WorkerSettings(settings.state, settings.storage, 60, 5),
                          dynamodb_client=client, s3_client=client, legacy_bindings=bindings,
                          adapters=[adapter()], required_bindings=execution.required_bindings, clock=lambda: 1000)
    relay = build_relay(RelaySettings(settings.state, "https://sqs.example.invalid/local", 30, 5, 10, 2),
                        dynamodb_client=client, sqs_client=client, clock=lambda: 1000)
    assert service.auth.state is service.state
    assert service.calculation.state is service.state
    assert service.calculation.jobs.state is service.state
    assert service.catalog.slot_keys == Catalog().slot_keys
    assert worker.jobs.state.client is relay.jobs.state.client is service.state.client is client
    assert worker.lease_seconds == 60 and relay.page_size == 10
    assert not hasattr(worker, "auth") and not hasattr(relay, "storage")
    keys["v1"] = b"X" * 32
    assert service.auth.keys["v1"] != keys["v1"]
    assert objects.objects == {}


def test_catalog_and_schema_snapshots_survive_caller_and_return_value_mutation():
    supplied, supplied_schemas = definitions(), schemas()
    execution = ExecutionCatalog(supplied, supplied_schemas)
    expected = typed.canonical_bytes(supplied["mock-cpr:adult"])
    supplied["mock-cpr:adult"]["calculation_profile"]["Custom"]["PassThreshold"] = 1
    supplied_schemas["test-projection"].metric_fields["CompressionDepth"]["value"] = {}
    first = execution.get_definition("mock-cpr", "adult")
    assert typed.canonical_bytes(first) == expected
    first["condition"]["target"] = "child"
    execution.schemas["test-projection"].metric_fields.clear()
    assert execution.get_definition("mock-cpr", "adult")["condition"]["target"] == "adult"
    assert execution.schemas["test-projection"].metric_fields == {"CompressionDepth": {"value": "scalar"}}
    for program in PROGRAMS:
        for target in TARGETS:
            row = json.loads(Catalog(execution).definition(program[0], target))
            assert row["goal"] == {"kind": program[2], "required": program[3]}
            assert type(row["calculation_profile"]["Custom"]["PassThreshold"]) is float
            assert row["calculation_profile"]["Custom"]["CertificateAdult"] is False


@pytest.mark.parametrize("change", [
    "missing_slot", "extra_slot", "wrong_target", "missing_field", "extra_field", "missing_schema",
    "wrong_schema_version", "invalid_schema_tree", "invalid_schema_name", "nonfinite", "surrogate", "cycle",
    "profile_credential", "condition_credential", "unknown_profile", "unknown_condition",
    "schema_key_surrogate", "retained_schema_surrogate",
])
def test_invalid_contracts_fail_before_any_client_is_available(change):
    supplied, supplied_schemas = definitions(), schemas()
    item = supplied["mock-cpr:adult"]
    if change == "missing_slot":
        supplied.pop("mock-two-rescuer-aed:infant")
    elif change == "extra_slot":
        supplied["unconfirmed:adult"] = deepcopy(item)
    elif change == "wrong_target":
        item["condition"]["target"] = "child"
    elif change == "missing_field":
        item.pop("profile_version")
    elif change == "extra_field":
        item["goal"] = {"required": 0}
    elif change == "missing_schema":
        supplied_schemas.clear()
    elif change == "wrong_schema_version":
        supplied_schemas = {"mismatched": supplied_schemas["test-projection"]}
    elif change == "invalid_schema_tree":
        supplied_schemas["test-projection"].metric_fields["CompressionDepth"] = ["scalar", "scalar"]
    elif change == "invalid_schema_name":
        supplied_schemas["test-projection"].metric_fields["UnverifiedMetric"] = {}
    elif change == "schema_key_surrogate":
        supplied_schemas["test-projection"].metric_fields["CompressionDepth"] = {"\ud800": "scalar"}
    elif change == "retained_schema_surrogate":
        supplied_schemas["\ud800"] = ProjectionSchema("\ud800", {})
    elif change == "nonfinite":
        item["calculation_profile"]["Custom"]["PassThreshold"] = float("nan")
    elif change == "surrogate":
        item["profile_version"] = "PRIVATE-MARKER-\ud800"
    elif change == "profile_credential":
        item["calculation_profile"]["client_secret"] = "PRIVATE-MARKER"
    elif change == "condition_credential":
        item["condition"]["access_token"] = "PRIVATE-MARKER"
    elif change == "unknown_profile":
        item["calculation_profile"]["Custom"]["unverified"] = 1
    elif change == "unknown_condition":
        item["condition"]["unverified"] = 1
    else:
        item["calculation_profile"]["cycle"] = item
    with pytest.raises(ValueError) as error:
        ExecutionCatalog(supplied, supplied_schemas)
    assert str(error.value) == "Invalid explicit journey composition."
    assert error.value.__suppress_context__ is True


def test_worker_requires_explicit_current_and_retained_versions_without_invoking_them():
    _, bindings, settings = configuration()
    kwargs = dict(dynamodb_client=NoClientCalls(), s3_client=NoClientCalls(), legacy_bindings=bindings,
                  required_bindings=(("test-adapter", "test-projection"), ("old-adapter", "old-projection")))
    worker_settings = WorkerSettings(settings.state, settings.storage, 60, 5)
    with pytest.raises(ValueError):
        build_worker(worker_settings, adapters=[adapter()], **kwargs)
    old = adapter("old-adapter", "old-projection")
    worker = build_worker(worker_settings, adapters=[adapter(), old], **kwargs)
    assert worker.adapters.resolve("old-adapter", "old-projection") is old
    with pytest.raises(ValueError):
        build_worker(worker_settings, adapters=[adapter(), adapter()], **kwargs)
    incomplete = adapter()
    incomplete.get_chart = None
    with pytest.raises(ValueError):
        build_worker(worker_settings, adapters=[incomplete, old], **kwargs)


@pytest.mark.parametrize("cause", ["wrong_role", "bad_key", "missing_key", "namespace", "clock", "unverified_catalog"])
def test_bad_api_wiring_never_makes_storage_or_authentication_calls(cause):
    _, bindings, settings = configuration()
    kwargs = dict(dynamodb_client=NoClientCalls(), s3_client=NoClientCalls(), legacy_bindings=bindings,
                  resume_keys={"v1": b"K" * 32}, current_key_version="v1",
                  execution=ExecutionCatalog(definitions(), schemas()))
    if cause == "wrong_role":
        settings = WorkerSettings(settings.state, settings.storage, 60, 5)
    elif cause == "bad_key":
        kwargs["resume_keys"] = {"v1": b"PRIVATE-MARKER"}
    elif cause == "missing_key":
        kwargs["current_key_version"] = "missing"
    elif cause == "namespace":
        settings = replace(settings, storage=replace(settings.storage, bucket="different-private-bucket"))
    elif cause == "clock":
        kwargs["clock"] = None
    else:
        kwargs["execution"] = Catalog()
    with pytest.raises(ValueError) as error:
        build_application(settings, **kwargs)
    assert str(error.value) == "Invalid explicit journey composition."


def test_api_schema_registry_retains_previous_declared_projection_for_resubmission():
    supplied = schemas()
    supplied["old-projection"] = ProjectionSchema("old-projection", {})
    execution = ExecutionCatalog(definitions(), supplied)
    _, bindings, settings = configuration()
    service = build_application(settings, dynamodb_client=NoClientCalls(), s3_client=NoClientCalls(),
                                legacy_bindings=bindings, resume_keys={"v1": b"K" * 32},
                                current_key_version="v1", execution=execution)
    assert set(service.calculation.schemas) == {"test-projection", "old-projection"}


@pytest.mark.parametrize("role,missing", [
    ("api", "dynamodb_client"), ("api", "s3_client"), ("api", "legacy_bindings"),
    ("worker", "dynamodb_client"), ("worker", "s3_client"), ("worker", "legacy_bindings"),
    ("relay", "dynamodb_client"), ("relay", "sqs_client"),
])
def test_missing_explicit_dependency_is_rejected_without_sdk_introspection(role, missing):
    _, bindings, settings = configuration()
    execution = ExecutionCatalog(definitions(), schemas())
    kwargs = {"dynamodb_client": NoClientCalls()}
    if role == "relay":
        factory = build_relay
        config = RelaySettings(settings.state, "https://sqs.example.invalid/local", 30, 5, 10, 2)
        kwargs["sqs_client"] = NoClientCalls()
    else:
        kwargs.update(s3_client=NoClientCalls(), legacy_bindings=bindings)
        if role == "api":
            factory, config = build_application, settings
            kwargs.update(resume_keys={"v1": b"K" * 32}, current_key_version="v1", execution=execution)
        else:
            factory = build_worker
            config = WorkerSettings(settings.state, settings.storage, 60, 5)
            kwargs.update(adapters=[adapter()], required_bindings=execution.required_bindings)
    kwargs[missing] = None
    with pytest.raises(ValueError) as error:
        factory(config, **kwargs)
    assert str(error.value) == "Invalid explicit journey composition."

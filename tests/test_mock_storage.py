"""Failure, identity, and stale-writer attacks against the artifact boundary."""

from copy import deepcopy
import hashlib
import json

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from mock_journey.errors import JourneyError
from mock_journey.projection import project_input, typed_identity
from mock_journey.storage import JourneyStorage
from mock_journey import typed
from tests.mock_storage_support import MemoryS3, MemoryLegacyBindings
from tests.test_mock_projection import measurement_fixture


def setup_storage(stage="development"):
    client = MemoryS3()
    bindings = MemoryLegacyBindings(client)
    storage = JourneyStorage(client, legacy_bindings=bindings, stage=stage,
                             limits={"input_bytes": 1_000_000, "artifact_bytes": 2_000_000})
    body, definition, schema = measurement_fixture()
    projected = project_input(body, definition, schema)
    binding = {"attempt_id": "attempt1", "epoch": "epoch1", "input_digest": typed_identity(projected),
               "adapter_version": definition["adapter_version"], "projection_version": definition["projection_version"]}
    return storage, client, bindings, projected, binding


def call_binding(binding):
    return {**binding, "job_id": "job1", "call_id": "call1"}


def test_input_roundtrip_checks_raw_manifest_and_preserves_bytes():
    store, client, _, projected, binding = setup_storage()
    saved = store.save_input(projected, binding)
    loaded = store.load_input(saved["manifest_ref"], binding)
    assert loaded.projected == projected
    assert loaded.raw_base == saved["raw_base"]
    assert sorted(key.rsplit(".", 1)[-1] for _, key in client.objects) == ["bin", "json", "json"]
    assert saved["raw_base"].startswith(store.prefix + "_no_org/")


def test_configured_version_spelling_is_not_treated_as_a_path_segment():
    store, _, _, _, binding = setup_storage()
    body, definition, schema = measurement_fixture()
    definition["adapter_version"] = "adapter-v1.0"
    definition["projection_version"] = "projection-v1.0"
    schema = type(schema)(definition["projection_version"], schema.metric_fields)
    projected = project_input(body, definition, schema)
    binding.update(input_digest=typed_identity(projected), adapter_version=definition["adapter_version"],
                   projection_version=definition["projection_version"])
    saved = store.save_input(projected, binding)
    assert store.load_input(saved["manifest_ref"], binding).projected == projected
    assert "adapter-v1.0" not in saved["raw_base"]


@pytest.mark.parametrize("suffix", [".bin", ".meta.json", ".request.json"])
def test_failure_of_any_mandatory_file_never_returns_an_accepted_manifest(suffix):
    store, client, _, projected, binding = setup_storage()
    client.fail_put_suffix = suffix
    with pytest.raises(JourneyError):
        store.save_input(projected, binding)


def test_legacy_test_stage_skip_is_not_a_durable_success():
    store, client, _, projected, binding = setup_storage("test")
    with pytest.raises(JourneyError) as raised:
        store.save_input(projected, binding)
    assert raised.value.code == "TEMPORARILY_UNAVAILABLE"
    assert not client.objects


def test_distinct_uploads_for_one_attempt_do_not_overwrite_the_winner():
    store, client, _, projected, binding = setup_storage()
    first = store.save_input(projected, binding)
    original_objects = deepcopy(client.objects)
    body, definition, schema = measurement_fixture()
    body["cpr_b64_data"] += b"different"
    second_projection = project_input(body, definition, schema)
    second = store.save_input(second_projection, {**binding, "input_digest": typed_identity(second_projection)})
    assert first["raw_base"] != second["raw_base"]
    assert all(client.objects[key] == value for key, value in original_objects.items())
    assert store.load_input(first["manifest_ref"], binding).projected.cpr_bytes == projected.cpr_bytes


@pytest.mark.parametrize("what", ["raw", "manifest", "binding"])
def test_tampered_or_rebound_input_is_rejected(what):
    store, client, _, projected, binding = setup_storage()
    saved = store.save_input(projected, binding)
    expected = binding
    if what == "binding":
        expected = {**binding, "epoch": "different"}
    else:
        key = saved["raw_base"] + (".bin" if what == "raw" else ".request.json")
        client.objects[(store.bucket, key)]["Body"] += b"corruption"
    with pytest.raises(JourneyError) as raised:
        store.load_input(saved["manifest_ref"], expected)
    assert raised.value.code == "STORED_INPUT_INVALID"


def test_calculation_candidate_can_be_found_after_put_without_a_database_ref_update():
    store, _, _, _, binding = setup_storage()
    binding = call_binding(binding)
    planned = store.planned_calculation(binding)
    assert store.load_calculation(planned, binding) is None
    result = store.save_calculation(planned, b'{"real":"wire bytes"}', binding)
    assert store.load_calculation(planned, binding) == b'{"real":"wire bytes"}'
    assert store.save_calculation(planned, b'{"real":"wire bytes"}', binding) == result
    with pytest.raises(JourneyError):
        store.save_calculation(planned, b"different-response", binding)
    with pytest.raises(JourneyError):
        store.load_calculation(planned, {**binding, "call_id": "different"})


@pytest.mark.parametrize("ref", [
    {"bucket": "another-bucket", "key": "anything"},
    {"bucket": "brayden-online-v2-api-storage", "key": "calculator_result/interpreted_rtdata/hstm/private"},
    {"bucket": "brayden-online-v2-api-storage", "key": "calculator_result/interpreted_rtdata/arc/development/../private"},
])
def test_foreign_references_cannot_be_fetched(ref):
    store, _, _, _, binding = setup_storage()
    with pytest.raises(JourneyError):
        store.load_calculation(ref, call_binding(binding))


def test_stale_chart_writer_uses_the_pinned_snapshot_even_after_fetching_another_candidate():
    store, client, _, projected, input_binding = setup_storage()
    saved = store.save_input(projected, input_binding)
    binding = call_binding(input_binding)
    candidate_a = store.save_chart_candidate({"curve": [1.0, None]}, "a" * 64, binding, 1)
    candidate_b = store.save_chart_candidate({"curve": [999]}, "b" * 64, binding, 2)
    selected = {**candidate_a, "revision": 1}
    first = store.publish_selected_chart("job1", saved["raw_base"], binding, lambda _: deepcopy(selected))
    # The stale worker holds B, but there is no API accepting that candidate as
    # authority; its next publish must read the same immutable DB selection A.
    assert candidate_b["snapshot_ref"] != selected["snapshot_ref"]
    second = store.publish_selected_chart("job1", saved["raw_base"], binding, lambda _: deepcopy(selected))
    assert first == second
    assert client.objects[(store.bucket, first["key"])]["Body"] == json.dumps({"curve": [1.0, None]}).encode()
    assert store.sign_chart(first).startswith("https://charts.example.invalid/")
    assert client.sign_calls[-1][2] == 300


def test_chart_publication_requires_the_bound_input_and_never_uses_another_raw_stem():
    store, _, _, projected, input_binding = setup_storage()
    first = store.save_input(projected, input_binding)
    other = store.save_input(projected, {**input_binding, "attempt_id": "another-attempt"})
    binding = call_binding(input_binding)
    selected = {**store.save_chart_candidate({}, "a" * 64, binding, 1), "revision": 1}
    with pytest.raises(JourneyError) as raised:
        store.publish_selected_chart("job1", other["raw_base"], binding, lambda _: selected)
    assert raised.value.code == "STORED_INPUT_INVALID"
    store.publish_selected_chart("job1", first["raw_base"], binding, lambda _: selected)


def test_chart_serialization_mismatch_is_not_silently_republished_or_changed_to_null():
    store, client, _, projected, input_binding = setup_storage()
    saved = store.save_input(projected, input_binding)
    binding = call_binding(input_binding)
    selected = {**store.save_chart_candidate({"x": 1}, "a" * 64, binding, 1), "revision": 1}
    ref = selected["snapshot_ref"]
    body = b'{"x":1}'  # Equivalent JSON, different bytes from the pinned serializer.
    checksum = hashlib.sha256(body).hexdigest()
    client.objects[(store.bucket, ref["key"])]["Body"] = body
    client.objects[(store.bucket, ref["key"])]["Metadata"]["arc-sha256"] = checksum
    selected.update(published_body_sha256=checksum)
    selected["snapshot_ref"] = {**ref, "sha256": checksum, "size": len(body)}
    with pytest.raises(JourneyError):
        store.publish_selected_chart("job1", saved["raw_base"], binding, lambda _: selected)
    assert (store.bucket, saved["raw_base"] + ".json") not in client.objects


def test_no_chart_is_an_explicit_selection_and_unset_is_not_no_chart():
    store, _, _, projected, input_binding = setup_storage()
    saved = store.save_input(projected, input_binding)
    binding = call_binding(input_binding)
    with pytest.raises(JourneyError):
        store.publish_selected_chart("job1", saved["raw_base"], binding, lambda _: {"kind": "unset", "revision": 0})
    publication = store.publish_selected_chart("job1", saved["raw_base"], binding,
                                               lambda _: {"kind": "no_chart", "revision": 1, **binding})
    assert publication == {"kind": "no_chart", "selection_revision": 1, **binding}
    assert store.sign_chart(publication) is None


def test_final_candidates_are_unique_and_bytes_are_bound_to_the_selected_chart():
    store, client, _, _, binding = setup_storage()
    binding = call_binding(binding)
    publication = {"kind": "no_chart", "selection_revision": 1, **binding}
    result = b'{"integer": 80, "float": 80.0, "value": null, "chart_dataset_url": null}'
    first = store.save_final(result, binding, 1, publication)
    second = store.save_final(result, binding, 2, publication)
    assert first["key"] != second["key"]
    assert store.read_final(first, binding, publication) == result
    with pytest.raises(JourneyError):
        store.read_final(first, binding, {**publication, "selection_revision": 2})
    client.objects[(store.bucket, first["key"])]["Metadata"]["arc-chart"] = typed.digest({"different": True})
    with pytest.raises(JourneyError):
        store.read_final(first, binding, publication)


def test_artifact_kind_cannot_be_confused_even_with_the_same_call_binding():
    store, _, _, _, binding = setup_storage()
    binding = call_binding(binding)
    publication = {"kind": "no_chart", "selection_revision": 1, **binding}
    final = store.save_final(b'{"existing": 1}', binding, 1, publication)
    with pytest.raises(JourneyError):
        store.load_calculation({"bucket": final["bucket"], "key": final["key"]}, binding)
    candidate = store.save_calculation(store.planned_calculation(binding), b"actual-calculation-result", binding)
    with pytest.raises(JourneyError):
        store.read_final(candidate, binding, publication)


def test_read_cap_checks_actual_body_size_instead_of_trusting_content_length():
    store, client, _, _, binding = setup_storage()
    binding = call_binding(binding)
    ref = store.save_calculation(store.planned_calculation(binding), b"small", binding)
    client.objects[(store.bucket, ref["key"])]["Body"] = b"x" * (store.artifact_limit + 1)
    with pytest.raises(JourneyError) as raised:
        store.load_calculation({"bucket": ref["bucket"], "key": ref["key"]}, binding)
    assert raised.value.code == "STORED_INPUT_INVALID"


@pytest.mark.parametrize("artifact", ["manifest", "raw", "meta", "aed"])
def test_committed_input_artifact_loss_is_permanent_integrity_failure(artifact):
    store, client, _, projected, binding = setup_storage()
    projected = type(projected)(projected.cpr_bytes, b"collected-aed-bytes", projected.payload)
    binding["input_digest"] = typed_identity(projected)
    saved = store.save_input(projected, binding)
    suffix = {"manifest": ".request.json", "raw": ".bin", "meta": ".meta.json", "aed": ".aed.bin"}[artifact]
    del client.objects[(store.bucket, saved["raw_base"] + suffix)]
    with pytest.raises(JourneyError) as raised:
        store.load_input(saved["manifest_ref"], binding)
    assert raised.value.code == "STORED_INPUT_INVALID"


@pytest.mark.parametrize("artifact", ["final", "snapshot", "published_chart"])
def test_committed_final_or_selected_chart_loss_never_becomes_retry_or_no_chart(artifact):
    store, client, _, projected, input_binding = setup_storage()
    saved = store.save_input(projected, input_binding)
    binding = call_binding(input_binding)
    selected = {**store.save_chart_candidate({"x": [1]}, "a" * 64, binding, 1), "revision": 1}
    publication = store.publish_selected_chart("job1", saved["raw_base"], binding, lambda _: selected)
    final = store.save_final(b'{"score": 90}', binding, 1, publication)
    if artifact == "final":
        key, read = final["key"], lambda: store.read_final(final, binding, publication)
    elif artifact == "snapshot":
        key = selected["snapshot_ref"]["key"]
        read = lambda: store.publish_selected_chart("job1", saved["raw_base"], binding, lambda _: selected)
    else:
        key, read = publication["key"], lambda: store.sign_chart(publication)
    del client.objects[(store.bucket, key)]
    with pytest.raises(JourneyError) as raised:
        read()
    assert raised.value.code == "STORED_INPUT_INVALID"


@pytest.mark.parametrize("code", ["NoSuchKey", "404", "NotFound"])
def test_only_missing_objects_distinguish_committed_new_and_optional_reads(code):
    store, client, _, projected, binding = setup_storage()
    saved = store.save_input(projected, binding)
    def missing(**kwargs):
        raise ClientError({"Error": {"Code": code, "Message": "PRIVATE-STORAGE-ERROR"}}, "GetObject")
    client.get_object = missing
    with pytest.raises(JourneyError) as raised:
        store.load_input(saved["manifest_ref"], binding)
    assert raised.value.code == "STORED_INPUT_INVALID"
    with pytest.raises(JourneyError) as raised:
        store.save_input(projected, binding)
    assert raised.value.code == "TEMPORARILY_UNAVAILABLE"
    call = call_binding(binding)
    assert store.load_calculation(store.planned_calculation(call), call) is None
    with pytest.raises(JourneyError) as raised:
        store.save_final(b'{}', call, 1, {"kind": "no_chart", "selection_revision": 1, **call})
    assert raised.value.code == "TEMPORARILY_UNAVAILABLE"
    assert "PRIVATE-STORAGE-ERROR" not in str(raised.value)


@pytest.mark.parametrize("error", [
    ClientError({"Error": {"Code": "AccessDenied"}}, "GetObject"),
    ClientError({"Error": {"Code": "InternalError"}}, "GetObject"),
    EndpointConnectionError(endpoint_url="https://storage.example.invalid"),
])
def test_committed_and_optional_access_or_transport_failures_remain_retryable(error):
    store, client, _, projected, binding = setup_storage()
    saved = store.save_input(projected, binding)
    def unavailable(**kwargs):
        raise error
    client.get_object = unavailable
    call = call_binding(binding)
    for read in (lambda: store.load_input(saved["manifest_ref"], binding),
                 lambda: store.load_calculation(store.planned_calculation(call), call)):
        with pytest.raises(JourneyError) as raised:
            read()
        assert raised.value.code == "TEMPORARILY_UNAVAILABLE"


def test_new_chart_publication_missing_on_write_confirmation_is_retryable():
    store, client, _, projected, input_binding = setup_storage()
    saved = store.save_input(projected, input_binding)
    binding = call_binding(input_binding)
    selected = {**store.save_chart_candidate({"x": 1}, "a" * 64, binding, 1), "revision": 1}
    original_get = client.get_object
    def unreadable_new_publication(**kwargs):
        if kwargs["Key"] == saved["raw_base"] + ".json":
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return original_get(**kwargs)
    client.get_object = unreadable_new_publication
    with pytest.raises(JourneyError) as raised:
        store.publish_selected_chart("job1", saved["raw_base"], binding, lambda _: selected)
    assert raised.value.code == "TEMPORARILY_UNAVAILABLE"

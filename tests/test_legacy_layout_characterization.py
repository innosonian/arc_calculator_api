"""P4 characterization: the legacy raw/chart object layout (D28) across its implementations.

util/uploader.py (ported original, left untouched), AwsLegacyBindings,
LocalLegacyBindings and the test MemoryLegacyBindings must produce the same
object keys for the same (directory, stage, org, stem):

  {directory}/{stage}/{org}/{UTC date of the stem}/{stem}.bin | .meta.json | .aed.bin | .json

The expected keys are spelled out here. The implementations serialize meta
differently (json.dumps(default=str) vs typed.json_bytes); they are not merged,
and JourneyStorage verifies the uploaded meta with typed.json_bytes, so the two
must give the same bytes for the meta that build_raw_input_meta produces from
JSON-native input. That equality (and where it stops holding) is pinned below.
"""

import json
import math
from types import SimpleNamespace
import uuid

import pytest

from mock_journey import typed
from util import uploader


STEM = "CPR-ACTION-1800000000-" + str(uuid.UUID(int=7))
DATE = "2027-01-15"  # UTC date of epoch 1800000000
ORG = str(uuid.UUID(int=9))
DIRECTORY = "calculator_result/interpreted_rtdata/arc"
STAGE = "dev"
BASE = f"{DIRECTORY}/{STAGE}/{ORG}/{DATE}/{STEM}"


class Recorder:
    def __init__(self):
        self.puts = []
        self.signed = []

    def put_object(self, *, Bucket, Key, Body, **extra):
        self.puts.append((Bucket, Key, Body))
        return {}

    def generate_presigned_url(self, operation, *, Params, ExpiresIn):
        self.signed.append((operation, Params, ExpiresIn))
        return "https://signed.invalid/" + Params["Key"]


def _meta():
    return uploader.build_raw_input_meta({"mode": "training", "target": "adult"}, [], None)


def test_date_prefix_of_the_fixed_stem():
    assert uploader.date_prefix(STEM) == DATE


def _aws(recorder, *, directory=DIRECTORY, stage=STAGE):
    from mock_journey.aws_storage import AwsLegacyBindings
    return AwsLegacyBindings(recorder, SimpleNamespace(bucket="private-bucket", directory=directory, stage=stage))


def _local(recorder):
    from local_server.object_storage import LocalLegacyBindings
    bindings = object.__new__(LocalLegacyBindings)  # The layout only; no private file store is opened.
    bindings.client = SimpleNamespace(put_object=recorder.put_object, stage=STAGE)
    bindings.chart_service = None
    bindings.bucket, bindings.directory = "private-bucket", DIRECTORY
    return bindings


def _memory(recorder):
    from tests.mock_storage_support import MemoryLegacyBindings
    return MemoryLegacyBindings(recorder)


@pytest.mark.parametrize("factory", [_aws, _local, _memory], ids=["aws", "local", "memory"])
def test_binding_raw_and_chart_keys(factory):
    recorder = Recorder()
    bindings = factory(recorder)
    meta = _meta()
    assert bindings.upload_raw_input(b"cpr", b"aed", meta, stage=STAGE, key_stem=STEM, org=ORG,
                                     directory=DIRECTORY) == BASE
    assert bindings.upload_raw_input(b"cpr", b"", meta, stage=STAGE, key_stem=STEM, org=ORG,
                                     directory=DIRECTORY) == BASE
    assert bindings.upload_json_file({"chart": [1.5, 2]}, directory=DIRECTORY, stage=STAGE, key_stem=STEM,
                                     org=ORG) == BASE + ".json"
    assert [key for _, key, _ in recorder.puts] == [
        BASE + ".bin", BASE + ".meta.json", BASE + ".aed.bin", BASE + ".bin", BASE + ".meta.json", BASE + ".json"]
    bodies = {key: body for _, key, body in recorder.puts}
    meta_body = bodies[BASE + ".meta.json"]
    assert (meta_body.encode() if type(meta_body) is str else meta_body) == typed.json_bytes(meta)


def test_ported_uploader_functions_produce_the_same_keys(monkeypatch):
    recorder = Recorder()
    monkeypatch.setattr(uploader, "client", lambda *args, **kwargs: recorder)
    meta = _meta()
    assert uploader.upload_raw_input(b"cpr", b"aed", meta, stage=STAGE, key_stem=STEM, org=ORG,
                                     directory=DIRECTORY) == BASE
    assert uploader.upload_json_file({"chart": 1}, directory=DIRECTORY, stage=STAGE, key_stem=STEM,
                                     org=ORG) == BASE + ".json"
    assert [(bucket, key) for bucket, key, _ in recorder.puts] == [
        (uploader.BUCKET, BASE + ".bin"), (uploader.BUCKET, BASE + ".meta.json"),
        (uploader.BUCKET, BASE + ".aed.bin"), (uploader.BUCKET, BASE + ".json")]
    assert uploader.upload_raw_input(b"cpr", b"", meta, stage="test", key_stem=STEM, org=ORG) is None
    assert uploader.upload_json_file({}, stage="test", key_stem=STEM, org=ORG) is None


def test_aws_binding_rejects_another_namespace_and_keeps_its_byte_rules():
    recorder = Recorder()
    bindings = _aws(recorder)
    for directory, stage in ((DIRECTORY + "x", STAGE), (DIRECTORY, "prod")):
        with pytest.raises(ValueError, match=r"\AInvalid AWS storage binding\.\Z"):
            bindings.upload_raw_input(b"", b"", {}, stage=stage, key_stem=STEM, org=ORG, directory=directory)
        with pytest.raises(ValueError, match=r"\AInvalid AWS storage binding\.\Z"):
            bindings.upload_json_file({}, directory=directory, stage=stage, key_stem=STEM, org=ORG)
    assert recorder.puts == []
    bindings.upload_raw_input(None, None, {"when": 1.5}, stage=STAGE, key_stem=STEM, org=ORG, directory=DIRECTORY)
    assert recorder.puts == [("private-bucket", BASE + ".bin", b""),
                             ("private-bucket", BASE + ".meta.json", '{"when": 1.5}')]
    assert bindings.create_signed_url(BASE + ".json") == "https://signed.invalid/" + BASE + ".json"
    assert recorder.signed == [("get_object", {"Bucket": "private-bucket", "Key": BASE + ".json"}, 300)]
    for key, expires in ((BASE + ".bin", 300), ("other/dev/" + STEM + ".json", 300), (BASE + ".json", 299),
                         (None, 300)):
        with pytest.raises(ValueError, match=r"\AInvalid AWS chart binding\.\Z"):
            bindings.create_signed_url(key, expires_in=expires)


@pytest.mark.parametrize("condition,events,organization", [
    ({"mode": "training", "target": "adult", "is_2rescuers": False}, [], None),
    (None, None, {}),
    ({"target": "infant"}, [{"event": 3, "timestamp": 12.25, "last_timestamp": None}],
     {"org_id": str(uuid.UUID(int=5)), "org_name": "기관 Ω", "First_name": "", "Last_name": "Lee"}),
    ({"target": "child", "cpr_cycle_type": "152"}, [{"event": 1, "timestamp": 1e-7}, {"event": 2, "timestamp": 10**20}],
     {"org_id": None, "org_name": "é́", "First_name": "\U0001f600", "Last_name": None}),
])
def test_default_str_meta_serialization_equals_typed_json_for_projected_meta(condition, events, organization):
    meta = uploader.build_raw_input_meta(condition, events, organization)
    assert json.dumps(meta, default=str).encode("utf-8") == typed.json_bytes(meta)


def test_serializers_differ_outside_json_native_values():
    # Projection and canonical_bytes reject these before a meta is built; the
    # two serializers must therefore never be merged into one "equivalent" one.
    assert json.dumps({"v": math.nan}, default=str) == '{"v": NaN}'
    with pytest.raises(ValueError):
        typed.json_bytes({"v": math.nan})
    assert json.dumps({"v": {1, 2}.__class__.__name__}, default=str) == '{"v": "set"}'
    assert json.dumps({"v": b"x"}, default=str) == '{"v": "b\'x\'"}'
    with pytest.raises(ValueError):
        typed.json_bytes({"v": b"x"})


def test_chart_json_serialization_equivalence_for_chart_shaped_data():
    chart = {"compression": [[0, 1.25], [1, 55.5]], "ventilation": [], "labels": ["a", "é"], "none": None}
    assert json.dumps(chart).encode("utf-8") == typed.json_bytes(chart)


def test_internal_calculator_stem_rule_matches_the_generated_stem():
    from mock_journey.internal_calculator import _STEM
    match = _STEM.fullmatch(STEM)
    assert match and match[1] == "1800000000" and match[2] == str(uuid.UUID(int=7))
    generated = uploader.build_key_stem()
    assert _STEM.fullmatch(generated)
    for bad in ("CPR-ACTION-180000000-" + str(uuid.UUID(int=7)), STEM + ".json", "X" + STEM):
        assert _STEM.fullmatch(bad) is None


def test_shared_layout_rules_spell_the_same_keys():
    from util import legacy_layout
    assert legacy_layout.stage_prefix(DIRECTORY, STAGE) == f"{DIRECTORY}/{STAGE}/"
    assert legacy_layout.object_base(DIRECTORY, STAGE, ORG, DATE, STEM) == BASE
    assert (legacy_layout.RAW_SUFFIX, legacy_layout.META_SUFFIX, legacy_layout.AED_SUFFIX,
            legacy_layout.CHART_SUFFIX) == (".bin", ".meta.json", ".aed.bin", ".json")
    assert legacy_layout.KEY_STEM.fullmatch(STEM)


def test_journey_storage_plans_and_verifies_the_legacy_layout_keys(monkeypatch):
    """JourneyStorage spells its raw/meta/AED/manifest/chart keys from the shared layout (B-02)."""
    from mock_journey import storage as storage_module
    from tests.projection_storage_support import setup_storage
    storage, client, bindings, projected, binding = setup_storage("dev")
    directory = bindings.directory
    monkeypatch.setattr(bindings, "build_key_stem", lambda: STEM)
    assert storage.prefix == f"{directory}/dev/"
    assert storage_module.MANIFEST_SUFFIX == ".request.json"
    saved = storage.save_input(projected, binding)
    base = f"{directory}/dev/_no_org/{DATE}/{STEM}"
    assert saved["raw_base"] == base
    assert saved["manifest_ref"]["key"] == base + ".request.json"
    assert sorted(key for _, key in client.objects) == [base + ".bin", base + ".meta.json", base + ".request.json"]
    loaded = storage.load_input(saved["manifest_ref"], binding)
    assert loaded.raw_base == base and loaded.projected == projected
    # The chart key is the same base with the chart suffix; the raw base is recovered by stripping it.
    with pytest.raises(Exception):
        storage.sign_chart({**binding, "job_id": "j", "call_id": "c", "kind": "snapshot",
                            "key": base + ".jsonx", "published_body_sha256": "0" * 64})

"""Explicit S3 binding keeps legacy bytes/key conventions without hidden SDKs."""

import json
import pytest

from mock_journey.aws_settings import AwsSettings
from mock_journey.aws_storage import AwsLegacyBindings
from tests.mock_storage_support import MemoryS3
from tests.test_aws_runtime import configuration


def binding():
    objects = MemoryS3()
    settings = AwsSettings.parse(json.dumps(configuration()), "api").role_settings.storage
    return objects, AwsLegacyBindings(objects, settings)


def test_raw_meta_chart_and_signature_use_the_explicit_bucket_and_original_serialization():
    objects, legacy = binding()
    stem = legacy.build_key_stem()
    kwargs = {"directory": legacy.directory, "stage": "dev", "key_stem": stem, "org": "_no_org"}
    meta = {"observed": 1.0, "empty": None}
    base = legacy.upload_raw_input(b"raw", b"aed", meta, **kwargs)
    key = legacy.upload_json_file(meta, **kwargs)
    assert key == base + ".json"
    assert objects.objects[(legacy.bucket, base + ".meta.json")]["Body"] == json.dumps(meta, default=str).encode()
    assert objects.objects[(legacy.bucket, key)]["Body"] == json.dumps(meta).encode()
    assert objects.objects[(legacy.bucket, base + ".aed.bin")]["Body"] == b"aed"
    assert legacy.create_signed_url(key)
    assert objects.sign_calls == [("get_object", {"Bucket": legacy.bucket, "Key": key}, 300)]


@pytest.mark.parametrize("change", [{"stage": "prod"}, {"directory": "other/path"}])
def test_binding_mismatch_rejects_without_write(change):
    objects, legacy = binding()
    kwargs = {"directory": legacy.directory, "stage": "dev", "key_stem": legacy.build_key_stem(), "org": "_no_org", **change}
    with pytest.raises(ValueError):
        legacy.upload_raw_input(b"raw", b"", {}, **kwargs)
    assert objects.objects == {}


@pytest.mark.parametrize("key,lifetime", [("outside/raw.bin", 300), ("calculator_result/arc/dev/a.bin", 300),
                                          ("calculator_result/arc/dev/a.json", 301)])
def test_signing_rejects_nonchart_wrong_namespace_or_changed_lifetime(key, lifetime):
    objects, legacy = binding()
    with pytest.raises(ValueError):
        legacy.create_signed_url(key, expires_in=lifetime)
    assert objects.sign_calls == []

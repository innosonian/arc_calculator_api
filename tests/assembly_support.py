"""Constructor-only definitions, schemas, adapters and settings for role assembly tests.

These existed as identical copies in tests/test_mock_assembly.py and
tests/test_vcc_wiring.py; both now import this one copy (moved unchanged).
"""

from types import SimpleNamespace

import pytest

from mock_journey.catalog import PROGRAMS, TARGETS, definition_key
from mock_journey.projection import ProjectionSchema
from mock_journey.settings import ApiSettings, StateSettings, StorageSettings
from tests.mock_storage_support import MemoryLegacyBindings, MemoryS3


def definitions():
    # Merely constructor fixtures, not a supplied execution registry.
    return {definition_key(program[0], target): {
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

"""Projected measurement input and in-memory journey storage shared by storage tests.

``measurement_fixture`` (from test_mock_projection.py) and ``setup_storage``
(from test_mock_storage.py) moved here unchanged so other test modules no longer
import a test module; both test modules re-export them.
"""

from copy import deepcopy

from mock_journey.projection import ProjectionSchema, project_input, typed_identity
from mock_journey.storage import JourneyStorage
from services.http.schemas import DEFAULT_CONDITION
from tests.mock_storage_support import MemoryLegacyBindings, MemoryS3


def measurement_fixture():
    condition = {**DEFAULT_CONDITION, "guideline": "ARC2025"}
    definition = {"condition": condition, "calculation_profile": {},
                  "goal": {"kind": "cycles", "required": 3}, "catalog_version": "mock-catalog-v1",
                  "profile_version": "tester-v1", "adapter_version": "test-adapter-v1", "projection_version": "test-projection-v1"}
    body = {"cpr_b64_data": b"actual-collected-measurement", "aed_b64_data": b"",
            "condition": deepcopy(condition), "vp_event_list": []}
    schema = ProjectionSchema("test-projection-v1", {"CompressionDepth": {"%_Good": "scalar"}})
    return body, definition, schema


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

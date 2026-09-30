"""Local worker composition: adapter list and startup binding checks (S6-11, S9-18, S10-09).

No sockets, DB or worker process: the real ``build_worker`` runs over a client
that fails on any SDK call, so only construction-time checks are exercised.
"""

from types import SimpleNamespace

import pytest

from local_server import object_storage as objects
from local_server import runtime
from local_server.database import prepare_material
from mock_journey import contracts
from mock_journey.contracts import (
    CURRENT_ADAPTER_VERSION, PENDING_GOAL_ADAPTER_VERSION, RETAINED_PENDING_GOAL_ADAPTER_VERSION,
)
from mock_journey.cycle_goal import closed_cycle_count
from mock_journey.execution_definitions import execution_catalog
from mock_journey.internal_calculator import InternalCalculator


PROJECTION = "arc-local-projection-v1"


class NoClientCalls:
    def __getattr__(self, name):
        pytest.fail("Worker construction accessed an SDK client operation.")


@pytest.fixture
def installation(tmp_path):
    root = tmp_path.resolve() / "installation"
    root.mkdir(mode=0o700)
    local = prepare_material(root)
    return local, objects.prepare_object_material(local)


def captured_local_worker(installation, monkeypatch):
    """(build_worker kwargs, runner, owned objects) of the real local composition."""
    import mock_journey.assembly

    local, object_material = installation
    captured = {}
    original = mock_journey.assembly.build_worker

    def spy(settings, **kwargs):
        captured.update(kwargs)
        return original(settings, **kwargs)

    monkeypatch.setattr(mock_journey.assembly, "build_worker", spy)
    database = SimpleNamespace(table_name="arc_mock_local_v1", client=NoClientCalls(), operations=None)
    runner, owned = runtime.build_local_worker(database, local, runtime.LocalOptions(), "127.0.0.1", 8000,
                                               object_material=object_material)
    return captured, runner, owned


def test_local_worker_registers_current_then_retained_internal_calculators(installation, monkeypatch):
    captured, runner, owned = captured_local_worker(installation, monkeypatch)
    try:
        assert runtime.PROJECTION_VERSION == PROJECTION
        rows = [(type(a), a.version, a.projection_version, a.stage, a.allow_pending_cycle_goal,
                 a.cycle_goal_resolver) for a in captured["adapters"]]
        assert rows == [
            (InternalCalculator, CURRENT_ADAPTER_VERSION, PROJECTION, "local", False, closed_cycle_count),
            (InternalCalculator, RETAINED_PENDING_GOAL_ADAPTER_VERSION, PROJECTION, "local", True, None),
            (InternalCalculator, PENDING_GOAL_ADAPTER_VERSION, PROJECTION, "local", True, None),
        ]
        required = tuple(captured["required_bindings"])
        # The catalog's current binding is always checked at startup; every
        # checked binding names one of the adapters registered above.
        assert (CURRENT_ADAPTER_VERSION, PROJECTION) in required
        assert set(required) <= {(CURRENT_ADAPTER_VERSION, PROJECTION),
                                 (RETAINED_PENDING_GOAL_ADAPTER_VERSION, PROJECTION),
                                 (PENDING_GOAL_ADAPTER_VERSION, PROJECTION)}
        assert captured["operations"] is None
        assert getattr(runner._relay, "processing_reserve_ms", None) is None
        assert getattr(runner._relay, "relay_budget", None) is None
        assert runner._relay.page_size == 20 and runner._relay.max_pages == 5
    finally:
        owned.close()


def test_local_retained_adapters_are_exactly_the_code_registry(installation, monkeypatch):
    # D127/B-03: the local Worker reads the whole registry, not a literal.
    assert contracts.RETAINED_ADAPTER_VERSIONS == ("arc-local-calculator-pending-v2",
                                                   "arc-internal-detection-pending-v3")
    catalog_bindings = execution_catalog().required_bindings
    captured, _, owned = captured_local_worker(installation, monkeypatch)
    try:
        assert [a.version for a in captured["adapters"]] == [CURRENT_ADAPTER_VERSION,
                                                              *contracts.RETAINED_ADAPTER_VERSIONS]
        assert tuple(captured["required_bindings"]) == catalog_bindings + (
            ("arc-local-calculator-pending-v2", PROJECTION), ("arc-internal-detection-pending-v3", PROJECTION))
    finally:
        owned.close()
    # Negative case: an emptied registry removes the retained adapter and its
    # binding from the local composition (nothing else names that version).
    monkeypatch.setattr(contracts, "RETAINED_ADAPTER_VERSIONS", ())
    captured, _, owned = captured_local_worker(installation, monkeypatch)
    try:
        assert [a.version for a in captured["adapters"]] == [CURRENT_ADAPTER_VERSION]
        assert tuple(captured["required_bindings"]) == catalog_bindings
        assert all(version == CURRENT_ADAPTER_VERSION for version, _ in catalog_bindings)
    finally:
        owned.close()

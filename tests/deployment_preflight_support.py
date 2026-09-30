"""Syntax-only deployment binding fixtures shared by preflight and Actions tests.

Moved unchanged from tests/test_deployment_preflight.py so other test modules no
longer import a test module; test_deployment_preflight re-exports them.
"""

from scripts import deployment_preflight as preflight


def fixture_binding():
    # Syntax fixtures only, never configured AWS resources or policy defaults.
    return {"account_id": "000000000000", "region": "xz-region-1", "runtime_stage": "unchanged_runtime_label",
            "calculator": {"function_name": "FixtureCalc", "role_name": "FixtureRole", "memory_mb": 512,
                           "timeout_seconds": 120, "log_retention_days": 30, "reserved_concurrency": None,
                           "storage_bucket": preflight.read_storage_contract()[0], "storage_prefix": preflight.read_storage_contract()[1], "storage_region": "xy-region-1"},
            "gateway": {"api_id": "fixtureapi", "stage": "unchanged_gateway_label", "route": "/cpr-analysis",
                        "rate_limit": None, "burst_limit": None}}


def configuration(environment="beta"):
    return {"schema_version": 1, "environments": {environment: fixture_binding()}}


def variables():
    return {"STAGE": "unchanged_runtime_label", "ARC_STORAGE_REGION": "xy-region-1",
            "ARC_MOCK_ENVIRONMENT": "unchanged_identity_namespace", "ARC_MOCK_TABLE_NAME": "unchanged_table"}

"""U0 baseline on DynamoDB Local: same scripts and fixtures as tests/test_v2_baseline.py.

The templates in tests/fixtures/v2_baseline/ were captured on the in-memory
table; matching them here on an isolated DynamoDB Local table (with GSI1)
also checks tests/memory_dynamodb.py against real conditional-write, query
and transaction behavior. See tests/test_v2_baseline.py for the capture method.
"""

import pytest

from tests.journey_support import dynamodb_local_store
from tests.memory_dynamodb import PROBE_EXPECTED, conformance_probe
from tests.v2_baseline_support import (
    LEGACY_SECTIONS, assert_matches, flow_template, golden_flow, legacy_active_session, legacy_new_session,
    legacy_template,
)


def test_memory_dynamodb_probe_matches_dynamodb_local(dynamodb_client):
    with dynamodb_local_store(dynamodb_client) as store:
        assert conformance_probe(store.client, store.table) == PROBE_EXPECTED


def test_v2_flow_matches_baseline_on_dynamodb_local(dynamodb_client):
    with dynamodb_local_store(dynamodb_client) as store:
        assert_matches(flow_template(), golden_flow(store))


@pytest.mark.parametrize("name,script", [
    ("legacy_active_session", legacy_active_session),
    ("legacy_new_session", legacy_new_session),
])
def test_legacy_mock_v1_rows_match_baseline_on_dynamodb_local(dynamodb_client, name, script):
    with dynamodb_local_store(dynamodb_client) as store:
        assert_matches(legacy_template(name), script(store), sections=LEGACY_SECTIONS)

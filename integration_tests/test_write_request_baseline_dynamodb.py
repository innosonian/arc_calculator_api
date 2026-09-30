"""P4 write-request baseline on DynamoDB Local: same flows and fixture as tests/test_write_request_baseline.py.

The template was captured on the in-memory table. Recording the requests sent
to an isolated DynamoDB Local table (with GSI1) shows that the flows, and so
the requests, do not depend on the in-memory emulation. DynamoDB returns item
attributes in its own order and the runtime rebuilds rows from reads, so the
object key order is compared only by the in-memory test.
"""

import pytest

from tests.journey_support import dynamodb_local_store
from tests.write_baseline_support import FLOWS, RecordingClient, assert_writes_match, load_write_baseline, observe


@pytest.mark.parametrize("name", list(FLOWS))
def test_write_requests_match_baseline_on_dynamodb_local(dynamodb_client, name):
    with dynamodb_local_store(RecordingClient(dynamodb_client)) as store:
        assert_writes_match(load_write_baseline()[name], observe(name, store), name, key_order=False)

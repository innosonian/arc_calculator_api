"""Explicit DynamoDB Local endpoint, client and throwaway table fixtures.

Moved unchanged from integration_tests/conftest.py, then from
integration_tests/dynamodb_local_support.py into tests/ (E-14) so the suite
folders depend on tests/ only.
The integration_tests and http_pipeline_tests conftests register the fixtures
by importing them; each keeps its own ``loopback_network_guard``, which
``dynamodb_client`` requests by name. Nothing under tests/ requests these
fixtures itself: the offline suite has no DynamoDB Local endpoint.
"""

import os
from urllib.parse import urlsplit
import uuid

import boto3
from botocore.config import Config
import pytest


@pytest.fixture
def dynamodb_endpoint():
    endpoint = os.environ.get("ARC_TEST_DYNAMODB_ENDPOINT")
    if not endpoint:
        raise pytest.UsageError("Set ARC_TEST_DYNAMODB_ENDPOINT explicitly to the local test server.")
    try:
        parsed = urlsplit(endpoint)
        valid = (
            parsed.scheme == "http"
            and parsed.hostname == "127.0.0.1"
            and parsed.port is not None
            and 0 < parsed.port < 65536
            and parsed.username is None
            and parsed.password is None
            and parsed.path in ("", "/")
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        valid = False
    if not valid:
        raise pytest.UsageError("DynamoDB integration tests require an explicit http://127.0.0.1:port endpoint.")
    return endpoint.rstrip("/")


@pytest.fixture
def dynamodb_client(dynamodb_endpoint, loopback_network_guard):
    # Explicit fake keys never make a normal AWS endpoint safe. The validated
    # endpoint and socket guard above are required in addition to these values.
    client = boto3.client(
        "dynamodb",
        endpoint_url=dynamodb_endpoint,
        region_name="us-east-2",
        aws_access_key_id="ARCLocalTestAccess",
        aws_secret_access_key="ARCLocalTestSecret",
        aws_session_token="ARCLocalTestSession",
        config=Config(
            proxies={},
            connect_timeout=2,
            read_timeout=5,
            retries={"total_max_attempts": 1},
        ),
    )
    try:
        yield client
    finally:
        client.close()


@pytest.fixture
def dynamodb_table(dynamodb_client):
    name = "arc_mock_p2_" + uuid.uuid4().hex
    dynamodb_client.create_table(
        TableName=name,
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"}, {"AttributeName": "SK", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    try:
        yield name
    finally:
        # Only this fixture's newly generated table is ever deleted.
        dynamodb_client.delete_table(TableName=name)

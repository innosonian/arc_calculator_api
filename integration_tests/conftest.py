"""Explicit loopback-only DynamoDBLocal tests, separate from tests/ AWS guards."""

import os
import socket
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


@pytest.fixture(autouse=True)
def loopback_network_guard(monkeypatch, dynamodb_endpoint):
    port = urlsplit(dynamodb_endpoint).port
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def validate(address):
        if not isinstance(address, tuple) or address[:2] != ("127.0.0.1", port):
            pytest.fail("Integration test attempted a connection outside its explicit loopback endpoint.")

    def connect(sock, address):
        validate(address)
        return original_connect(sock, address)

    def connect_ex(sock, address):
        validate(address)
        return original_connect_ex(sock, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_CONFIG_FILE", os.devnull)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", os.devnull)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_PROFILE", raising=False)
    monkeypatch.delenv("SENTRY_DSN", raising=False)


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

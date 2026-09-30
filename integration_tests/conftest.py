"""Explicit loopback-only DynamoDBLocal tests, separate from tests/ AWS guards."""

from urllib.parse import urlsplit

import pytest

from tests.dynamodb_local_support import (  # noqa: F401 (fixtures)
    dynamodb_client, dynamodb_endpoint, dynamodb_table,
)
from tests.network_guard_support import guard_socket_connect, isolate_aws_environment, loopback_validator


@pytest.fixture(autouse=True)
def loopback_network_guard(monkeypatch, dynamodb_endpoint):
    port = urlsplit(dynamodb_endpoint).port
    # A one-element tuple keeps the exact `address[:2] != ("127.0.0.1", port)` comparison.
    guard_socket_connect(monkeypatch, loopback_validator(
        (("127.0.0.1", port),), "Integration test attempted a connection outside its explicit loopback endpoint.",
    ))
    isolate_aws_environment(monkeypatch)

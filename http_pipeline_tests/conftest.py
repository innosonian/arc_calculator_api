"""Allow only each test's explicit loopback DB and HTTP endpoints."""

from urllib.parse import urlsplit

import pytest

from tests.dynamodb_local_support import (  # noqa: F401 (fixtures)
    dynamodb_client, dynamodb_endpoint, dynamodb_table,
)
from tests.network_guard_support import guard_socket_connect, isolate_aws_environment, loopback_validator


@pytest.fixture(autouse=True)
def loopback_network_guard(monkeypatch, dynamodb_endpoint):
    allowed = {("127.0.0.1", urlsplit(dynamodb_endpoint).port)}
    # Tests extend the returned set with the loopback HTTP servers they start.
    guard_socket_connect(monkeypatch, loopback_validator(
        allowed, "HTTP integration attempted an unconfigured destination.",
    ))
    isolate_aws_environment(monkeypatch)
    return allowed

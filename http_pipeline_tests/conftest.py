"""Allow only each test's explicit loopback DB and HTTP endpoints."""

import os
import socket
from urllib.parse import urlsplit

import pytest

from integration_tests.conftest import dynamodb_endpoint, dynamodb_client, dynamodb_table


@pytest.fixture(autouse=True)
def loopback_network_guard(monkeypatch, dynamodb_endpoint):
    allowed = {("127.0.0.1", urlsplit(dynamodb_endpoint).port)}
    original_connect, original_connect_ex = socket.socket.connect, socket.socket.connect_ex

    def connect(sock, address):
        if not isinstance(address, tuple) or address[:2] not in allowed:
            pytest.fail("HTTP integration attempted an unconfigured destination.")
        return original_connect(sock, address)

    def connect_ex(sock, address):
        if not isinstance(address, tuple) or address[:2] not in allowed:
            pytest.fail("HTTP integration attempted an unconfigured destination.")
        return original_connect_ex(sock, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_CONFIG_FILE", os.devnull)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", os.devnull)
    for name in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE", "SENTRY_DSN"):
        monkeypatch.delenv(name, raising=False)
    return allowed

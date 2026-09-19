"""Calculation GET does not execute submission; transmit remains 0."""

import json
from types import SimpleNamespace

from mock_journey.handler import handle
from tests.vcc_support import event
from integration_tests.test_vcc_state_dynamodb import application, login


CONTEXT = SimpleNamespace(aws_request_id="vcc-calc-http")


def test_calculation_get_missing_attempt_is_404_without_submit(dynamodb_client, dynamodb_table):
    app = application(dynamodb_client, dynamodb_table)
    token, _ = login(app)
    calls = []
    original = app.gateway.submit

    def wrapped(intent):
        calls.append(intent)
        return original(intent)

    app.gateway.submit = wrapped
    response = handle(event(
        "GET", "/api/v2/attempts/50000000-0000-4000-8000-000000000001/calculation/",
        token=token,
    ), CONTEXT, app)
    assert response["statusCode"] == 404
    assert calls == []
    assert json.loads(response["body"])["success"] is False

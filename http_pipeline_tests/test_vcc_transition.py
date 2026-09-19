"""D6 HTTP transition: mock/v1 and /cpr-analysis 404 in course_v2."""

import json
from types import SimpleNamespace

from mock_journey.handler import handle
from tests.vcc_support import OLD_ROUTES, event
from integration_tests.test_vcc_state_dynamodb import application, login


CONTEXT = SimpleNamespace(aws_request_id="vcc-http-transition")


def test_legacy_http_routes_404(dynamodb_client, dynamodb_table):
    app = application(dynamodb_client, dynamodb_table)
    token, _ = login(app)
    for method, path in OLD_ROUTES:
        response = handle(event(method, path, token=token, body={} if method in ("POST", "PUT") else None),
                          CONTEXT, app)
        assert response["statusCode"] == 404
        assert json.loads(response["body"])["error"]["code"] == "NOT_FOUND"

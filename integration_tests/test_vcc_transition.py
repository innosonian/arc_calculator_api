"""D6/T9 transition: old routes 404, worker-compatible rows remain."""

from types import SimpleNamespace
import json

from mock_journey.handler import handle
from tests.vcc_support import OLD_ROUTES, event
from integration_tests.test_vcc_state_dynamodb import application, login


CONTEXT = SimpleNamespace(aws_request_id="vcc-transition")


def test_course_v2_all_legacy_routes_404(dynamodb_client, dynamodb_table):
    app = application(dynamodb_client, dynamodb_table)
    token, _ = login(app)
    for method, path in OLD_ROUTES:
        body = {"loginId": "x", "password": "y"} if method in ("POST", "PUT") else None
        response = handle(event(method, path, body=body, token=token), CONTEXT, app)
        assert response["statusCode"] == 404, (method, path, response["statusCode"])
        payload = json.loads(response["body"])
        assert payload["error"]["code"] == "NOT_FOUND"

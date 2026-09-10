"""Explicit opt-in wiring; no test store or calculation fixtures in runtime."""

import base64
import json
import os

from mock_journey.auth import AuthManager
from mock_journey.catalog import Catalog
from mock_journey.errors import JourneyError
from mock_journey.service import JourneyService


_application = None


def get_application():
    global _application
    if _application is not None:
        return _application
    required = (
        "ARC_MOCK_ENVIRONMENT", "ARC_MOCK_TABLE_NAME", "ARC_MOCK_REGION",
        "ARC_MOCK_RESUME_KEYS", "ARC_MOCK_RESUME_KEY_VERSION",
    )
    if os.environ.get("ARC_MOCK_ENABLED") != "true" or any(not os.environ.get(key) for key in required):
        raise JourneyError("TEMPORARILY_UNAVAILABLE")
    values = json.loads(os.environ["ARC_MOCK_RESUME_KEYS"])
    if type(values) is not dict or any(type(v) is not str for v in values.values()):
        raise JourneyError("TEMPORARILY_UNAVAILABLE")
    keys = {key: base64.b64decode(value, validate=True) for key, value in values.items()}
    # Validate secrets before creating an SDK client. Never discover local/test endpoints.
    auth = AuthManager(None, os.environ["ARC_MOCK_ENVIRONMENT"], keys, os.environ["ARC_MOCK_RESUME_KEY_VERSION"])
    import boto3
    from mock_journey.state import DynamoStateRepository
    state = DynamoStateRepository(boto3.client("dynamodb", region_name=os.environ["ARC_MOCK_REGION"]),
                                  os.environ["ARC_MOCK_TABLE_NAME"])
    auth.state = state
    _application = JourneyService(state, auth, Catalog())
    return _application

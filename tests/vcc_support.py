"""Shared VCC wiring helpers. Does not capture implementation output as oracles."""

from pathlib import Path
import hashlib
import json
import uuid

from mock_journey.auth import PRINCIPAL
from mock_journey.course_contracts import LearnerContext, CONTRACT_VERSION
from mock_journey.course_settings import fixture_course_settings
from mock_journey.typed import parse_json


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "vcc_contract" / "v1"
OLD_ROUTES = (
    ("POST", "/mock/v1/sessions"),
    ("GET", "/mock/v1/session"),
    ("DELETE", "/mock/v1/session"),
    ("GET", "/mock/v1/programs"),
    ("POST", "/mock/v1/attempts"),
    ("GET", "/mock/v1/attempts/a1234567-1234-4234-9234-123456789abc"),
    ("POST", "/mock/v1/attempts/a1234567-1234-4234-9234-123456789abc/reauthorize"),
    ("POST", "/mock/v1/attempts/a1234567-1234-4234-9234-123456789abc/cancel"),
    ("POST", "/mock/v1/attempts/a1234567-1234-4234-9234-123456789abc/calculation"),
    ("GET", "/mock/v1/attempts/a1234567-1234-4234-9234-123456789abc/calculation"),
    ("GET", "/mock/v1/attempts/a1234567-1234-4234-9234-123456789abc/chart-link"),
    ("POST", "/cpr-analysis"),
)


def load_fixture(name):
    return parse_json((FIXTURES / name).read_bytes())


def fixture_hash(name):
    return hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest()


def dummy_learner():
    row = load_fixture("course_bundle.json")["learners"]["dummy"]
    return LearnerContext(row["provider"], row["tenant_id"], row["learner_id"], PRINCIPAL, True)


def mapping_document():
    return load_fixture("execution_mapping.json")


def course_settings():
    return fixture_course_settings()


def event(method, path, *, body=None, token=None, query=None, content_type="application/json"):
    headers = {}
    if content_type:
        headers["Content-Type"] = content_type
    if token:
        headers["Authorization"] = f"Bearer {token}"
    payload = "" if body is None else (body if type(body) is str else json.dumps(body, allow_nan=False))
    query = {} if query is None else query
    return {
        "httpMethod": method, "path": path, "headers": headers,
        "multiValueHeaders": {key: [value] for key, value in headers.items()},
        "queryStringParameters": query,
        "multiValueQueryStringParameters": {key: [value] for key, value in query.items()},
        "body": payload, "isBase64Encoded": False,
    }


def request_id():
    return str(uuid.uuid4())


assert CONTRACT_VERSION == "vcc-internal-v1"

"""Synthetic AWS runtime settings, Lambda context and SDK factory shared by tests.

Moved unchanged from tests/test_aws_runtime.py so other test modules no longer
import a test module; test_aws_runtime re-exports the same objects.
"""

import base64
from copy import deepcopy
from dataclasses import asdict
import json
from types import SimpleNamespace
import uuid

from mock_journey.contracts import CURRENT_ADAPTER_VERSION, RETAINED_ADAPTER_VERSIONS
from mock_journey.course_settings import fixture_course_settings
from mock_journey.dev_course import CATALOG_VERSION, MODE
from mock_journey.execution_definitions import PROJECTION_VERSION
from tests.mock_storage_support import MemoryS3


def course_section():
    """The explicit Dummy Dev course section that API and Worker now require."""
    return {"mode": MODE, "catalog_version": CATALOG_VERSION, "settings": asdict(fixture_course_settings())}


def configuration(role="api"):
    sdk = {"connect_timeout": 0.1, "read_timeout": 0.1, "total_max_attempts": 1, "retry_mode": "standard"}
    result = {"schema": 1, "role": role, "account_id": "123456789012", "partition": "aws",
              "environment": "synthetic-dev", "region": "us-east-1",
              "state": {"table_name": "synthetic-journey", "max_conflict_retries": 4}, "sdk": sdk,
              "logs": {"capacity": 20, "max_bytes": 16384, "flush_budget_ms": 40,
                       "response_reserve_ms": 20, "sdk": deepcopy(sdk)}}
    if role != "relay":
        result.update(storage={"stage": "dev", "bucket": "synthetic-private-bucket", "directory": "calculator_result/arc",
                               "input_bytes": 1000000, "artifact_bytes": 8000000},
                      execution={"current_adapter_version": CURRENT_ADAPTER_VERSION,
                                 "projection_version": PROJECTION_VERSION,
                                 "retained_adapter_versions": list(RETAINED_ADAPTER_VERSIONS)},
                      course=course_section())
    result[role] = ({"payload_limit": 1400000} if role == "api" else
                    {"lease_seconds": 1, "retry_seconds": 1, "renewal_interval_seconds": 0.05,
                     "renewal_timeout_seconds": 0.1, "processing_reserve_ms": 500} if role == "worker" else
                    {"queue_url": "https://sqs.us-east-1.amazonaws.com/123456789012/synthetic-jobs",
                     "lease_seconds": 10, "retry_seconds": 1, "page_size": 10, "max_pages": 2,
                     "processing_reserve_ms": 500})
    return result


def environment(role="api", config=None):
    value = configuration(role) if config is None else config
    result = {"ARC_MOCK_ENABLED": "true", "ARC_JOURNEY_CONFIG": json.dumps(value)}
    if role == "api":
        result.update(ARC_MOCK_RESUME_KEYS=json.dumps({"v1": base64.b64encode(b"K" * 32).decode()}),
                      ARC_MOCK_RESUME_KEY_VERSION="v1")
    return result


def context(remaining=10000):
    return SimpleNamespace(aws_request_id=str(uuid.uuid4()), get_remaining_time_in_millis=lambda: remaining,
                           invoked_function_arn="arn:aws:lambda:us-east-1:123456789012:function:synthetic")


class FakeSdk:
    def __init__(self, s3=None):
        self.calls, self.closed, self.logs = [], [], []
        self.s3 = s3 or MemoryS3()

    def __call__(self, service, **kwargs):
        self.calls.append((service, kwargs))
        if service == "s3":
            return self.s3
        return SimpleNamespace(close=lambda: self.closed.append(service),
                               put_item=lambda **args: self.logs.append(args),
                               send_message=lambda **args: {"MessageId": "synthetic"})


def course_configuration(role="api"):
    """API/Worker documents already require it; for Relay this adds a forbidden section."""
    config = configuration(role)
    config["course"] = course_section()
    assert (config["course"]["mode"], config["course"]["catalog_version"]) == (MODE, CATALOG_VERSION)
    return config

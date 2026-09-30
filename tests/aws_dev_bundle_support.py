"""Consistent synthetic role documents and bundle check shared by bundle tests.

Moved unchanged from tests/test_aws_dev_bundle.py so other test modules no longer
import a test module; test_aws_dev_bundle re-exports them.
"""

import json

from scripts.validate_aws_dev_bundle import validate_bundle
from tests.aws_runtime_support import configuration


def documents():
    # configuration() already carries the course section API/Worker require.
    result = {role: configuration(role) for role in ("api", "worker", "relay")}
    assert result["api"]["course"]["mode"] == "course_v2_dummy"
    assert result["api"]["course"]["catalog_version"] == "arc-dummy-dev-v1"
    assert "course" not in result["relay"]
    return result


def check(configs, **timing):
    return validate_bundle({role: json.dumps(value) for role, value in configs.items()},
        **{"api_timeout": 30, "worker_timeout": 60, "relay_timeout": 30,
           "queue_visibility": 360, "batch_window": 0, **timing})

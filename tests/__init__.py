"""Test packages. Registers assertion rewriting for every shared support module.

Helpers that test modules share live in support modules (tests/*_support.py,
the doubles and seeds, integration_tests/*_support.py, http_pipeline_tests
support, local_server_tests/scripted_dynamodb.py). pytest rewrites asserts
only in test modules and registered modules, so every non-test module of the
test folders is registered here, before any ``tests.*`` submodule is imported
(every test directory's conftest imports ``tests.network_guard_support``
first). Test outcomes do not depend on this; it keeps the assertion failure
detail (introspected operands) in the helpers' own asserts (E-12).
"""

from pathlib import Path
import sys

import pytest


_ROOT = Path(__file__).resolve().parent.parent
_SUITES = ("tests", "integration_tests", "http_pipeline_tests", "local_server_tests", "transport_integration_tests")


def _support_modules():
    """Every non-test module of the test folders that is not imported yet.

    A module already imported before ``tests`` cannot be rewritten any more
    and registering it would only warn.
    """
    for suite in _SUITES:
        for path in sorted((_ROOT / suite).glob("*.py")):
            name = path.stem
            module = f"{suite}.{name}"
            if name.startswith("test_") or name in ("conftest", "__init__") or module in sys.modules:
                continue
            yield module


pytest.register_assert_rewrite(*_support_modules())

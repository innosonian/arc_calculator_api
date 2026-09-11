"""Run the selected Actions regressions with collection-time AWS/network guards.

Dependency installation happens separately. These guards cover this Python
process; the existing deployment tests supply their own fake CLI subprocesses.
This is not an operating-system egress firewall or an AWS authentication test.
"""

import argparse
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TESTS = (
    "tests/test_deployment_preflight.py",
    "tests/test_deployment_preflight_security.py",
    "tests/test_validate_actions.py",
    "tests/test_actions_regression.py",
)
AWS_FORBIDDEN = "ACTIONS_REGRESSION_AWS_FORBIDDEN"
NETWORK_FORBIDDEN = "ACTIONS_REGRESSION_NETWORK_FORBIDDEN"
_NETWORK_EVENTS = frozenset((
    "socket.connect", "socket.connect_ex", "socket.getaddrinfo",
    "socket.gethostbyname", "socket.gethostbyaddr", "socket.getnameinfo",
    "socket.sendto", "socket.sendmsg",
))


class OfflineGuardViolation(BaseException):
    """Do not let application ``except Exception`` handlers swallow a violation."""


def _prepare_environment():
    # Reinforce the CI job's process-level STAGE before importing third parties.
    # The fake application env files used by preflight remain unchanged.
    os.environ["STAGE"] = "test"
    for name in tuple(os.environ):
        if name.startswith(("AWS_", "ARC_", "HSTM_", "SENTRY_")) or name in (
            "BOTO_CONFIG", "BOTO_PATH", "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
            "ACTIONS_ID_TOKEN_REQUEST_URL", "PYTEST_ADDOPTS", "PYTEST_PLUGINS",
        ):
            os.environ.pop(name, None)
    os.environ.update({
        "AWS_CONFIG_FILE": os.devnull,
        "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
        "BOTO_CONFIG": os.devnull,
        "AWS_EC2_METADATA_DISABLED": "true",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    sys.dont_write_bytecode = True


def _network_audit(event, _args):
    if event in _NETWORK_EVENTS:
        # Never include destination addresses or other potentially private data.
        raise OfflineGuardViolation(NETWORK_FORBIDDEN)


def _forbid_aws(*_args, **_kwargs):
    raise OfflineGuardViolation(AWS_FORBIDDEN)


def _install_sdk_guards():
    # These modules do not construct clients at import time. Install the network
    # audit hook first anyway, before pytest or repository modules are imported.
    import boto3
    import boto3.session
    import botocore.client
    import botocore.httpsession
    import botocore.session

    boto3.client = _forbid_aws
    boto3.resource = _forbid_aws
    boto3.session.Session.client = _forbid_aws
    boto3.session.Session.resource = _forbid_aws
    botocore.session.Session.create_client = _forbid_aws
    botocore.client.BaseClient._make_api_call = _forbid_aws
    botocore.httpsession.URLLib3Session.send = _forbid_aws
    # conftest imports util.uploader only after these replacements, so its
    # `from boto3 import client` alias also starts guarded. Its existing autouse
    # fixture may replace both aliases with its own pytest.fail guard as usual.


def main(argv=None):
    _prepare_environment()
    sys.addaudithook(_network_audit)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", help="Explicit pytest file paths; defaults to the Actions regressions")
    args = parser.parse_args(argv)
    paths = args.paths or DEFAULT_TESTS
    # Only accept file selections, not arbitrary pytest options or directory
    # discovery. Absolute temporary files support isolated collection probes.
    selected = []
    for path in paths:
        file_name, separator, node_id = path.partition("::")
        source = Path(file_name)
        source = source if source.is_absolute() else ROOT / source
        if source.suffix != ".py" or not source.is_file():
            parser.error("Every selection must name an existing Python test file")
        selected.append(str(source) + (separator + node_id if separator else ""))
    sys.path.insert(0, str(ROOT))
    _install_sdk_guards()
    import pytest

    return pytest.main(["-q", "-p", "no:cacheprovider", "--", *selected])


if __name__ == "__main__":
    raise SystemExit(main())

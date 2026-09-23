"""Offline cross-role checks for the explicitly selected Dummy course dev release.

This does not contact AWS, verify permissions, read keys, or deploy resources.
Actual Lambda timeouts and SQS settings must be supplied by the operator.
"""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mock_journey.aws_settings import AwsSettings


class BundleError(ValueError):
    pass


def validate_bundle(documents, *, api_timeout, worker_timeout, relay_timeout,
                    queue_visibility, batch_window):
    """Reject individually valid configurations that cannot serve one journey."""
    try:
        if type(documents) is not dict or set(documents) != {"api", "worker", "relay"}:
            raise BundleError("ROLE_SET_INVALID")
        parsed = {role: AwsSettings.parse(documents[role], role) for role in documents}
    except BundleError:
        raise
    except Exception:
        raise BundleError("ROLE_CONFIGURATION_INVALID") from None
    api, worker, relay = (parsed[role] for role in ("api", "worker", "relay"))
    for role in (worker, relay):
        if any(getattr(api, key) != getattr(role, key) for key in
               ("account_id", "partition", "environment", "region")):
            raise BundleError("ROLE_SCOPE_MISMATCH")
        if api.state.table_name != role.state.table_name:
            raise BundleError("STATE_TABLE_MISMATCH")
    if api.role_settings.storage != worker.role_settings.storage:
        raise BundleError("STORAGE_MISMATCH")
    if api.execution != worker.execution:
        raise BundleError("EXECUTION_VERSION_MISMATCH")
    if api.course is None or api.course != worker.course:
        raise BundleError("DUMMY_COURSE_CONFIGURATION_MISMATCH")
    timeouts = (api_timeout, worker_timeout, relay_timeout)
    if any(type(value) is not int or not 1 <= value <= 900 for value in timeouts):
        raise BundleError("LAMBDA_TIMEOUT_INVALID")
    if (type(batch_window) is not int or not 0 <= batch_window <= 300
            or type(queue_visibility) is not int or not 0 <= queue_visibility <= 43200):
        raise BundleError("QUEUE_TIMING_INVALID")
    # AWS guidance: six function timeouts plus the batch window. This is a
    # configuration check, not a guarantee of completion or exactly-once work.
    if queue_visibility < 6 * worker_timeout + batch_window:
        raise BundleError("QUEUE_VISIBILITY_TOO_SHORT")
    for role, seconds in zip((api, worker, relay), timeouts):
        reserve = role.logs.flush_budget_ms + role.logs.response_reserve_ms
        if role.role == "worker":
            reserve += role.timing[2]
        elif role.role == "relay":
            budget = role.relay_budget
            reserve = budget.acquire_ms + budget.step_ms + budget.reserve_ms
        if seconds * 1000 <= reserve:
            raise BundleError("INVOCATION_BUDGET_TOO_SHORT")
    return {"status": "dummy_dev_bundle_valid", "roles": ["api", "worker", "relay"],
            "aws_resources_verified": False, "secrets_verified": False,
            "deployment_performed": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for role in ("api", "worker", "relay"):
        parser.add_argument(f"--{role}-config", required=True, type=Path)
        parser.add_argument(f"--{role}-timeout", required=True, type=int, help="Configured Lambda seconds")
    parser.add_argument("--queue-visibility", required=True, type=int, help="Configured SQS seconds")
    parser.add_argument("--batch-window", required=True, type=int, help="Configured SQS batching seconds")
    args = parser.parse_args(argv)
    try:
        documents = {}
        for role in ("api", "worker", "relay"):
            # Only role configuration is read; keyrings and entire .env files
            # are deliberately outside this command's input contract.
            with getattr(args, f"{role}_config").open("rb") as stream:
                body = stream.read(65537)
            if len(body) > 65536:
                raise BundleError("CONFIGURATION_FILE_TOO_LARGE")
            documents[role] = body.decode("utf-8")
        result = validate_bundle(documents, api_timeout=args.api_timeout,
                                 worker_timeout=args.worker_timeout, relay_timeout=args.relay_timeout,
                                 queue_visibility=args.queue_visibility, batch_window=args.batch_window)
    except BundleError as error:
        result = {"status": "dummy_dev_bundle_invalid", "code": str(error),
                  "aws_resources_verified": False, "deployment_performed": False}
    except Exception:
        result = {"status": "dummy_dev_bundle_invalid", "code": "CONFIGURATION_READ_FAILED",
                  "aws_resources_verified": False, "deployment_performed": False}
    print(json.dumps(result))
    return 0 if result["status"] == "dummy_dev_bundle_valid" else 2


if __name__ == "__main__":
    raise SystemExit(main())

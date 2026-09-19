"""Prepare an explicit, create-only Relay initialization request offline.

This module never creates an SDK client or calls AWS. The operator reviews the
private request, verifies their destination/account, and applies it separately.
An existing row requires reviewed recovery; runtime never invokes this tool.
"""

import argparse
import json
import os
from pathlib import Path
from types import SimpleNamespace

from mock_journey.aws_settings import AwsSettings


def initialization_request(raw_config):
    settings = AwsSettings.parse(raw_config, "relay")
    from mock_journey.relay_progress import DynamoRelayProgress
    # The repository's pure constructor/initialization serializer needs no
    # credential or SDK object. Supplying None cannot initiate network work.
    state = SimpleNamespace(client=None, table_name=settings.state.table_name,
                            max_conflict_retries=settings.state.max_conflict_retries)
    progress = DynamoRelayProgress(state, environment=settings.environment,
                                  partition=settings.partition, account_id=settings.account_id,
                                  region=settings.region, queue_url=settings.role_settings.queue_url)
    return progress.initialization_request()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Prepare a private create-only Relay request without contacting AWS.")
    parser.add_argument("--config", required=True, help="Private relay role JSON")
    parser.add_argument("--output", required=True, help="New private request file; existing paths are refused")
    args = parser.parse_args(argv)
    try:
        request = initialization_request(Path(args.config).read_text(encoding="utf-8"))
        payload = json.dumps(request, ensure_ascii=True, allow_nan=False, indent=2) + "\n"
        # O_EXCL also refuses a pre-existing symlink. No mkdir, chmod of an
        # existing path, secret output, implicit resource lookup or overwrite.
        descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(payload)
    except Exception:
        print(json.dumps({"status": "initialization_request_not_prepared", "aws_access_checked": False,
                          "aws_write_performed": False}))
        return 2
    print(json.dumps({"status": "initialization_request_prepared", "aws_access_checked": False,
                      "aws_write_performed": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""AWS scope identifier rules shared by the AWS settings, Relay checkpoint and log store.

Pure patterns and helpers only (no SDK, no environment). Every consumer keeps
its own validation step and its own error (settings: invalid(), Relay:
ValueError("Invalid relay progress scope."), logs: OperationalLogError), so a
constructor used outside AwsSettings.parse still checks its scope itself.
scripts/deployment_preflight.py applies a separate, stricter partition policy.
"""

import hashlib


# The one environment-name length bound (mock_journey.settings re-exports it for
# ApiSettings and AuthManager, which apply it as a plain length check).
MAX_ENVIRONMENT_LENGTH = 128
ENVIRONMENT_PATTERN = r"[A-Za-z0-9_.-]{1,%d}" % MAX_ENVIRONMENT_LENGTH
ACCOUNT_ID_PATTERN = r"[0-9]{12}"
REGION_PATTERN = r"[a-z]{2}(?:-[a-z]+)+-[0-9]+"
TABLE_NAME_PATTERN = r"[A-Za-z0-9_.-]{3,255}"
PARTITIONS = ("aws", "aws-cn", "aws-us-gov")


def partition_matches_region(partition, region):
    """China and GovCloud regions belong to exactly their own partitions."""
    return (region.startswith("cn-") == (partition == "aws-cn")
            and region.startswith("us-gov-") == (partition == "aws-us-gov"))


def environment_namespace(prefix, environment):
    """A per-environment row prefix (RELAY_SCAN#…, OPS#…): prefix + sha256(environment)."""
    return prefix + hashlib.sha256(environment.encode()).hexdigest()

"""Named local-server values shared by the CLI, runtime, HTTP and chart modules.

This module imports nothing, so the CLI may read its defaults before it
isolates the environment and before any application module is imported.
The values are local implementation defaults (DECISIONS D29 for the chart
TTL; the local 1,000,000/8,000,000/1 GiB/60-second defaults), not ARC, AWS
or operating quotas. Changing one is a behavior change, not a refactor.
"""

# D29: a signed local chart capability lives exactly this many seconds.
CHART_URL_TTL_SECONDS = 300

# Control (non-measurement) request body and request header caps, in bytes.
BODY_LIMIT = 16 * 1024
HEADER_LIMIT = 16 * 1024

# CLI/LocalOptions starting values (tunable by flags; see LOCAL_RUN).
DEFAULT_CALCULATION_BODY_BYTES = 1_000_000
DEFAULT_ARTIFACT_BYTES = 8_000_000
DEFAULT_STORAGE_QUOTA_BYTES = 1_073_741_824
DEFAULT_WORKER_LEASE_SECONDS = 60
DEFAULT_WORKER_RETRY_SECONDS = 5
DEFAULT_WORKER_POLL_SECONDS = 0.25

"""ARC submission entrypoint, disabled until an official contract is configured.

This module has no transport, credentials, environment discovery, or side
effects. Its disabled result must never be reported as an ARC submission.
"""

from services.submission_response import disabled_submission_status


def submit_arc() -> dict:
    """Return the current non-submission outcome without contacting ARC."""
    return disabled_submission_status()


def run(event, context) -> dict:
    """Lambda entrypoint; supplied events cannot enable external submission."""
    return submit_arc()

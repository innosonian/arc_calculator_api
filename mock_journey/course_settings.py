"""Explicit fixture limits for VCC course tests. These are not operating quotas."""

from dataclasses import dataclass


_ERROR = "Invalid course settings."

# T7 injected fixture values. Callers that omit a field fail construction.
FIXTURE_MAX_COURSE_ITEMS = 64
FIXTURE_MAX_ASSIGNMENTS = 100
FIXTURE_MAX_BUNDLE_BYTES = 262144
FIXTURE_MAX_CONTROL_BODY_BYTES = 16384
FIXTURE_MAX_INTERVALS_PER_REPORT = 128
FIXTURE_MAX_MERGED_INTERVALS_PER_START = 512
FIXTURE_MAX_REPORTS_PER_START = 4096
FIXTURE_MAX_TRANSACTION_ACTIONS = 20
FIXTURE_MAX_CONFLICT_RETRIES = 4


def _invalid():
    return ValueError(_ERROR)


def _positive(value):
    # bool is not an int limit. Zero/negative values are configuration failures.
    if type(value) is not int or value <= 0:
        raise _invalid()


@dataclass(frozen=True)
class CourseSettings:
    max_course_items: int
    max_assignments: int
    max_bundle_bytes: int
    max_control_body_bytes: int
    max_intervals_per_report: int
    max_merged_intervals_per_start: int
    max_reports_per_start: int
    max_transaction_actions: int
    max_conflict_retries: int

    def __post_init__(self):
        for name in (
            "max_course_items", "max_assignments", "max_bundle_bytes", "max_control_body_bytes",
            "max_intervals_per_report", "max_merged_intervals_per_start", "max_reports_per_start",
            "max_transaction_actions", "max_conflict_retries",
        ):
            _positive(getattr(self, name))
        # Conflict retries stay inside the existing journey bound of 8.
        if self.max_conflict_retries > 8:
            raise _invalid()


def fixture_course_settings():
    """T7 fixture values. Tests and assembly must pass this object explicitly."""
    return CourseSettings(
        max_course_items=FIXTURE_MAX_COURSE_ITEMS,
        max_assignments=FIXTURE_MAX_ASSIGNMENTS,
        max_bundle_bytes=FIXTURE_MAX_BUNDLE_BYTES,
        max_control_body_bytes=FIXTURE_MAX_CONTROL_BODY_BYTES,
        max_intervals_per_report=FIXTURE_MAX_INTERVALS_PER_REPORT,
        max_merged_intervals_per_start=FIXTURE_MAX_MERGED_INTERVALS_PER_START,
        max_reports_per_start=FIXTURE_MAX_REPORTS_PER_START,
        max_transaction_actions=FIXTURE_MAX_TRANSACTION_ACTIONS,
        max_conflict_retries=FIXTURE_MAX_CONFLICT_RETRIES,
    )

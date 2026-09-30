"""Shared /api/v2 course-boundary primitives with explicit per-call policy.

Several course modules used to carry their own copy of these checks, each with a
slightly different public error code or rule. The copies are merged here only as
code; every rule difference stays a caller argument (D119): callers pass the
code they have always raised, and whether a text check also rejects a lone
surrogate. Unifying those policies is an app contract change, not cleanup.

No SDK, file, socket, or environment access.
"""

from datetime import datetime, timezone

from mock_journey.course_errors import CourseError


def fail(code):
    raise CourseError(code)


def require_text(value, *, code, check_utf8):
    """Nonempty str. With check_utf8, an unencodable str (lone surrogate) also fails."""
    if type(value) is not str or not value:
        fail(code)
    if check_utf8:
        try:
            value.encode("utf-8")
        except UnicodeError:
            raise CourseError(code) from None
    return value


def require_exact_bool(value, *, code):
    if type(value) is not bool:
        fail(code)
    return value


def require_exact_int(value, *, code):
    # bool is not an int here.
    if type(value) is not int:
        fail(code)
    return value


def require_natural_int(value, *, code):
    if type(value) is not int or value < 0:
        fail(code)
    return value


def require_positive_int(value, *, code):
    if type(value) is not int or value <= 0:
        fail(code)
    return value


def contains_key(value, keys):
    """True when a dict key at any depth of dict/list nesting is in keys.

    Values are never inspected. Each dict key is checked before its value is
    descended, in iteration order, and the walk stops at the first hit. The walk
    is recursive, so a nesting deeper than the interpreter limit still raises
    RecursionError to the caller, as the per-module copies did.
    """
    if type(value) is dict:
        for key, nested in value.items():
            if key in keys:
                return True
            if contains_key(nested, keys):
                return True
        return False
    if type(value) is list:
        for nested in value:
            if contains_key(nested, keys):
                return True
    return False


def rfc3339_precise(epoch_seconds, *, code):
    """Wire RFC3339 UTC keeping sub-second digits without trailing zeros.

    bool, non-numbers and NaN fail with ``code``. Other float edge cases (such
    as infinity) raise the datetime error unchanged.
    """
    if type(epoch_seconds) is bool or type(epoch_seconds) not in (int, float):
        fail(code)
    if type(epoch_seconds) is float and epoch_seconds != epoch_seconds:
        fail(code)
    dt = datetime.fromtimestamp(epoch_seconds, timezone.utc)
    if dt.microsecond == 0:
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    fraction = f"{dt.microsecond:06d}".rstrip("0")
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + fraction + "Z"


def rfc3339_seconds(seconds):
    """Wire RFC3339 UTC truncated to whole seconds with ``int()``.

    Same output as the whole-second copies in course_wiring, course_state and
    calculation.chart_link for years 1000-9999 (the strftime copy pads years
    below 1000 per platform). ``int()`` accepts bool and raises ValueError for
    NaN, unchanged.
    """
    return datetime.fromtimestamp(int(seconds), timezone.utc).isoformat().replace("+00:00", "Z")

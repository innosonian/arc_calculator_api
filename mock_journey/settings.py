"""Pure, explicit settings for assembling the existing journey roles.

These checks establish local syntax and constructor constraints, not approved
destinations, persistence, deployment sizing, or verified calculator contracts.
Clients, clocks, credentials, bindings and contracts are supplied separately.
"""

from dataclasses import dataclass
import math
import re
from urllib.parse import urlsplit

from mock_journey.aws_scope import MAX_ENVIRONMENT_LENGTH  # noqa: F401 (re-export; the one 128)


_ERROR = "Invalid journey settings."
_SEGMENT = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
# Shared bounds (also enforced by DynamoStateRepository and the AWS settings
# patterns). Each module keeps its own error type and message.
MAX_CONFLICT_RETRIES = 8


def _invalid():
    return ValueError(_ERROR)


def _text(value):
    if type(value) is not str or not value:
        raise _invalid()
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise _invalid() from None


def require_positive(value, error, *, number=False, finite=False):
    """A positive value of an exact type (bool is never accepted); raise ``error()`` otherwise.

    ``number`` also admits float; ``finite`` applies math.isfinite (which
    raises OverflowError for an int beyond float range). Checks run in that
    order. Callers choose the options and the error they already used.
    """
    if (type(value) not in ((int, float) if number else (int,))
            or (finite and not math.isfinite(value)) or value <= 0):
        raise error()
    return value


def _positive(*values):
    for value in values:
        require_positive(value, _invalid)


def base64_body_bytes(decoded_bytes):
    """Base64 text length of a decoded byte quota (4 * ceil(n / 3)).

    Same bound as local_server LocalJourneyLimits.payload_limit. It is a
    necessary size relation only, not an estimate of the JSON envelope.
    """
    _positive(decoded_bytes)
    return 4 * ((decoded_bytes + 2) // 3)


def _state(value):
    if type(value) is not StateSettings:
        raise _invalid()


def _storage(value):
    if type(value) is not StorageSettings:
        raise _invalid()


def _queue_url(value):
    _text(value)
    # urlsplit strips some control characters. Reject them before parsing so
    # validation cannot approve a different spelling from the supplied URL.
    if (not value.startswith("https://") or "\\" in value
            or "?" in value or "#" in value
            or any(char.isspace() or ord(char) < 32 or 127 <= ord(char) <= 159 for char in value)):
        raise _invalid()
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not parsed.netloc or not parsed.hostname
                or parsed.username is not None or parsed.password is not None):
            raise _invalid()
        if parsed.netloc.startswith("["):
            # urlsplit accepts garbage after a bracketed host while exposing
            # only the valid host portion through .hostname.
            suffix = parsed.netloc[parsed.netloc.index("]") + 1:]
            if suffix and not suffix.startswith(":"):
                raise _invalid()
        # Access validates a supplied port without resolving or contacting it.
        parsed.port
    except ValueError:
        raise _invalid() from None


# Public name of the queue URL check (the Relay checkpoint binds the same URL).
check_queue_url = _queue_url


def lease_renewal_exceeds(lease_seconds, interval_seconds, timeout_seconds):
    """The shared lease-renewal timing rule: renew at least three times per lease and finish in time.

    Only the comparison is shared; callers keep their own type/sign checks and
    their own error (AWS settings: invalid(), AwsLeaseGuardFactory: ValueError).
    """
    return interval_seconds > lease_seconds / 3 or interval_seconds + timeout_seconds >= lease_seconds


@dataclass(frozen=True)
class StateSettings:
    table_name: str
    max_conflict_retries: int

    def __post_init__(self):
        _text(self.table_name)
        _positive(self.max_conflict_retries)
        if self.max_conflict_retries > MAX_CONFLICT_RETRIES:
            raise _invalid()


@dataclass(frozen=True)
class StorageSettings:
    stage: str
    bucket: str
    directory: str
    input_bytes: int
    artifact_bytes: int

    def __post_init__(self):
        _text(self.stage)
        _text(self.bucket)
        _text(self.directory)
        if (not _SEGMENT.fullmatch(self.stage)
                or any(not _SEGMENT.fullmatch(part) for part in self.directory.split("/"))):
            raise _invalid()
        _positive(self.input_bytes, self.artifact_bytes)
        if self.artifact_bytes < self.input_bytes:
            raise _invalid()


@dataclass(frozen=True)
class ApiSettings:
    state: StateSettings
    storage: StorageSettings
    environment: str
    payload_limit: int

    def __post_init__(self):
        _state(self.state)
        _storage(self.storage)
        _text(self.environment)
        if len(self.environment) > MAX_ENVIRONMENT_LENGTH:
            raise _invalid()
        _positive(self.payload_limit)


@dataclass(frozen=True)
class WorkerSettings:
    state: StateSettings
    storage: StorageSettings
    lease_seconds: int
    retry_seconds: int

    def __post_init__(self):
        _state(self.state)
        _storage(self.storage)
        _positive(self.lease_seconds, self.retry_seconds)


@dataclass(frozen=True)
class RelaySettings:
    state: StateSettings
    queue_url: str
    lease_seconds: int
    retry_seconds: int
    page_size: int
    max_pages: int

    def __post_init__(self):
        _state(self.state)
        _queue_url(self.queue_url)
        _positive(self.lease_seconds, self.retry_seconds, self.page_size, self.max_pages)

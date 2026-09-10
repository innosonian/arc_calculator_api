"""Pure, explicit settings for assembling the existing journey roles.

These checks establish local syntax and constructor constraints, not approved
destinations, persistence, deployment sizing, or verified calculator contracts.
Clients, clocks, credentials, bindings and contracts are supplied separately.
"""

from dataclasses import dataclass
import re
from urllib.parse import urlsplit


_ERROR = "Invalid journey settings."
_SEGMENT = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


def _invalid():
    return ValueError(_ERROR)


def _text(value):
    if type(value) is not str or not value:
        raise _invalid()
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise _invalid() from None


def _positive(*values):
    if any(type(value) is not int or value <= 0 for value in values):
        raise _invalid()


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


@dataclass(frozen=True)
class StateSettings:
    table_name: str
    max_conflict_retries: int

    def __post_init__(self):
        _text(self.table_name)
        _positive(self.max_conflict_retries)
        if self.max_conflict_retries > 8:
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
        if len(self.environment) > 128:
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

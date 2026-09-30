"""Shared private-file primitives for the local installation (0700/0600, no links).

Only the standard library is imported, so the CLI may use this module before
it isolates the environment. Every primitive leaves the failure policy with
its caller: predicates return a bool, and the raising helpers receive the
caller's own error factory. This keeps each caller's exception type, fixed
message/code and check order unchanged (database.py raises fixed messages and
walks symlinks root-first with an lstat pre-check; object_storage.py raises
codes, walks leaf-first and has no pre-check). Raw OS/decoder errors that the
callers let escape (missing files, O_NOFOLLOW refusals, invalid JSON/base64)
still escape unchanged. A failed verified replace leaves its temporary file,
exactly as before; cleanup would be a behavior change.
"""

import base64
import json
import os
import stat


# installation.json and object-storage.json are read with this exact bound.
SMALL_RECORD_LIMIT = 4096


def private_file_violation(info):
    """True unless ``info`` is a single-link regular file owned by this user with mode 0600."""
    return (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1)


def private_directory_violation(info):
    """True unless ``info`` is a directory owned by this user with mode 0700."""
    return (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700)


def reject_symlink_components(path, *, root_first, error):
    """lstat ``path`` and every ancestor in the given order; raise ``error()`` at the first link.

    A missing component raises the raw FileNotFoundError from lstat, so the
    walk order decides which failure a caller observes.
    """
    components = (path, *path.parents)
    for component in (reversed(components) if root_first else components):
        if stat.S_ISLNK(component.lstat().st_mode):
            raise error()


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def unique_pairs(error):
    """A json ``object_pairs_hook`` that raises ``error()`` on a duplicate key."""
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise error()
            result[key] = value
        return result
    return pairs


def read_small_private_file(path, *, check, too_large, lstat_first):
    """Return at most SMALL_RECORD_LIMIT + 1 bytes of a validated private file.

    ``check(info)`` raises for a non-private file. With ``lstat_first`` the
    path is also checked before opening (rejecting FIFOs/devices/links with
    the caller's file error). The open never follows a link or blocks.
    """
    if lstat_first:
        check(path.lstat())
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        check(info)
        if info.st_size > SMALL_RECORD_LIMIT:
            raise too_large()
        with os.fdopen(fd, "rb", closefd=False) as stream:
            return stream.read(SMALL_RECORD_LIMIT + 1)
    finally:
        os.close(fd)


def is_canonical_key(text, length=32):
    """Strict base64 of exactly ``length`` bytes that re-encodes to the same text.

    Invalid base64 raises the decoder's binascii.Error, as the callers did.
    """
    decoded = base64.b64decode(text, validate=True)
    return len(decoded) == length and base64.b64encode(decoded).decode("ascii") == text


def create_private_file(name, content, *, check, dir_fd=None):
    """Create a new 0600 file (O_EXCL, never following a link), write ``content`` and fsync it.

    ``check(info)`` validates the fstat of the just-created descriptor with the
    caller's error. ``content`` may be bytes or a zero-argument callable that
    produces them after the check (so an encoding failure leaves the created
    empty file exactly as the callers' inline code did). The descriptor is
    always closed; the file itself is left for the caller's replace/unlink
    policy, and the directory is not synced here.
    """
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dir_fd)
    try:
        check(os.fstat(fd))
        raw = content() if callable(content) else content
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(fd)
    finally:
        os.close(fd)


def write_private_json(path, value, *, check, temporary=None, verify=None):
    """Create a new 0600 compact sorted-key JSON file, fsync it and fsync the directory.

    Without ``temporary`` the file is created at ``path`` (never overwritten).
    With ``temporary``, that new file is written first, ``verify()`` re-reads
    and compares the current ``path`` (raising the caller's error), and only
    then ``os.replace`` installs it. On failure the temporary is left as-is.
    """
    destination = path if temporary is None else temporary
    create_private_file(destination, lambda: json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8"),
                        check=check)
    if temporary is not None:
        verify()
        os.replace(temporary, path)
    sync_directory(path.parent)

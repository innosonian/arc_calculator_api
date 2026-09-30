"""Exact failure types/messages/codes and check order of the private-file primitives.

database.py (installation.json), object_storage.py (object-storage.json) and
the CLI lock deliberately differ: fixed human messages vs codes, an lstat
pre-check only for installation.json, root-first vs leaf-first symlink walks,
NaN rejection only in object storage and a leftover temporary on a failed
replace. These tests pin every difference before any shared helper exists.
No sockets, DB or SDK calls.
"""

import binascii
import errno
import json
import os
from pathlib import Path
import stat

import pytest

from local_server import cli
from local_server import database as db
from local_server import object_storage as objects
from local_server import private_fs


FILE_MESSAGE = "Local data files must be private regular files with mode 0600."
DIRECTORY_MESSAGE = "Local data directories must be owned by this user with mode 0700."
SYMLINK_MESSAGE = "Local data paths must not contain symbolic links."
GENERIC_MESSAGE = "Local database installation is unavailable or inconsistent."
KEY = "A" * 43 + "="  # canonical base64 of 32 zero-ish bytes
RECORD = {"version": 1, "installation_id": "a" * 32, "environment": "local-" + "a" * 32,
          "resume_key": KEY, "database_initialized": False}
MATERIAL = {"schema_version": 1, "installation_id": "a" * 32, "chart_key": KEY, "initialized": False}


@pytest.fixture
def root(tmp_path):
    value = tmp_path.resolve() / "private"
    value.mkdir(mode=0o700)
    return value


def write(path, data, mode=0o600):
    path.write_bytes(data if type(data) is bytes else data.encode())
    path.chmod(mode)
    return path


def database_error(call, message):
    with pytest.raises(db.LocalDatabaseError) as error:
        call()
    assert type(error.value) is db.LocalDatabaseError and str(error.value) == message


def object_error(call, code):
    with pytest.raises(objects.LocalObjectError) as error:
        call()
    assert error.value.code == code and str(error.value) == code


def test_key_constant_is_canonical_32_bytes():
    import base64
    assert len(base64.b64decode(KEY, validate=True)) == 32


# --- installation.json (database) -------------------------------------------------

def test_record_roundtrip_and_exact_4096_byte_bound(root):
    raw = json.dumps(RECORD).encode()
    assert db.read_installation_record(write(root / "r.json", raw + b" " * (4096 - len(raw)))) == RECORD
    database_error(lambda: db.read_installation_record(write(root / "s.json", raw + b" " * (4097 - len(raw)))), GENERIC_MESSAGE)


@pytest.mark.parametrize("kind", ["mode", "hardlink", "directory", "fifo", "symlink"])
def test_record_file_checks_happen_before_open_with_the_file_message(root, kind):
    path = root / "installation.json"
    if kind == "mode":
        write(path, json.dumps(RECORD), 0o644)
    elif kind == "hardlink":
        write(path, json.dumps(RECORD))
        os.link(path, root / "copy")
    elif kind == "directory":
        path.mkdir(mode=0o700)
    elif kind == "fifo":
        os.mkfifo(path, 0o600)
    else:
        write(root / "target", json.dumps(RECORD))
        path.symlink_to(root / "target")
    # The lstat pre-check rejects before O_NOFOLLOW/O_NONBLOCK opening.
    database_error(lambda: db.read_installation_record(path), FILE_MESSAGE)


def test_record_missing_path_is_a_raw_file_not_found(root):
    with pytest.raises(FileNotFoundError):
        db.read_installation_record(root / "missing.json")


@pytest.mark.parametrize("data,expected", [
    ('{"version": 1, "version": 1}', GENERIC_MESSAGE),
    (json.dumps({**RECORD, "version": True}), GENERIC_MESSAGE),
    (json.dumps({**RECORD, "extra": 1}), GENERIC_MESSAGE),
    (json.dumps([RECORD]), GENERIC_MESSAGE),
    (json.dumps({**RECORD, "environment": "local-b"}), GENERIC_MESSAGE),
    (json.dumps({**RECORD, "database_initialized": 0}), GENERIC_MESSAGE),
    (json.dumps({**RECORD, "resume_key": "AAAA"}), GENERIC_MESSAGE),
    # Non-canonical padding bits decode but do not round-trip.
    (json.dumps({**RECORD, "resume_key": "A" * 42 + "B="}), GENERIC_MESSAGE),
    # NaN is parsed (no parse_constant) and then fails the exact type check.
    (json.dumps(RECORD).replace('"version": 1', '"version": NaN'), GENERIC_MESSAGE),
])
def test_record_content_failures_are_generic(root, data, expected):
    database_error(lambda: db.read_installation_record(write(root / "r.json", data)), expected)


def test_record_decoder_errors_propagate_raw(root):
    with pytest.raises(json.JSONDecodeError):
        db.read_installation_record(write(root / "a.json", "{"))
    with pytest.raises(UnicodeDecodeError):
        db.read_installation_record(write(root / "b.json", b"\xff\xfe\xfd"))
    with pytest.raises(binascii.Error):
        db.read_installation_record(write(root / "c.json", json.dumps({**RECORD, "resume_key": "!!!!"})))


def test_record_write_is_exclusive_compact_sorted_and_private(root):
    path = root / "installation.json"
    db._write_record(path, RECORD)
    assert path.read_bytes() == json.dumps(RECORD, sort_keys=True, separators=(",", ":")).encode()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        db._write_record(path, RECORD)
    assert sorted(item.name for item in root.iterdir()) == ["installation.json"]


def test_record_replace_keeps_only_the_initialized_flag_change(root):
    path = root / "installation.json"
    db._write_record(path, RECORD)
    db._write_record(path, {**RECORD, "database_initialized": True}, replace=True)
    assert json.loads(path.read_bytes())["database_initialized"] is True
    assert sorted(item.name for item in root.iterdir()) == ["installation.json"]


def test_record_replace_mismatch_fails_and_leaves_the_temporary_copy(root):
    path = root / "installation.json"
    db._write_record(path, RECORD)
    before = path.read_bytes()
    database_error(lambda: db._write_record(path, {**RECORD, "environment": "local-" + "b" * 32}, replace=True),
                   GENERIC_MESSAGE)
    assert path.read_bytes() == before
    leftovers = [item.name for item in root.iterdir() if item.name != "installation.json"]
    assert len(leftovers) == 1 and leftovers[0].startswith(".installation-") and len(leftovers[0]) == 14 + 32


def test_database_directory_and_symlink_walk_is_root_first(root, tmp_path):
    database_error(lambda: db._no_symlink_components(Path("relative/dir")), GENERIC_MESSAGE)
    database_error(lambda: db._no_symlink_components(root / ".." / "private"), GENERIC_MESSAGE)
    link = tmp_path.resolve() / "link"
    link.symlink_to(root, target_is_directory=True)
    # A symlinked ancestor is reported even though a deeper component is missing.
    database_error(lambda: db._no_symlink_components(link / "missing" / "leaf"), SYMLINK_MESSAGE)
    with pytest.raises(FileNotFoundError):
        db._no_symlink_components(root / "missing" / "leaf")
    root.chmod(0o750)
    try:
        database_error(lambda: db._private_directory(root), DIRECTORY_MESSAGE)
    finally:
        root.chmod(0o700)
    db._private_directory(root)


def test_prepare_material_maps_raw_failures_to_the_generic_message(root):
    db.prepare_material(root)
    write(root / db.MATERIAL_FILENAME, "{", 0o600)
    database_error(lambda: db.prepare_material(root), GENERIC_MESSAGE)
    (root / db.MATERIAL_FILENAME).chmod(0o644)
    database_error(lambda: db.prepare_material(root), FILE_MESSAGE)


def test_directory_sync_does_not_follow_links(root, tmp_path):
    private_fs.sync_directory(root)
    link = tmp_path.resolve() / "dir-link"
    link.symlink_to(root, target_is_directory=True)
    with pytest.raises(OSError) as error:
        private_fs.sync_directory(link)
    # O_DIRECTORY|O_NOFOLLOW on a link: platform errno (macOS ENOTDIR).
    assert error.value.errno in (errno.ELOOP, errno.ENOTDIR)


def test_create_private_file_is_exclusive_checked_and_leaves_the_file_to_the_caller(root, tmp_path):
    """The shared creation skeleton of both JSON records and object envelopes."""
    checks = []
    private_fs.create_private_file(root / "new", b"payload", check=checks.append)
    assert (root / "new").read_bytes() == b"payload" and stat.S_IMODE((root / "new").stat().st_mode) == 0o600
    assert len(checks) == 1 and stat.S_ISREG(checks[0].st_mode) and checks[0].st_size == 0
    # Never overwrites, never follows a link at the name (O_EXCL on the link itself).
    with pytest.raises(FileExistsError):
        private_fs.create_private_file(root / "new", b"other", check=checks.append)
    (root / "alias").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(FileExistsError):
        private_fs.create_private_file(root / "alias", b"x", check=checks.append)
    assert (root / "new").read_bytes() == b"payload" and not (tmp_path / "elsewhere").exists()
    assert len(checks) == 1

    # The caller's check failure escapes unchanged; the created empty file is
    # left for the caller's own unlink/replace policy, and nothing is written.
    class Refused(Exception):
        pass

    def refuse(info):
        raise Refused()
    with pytest.raises(Refused):
        private_fs.create_private_file(root / "refused", lambda: pytest.fail("content produced after a failed check"),
                                       check=refuse)
    assert (root / "refused").read_bytes() == b""
    # Deferred content (the JSON records) is produced after the check; dir_fd
    # names the file relative to an open directory (the object envelopes).
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        private_fs.create_private_file("relative", lambda: b"via-dir-fd", check=lambda info: None, dir_fd=fd)
    finally:
        os.close(fd)
    assert (root / "relative").read_bytes() == b"via-dir-fd"
    assert stat.S_IMODE((root / "relative").stat().st_mode) == 0o600


# --- object-storage.json (object storage) -----------------------------------------

def test_material_roundtrip_and_exact_4096_byte_bound(root):
    raw = json.dumps(MATERIAL).encode()
    assert objects._read_material(write(root / "m.json", raw + b" " * (4096 - len(raw)))) == MATERIAL
    object_error(lambda: objects._read_material(write(root / "n.json", raw + b" " * (4097 - len(raw)))),
                 "LOCAL_STORAGE_UNAVAILABLE")


@pytest.mark.parametrize("data,code", [
    ('{"a": 1, "a": 1}', "LOCAL_OBJECT_INVALID"),
    (json.dumps(MATERIAL).replace('"schema_version": 1', '"schema_version": NaN'), "LOCAL_OBJECT_INVALID"),
    (json.dumps(MATERIAL).replace('"schema_version": 1', '"schema_version": Infinity'), "LOCAL_OBJECT_INVALID"),
    (json.dumps({**MATERIAL, "schema_version": 1.0}), "LOCAL_STORAGE_UNAVAILABLE"),
    (json.dumps({**MATERIAL, "extra": 1}), "LOCAL_STORAGE_UNAVAILABLE"),
    (json.dumps({**MATERIAL, "installation_id": "A" * 32}), "LOCAL_STORAGE_UNAVAILABLE"),
    (json.dumps({**MATERIAL, "initialized": 1}), "LOCAL_STORAGE_UNAVAILABLE"),
    (json.dumps({**MATERIAL, "chart_key": "AAAA"}), "LOCAL_STORAGE_UNAVAILABLE"),
    (json.dumps({**MATERIAL, "chart_key": "A" * 42 + "B="}), "LOCAL_STORAGE_UNAVAILABLE"),
])
def test_material_content_failures_keep_their_codes(root, data, code):
    object_error(lambda: objects._read_material(write(root / "m.json", data)), code)


def test_material_raw_failures_propagate(root):
    with pytest.raises(json.JSONDecodeError):
        objects._read_material(write(root / "a.json", "{"))
    with pytest.raises(UnicodeDecodeError):
        objects._read_material(write(root / "b.json", b"\xff\xfe"))
    with pytest.raises(binascii.Error):
        objects._read_material(write(root / "c.json", json.dumps({**MATERIAL, "chart_key": "!!!!"})))
    with pytest.raises(FileNotFoundError):
        objects._read_material(root / "missing.json")


@pytest.mark.parametrize("kind", ["mode", "hardlink", "directory", "fifo"])
def test_material_file_checks_use_fstat_after_open(root, kind):
    path = root / "object-storage.json"
    if kind == "mode":
        write(path, json.dumps(MATERIAL), 0o644)
    elif kind == "hardlink":
        write(path, json.dumps(MATERIAL))
        os.link(path, root / "copy")
    elif kind == "directory":
        path.mkdir(mode=0o700)
    else:
        os.mkfifo(path, 0o600)
    object_error(lambda: objects._read_material(path), "LOCAL_STORAGE_UNAVAILABLE")


def test_material_symlink_is_refused_by_the_open_itself(root):
    write(root / "target", json.dumps(MATERIAL))
    (root / "object-storage.json").symlink_to(root / "target")
    # No lstat pre-check: the raw O_NOFOLLOW error escapes this primitive.
    with pytest.raises(OSError) as error:
        objects._read_material(root / "object-storage.json")
    assert error.value.errno == errno.ELOOP


def test_material_write_first_and_replace(root):
    path = root / "object-storage.json"
    objects._write_material(path, MATERIAL, first=True)
    assert path.read_bytes() == json.dumps(MATERIAL, sort_keys=True, separators=(",", ":")).encode()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        objects._write_material(path, MATERIAL, first=True)
    objects._write_material(path, {**MATERIAL, "initialized": True}, first=False)
    assert json.loads(path.read_bytes())["initialized"] is True
    assert sorted(item.name for item in root.iterdir()) == ["object-storage.json"]


def test_material_replace_mismatch_fails_and_leaves_the_temporary_copy(root):
    path = root / "object-storage.json"
    objects._write_material(path, MATERIAL, first=True)
    before = path.read_bytes()
    object_error(lambda: objects._write_material(path, {**MATERIAL, "chart_key": "B" * 43 + "="}, first=False),
                 "LOCAL_STORAGE_UNAVAILABLE")
    assert path.read_bytes() == before
    leftovers = [item.name for item in root.iterdir() if item.name != "object-storage.json"]
    assert len(leftovers) == 1 and leftovers[0].startswith(".object-material-") and len(leftovers[0]) == 17 + 32


def test_object_directory_walk_is_leaf_first(root, tmp_path):
    object_error(lambda: objects._directory("relative"), "LOCAL_STORAGE_UNAVAILABLE")
    object_error(lambda: objects._directory(root / ".." / "private"), "LOCAL_STORAGE_UNAVAILABLE")
    link = tmp_path.resolve() / "link"
    link.symlink_to(root, target_is_directory=True)
    # Unlike the database walk, the missing leaf is reached before the link.
    with pytest.raises(FileNotFoundError):
        objects._directory(link / "missing")
    object_error(lambda: objects._directory(link), "LOCAL_STORAGE_UNAVAILABLE")
    root.chmod(0o750)
    try:
        object_error(lambda: objects._directory(root), "LOCAL_STORAGE_UNAVAILABLE")
    finally:
        root.chmod(0o700)
    assert objects._directory(str(root)) == root


def test_object_file_primitive_code(root):
    write(root / "f", b"x", 0o640)
    object_error(lambda: objects._file(os.stat(root / "f")), "LOCAL_STORAGE_UNAVAILABLE")
    (root / "f").chmod(0o600)
    assert objects._file(os.stat(root / "f")) is None


# --- CLI data directory and lock ---------------------------------------------------

def test_cli_lock_messages(tmp_path):
    base = tmp_path.resolve()
    with pytest.raises(cli.StartupError) as error:
        with cli.installation_lock(base / "a" / ".." / "b"):
            pass
    assert str(error.value) == "Use a data directory without parent traversal."
    public = base / "public"
    public.mkdir(mode=0o755)
    public.chmod(0o755)
    with pytest.raises(cli.StartupError) as error:
        with cli.installation_lock(public):
            pass
    assert str(error.value) == "The data directory must be owned by you with permissions 0700."
    # A missing directory is created (0700) because is_symlink() is False for it.
    created = base / "new" / "dir"
    with cli.installation_lock(created) as held:
        assert held == created and stat.S_IMODE(created.stat().st_mode) == 0o700
    for damage in ("mode", "hardlink"):
        data = base / ("lock-" + damage)
        data.mkdir(mode=0o700)
        lock = write(data / "server.lock", b"", 0o600)
        if damage == "mode":
            lock.chmod(0o640)
        else:
            os.link(lock, base / ("other-" + damage))
        with pytest.raises(cli.StartupError) as error:
            with cli.installation_lock(data):
                pass
        assert str(error.value) == "Invalid local server lock file."
    link = base / "alias"
    link.symlink_to(created, target_is_directory=True)
    with pytest.raises(cli.StartupError) as error:
        with cli.installation_lock(link / "child"):
            pass
    assert str(error.value) == "The data directory must not contain symlinks."

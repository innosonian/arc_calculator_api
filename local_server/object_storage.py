"""Private local objects behind the existing JourneyStorage interface.

No SDK clients or network operations. The CLI owns the installation lock;
object operations additionally use a cross-process flock and a thread lock.
"""

import base64
from contextlib import contextmanager
from dataclasses import dataclass, field
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import struct
from threading import RLock
import uuid

from botocore.exceptions import ClientError

from local_server.constants import CHART_URL_TTL_SECONDS
from local_server.private_fs import (
    create_private_file, is_canonical_key, private_directory_violation, private_file_violation,
    read_small_private_file, reject_symlink_components, sync_directory, unique_pairs, write_private_json,
)
from mock_journey import typed
from util import legacy_layout, uploader


_MATERIAL = "object-storage.json"
_MAGIC = b"ARCLOCAL1\x00"
_HEADER_LIMIT = 16 * 1024
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_IDENT = re.compile(r"[0-9a-f]{32}\Z")
_SEGMENT = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_OBJECT = re.compile(r"[0-9a-f]{64}\.object\Z")
_TEMPORARY = re.compile(r"\.object-tmp-[0-9a-f]{32}\Z")
# Signed chart keys: org / date / stem + chart suffix, under the stage prefix. Groups 1-3 are
# consumed below; the stem pattern's own groups come after them.
_CHART_KEY = re.compile(
    r"(_no_org|[0-9a-fA-F-]{36})/([0-9]{4}-[0-9]{2}-[0-9]{2})/("
    + legacy_layout.KEY_STEM.pattern.removesuffix(r"\Z") + r")" + re.escape(legacy_layout.CHART_SUFFIX)
)
_META_KEYS = frozenset(("arc-binding", "arc-sha256", "arc-chart"))


class LocalObjectError(RuntimeError):
    """Only fixed diagnostics; never nested filesystem messages or key values."""

    def __init__(self, code="LOCAL_STORAGE_UNAVAILABLE"):
        self.code = code
        super().__init__(code)


class _ObjectMissing(Exception):
    pass


def _fail(code="LOCAL_STORAGE_UNAVAILABLE"):
    raise LocalObjectError(code)


def _file(info):
    if private_file_violation(info):
        _fail()


def _directory(path):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        _fail()
    # Leaf first; every failure here is mapped by callers to a fixed code.
    reject_symlink_components(path, root_first=False, error=LocalObjectError)
    info = path.lstat()
    if private_directory_violation(info):
        _fail()
    return path


_pairs = unique_pairs(lambda: LocalObjectError("LOCAL_OBJECT_INVALID"))


def _json(body):
    return json.loads(body.decode("utf-8"), object_pairs_hook=_pairs,
                      parse_constant=lambda _: _fail("LOCAL_OBJECT_INVALID"))


def _read_material(path):
    # No lstat pre-check here (unlike installation.json): O_NOFOLLOW and the
    # fstat check reject links and non-private files after opening.
    value = _json(read_small_private_file(path, check=_file, too_large=LocalObjectError, lstat_first=False))
    if (type(value) is not dict or set(value) != {"schema_version", "installation_id", "chart_key", "initialized"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or type(value["installation_id"]) is not str or not _IDENT.fullmatch(value["installation_id"])
            or type(value["chart_key"]) is not str or type(value["initialized"]) is not bool):
        _fail()
    if not is_canonical_key(value["chart_key"]):
        _fail()
    return value


def _write_material(path, value, *, first):
    def verify():
        old = _read_material(path)
        if {**old, "initialized": value["initialized"]} != value:
            _fail()
    write_private_json(path, value, check=_file, verify=verify,
                       temporary=None if first else path.with_name(".object-material-" + uuid.uuid4().hex))


@dataclass(frozen=True)
class ObjectMaterial:
    data_dir: Path
    object_dir: Path
    installation_id: str
    signing_key: bytes = field(repr=False)


def prepare_object_material(local_material):
    """Create/recover private material while the caller owns installation_lock.

    Existing objects without their key are never adopted. Losing initialized
    objects never creates a fresh directory with the old installation identity.
    """
    try:
        from local_server.database import LocalMaterial, MATERIAL_FILENAME, read_installation_record

        if type(local_material) is not LocalMaterial:
            _fail()
        data_dir = _directory(local_material.data_dir)
        parent = read_installation_record(data_dir / MATERIAL_FILENAME)
        if (parent["installation_id"] != local_material.installation_id
                or parent["environment"] != local_material.environment):
            _fail()
        path, objects = data_dir / _MATERIAL, data_dir / "objects"
        if not path.exists() and not path.is_symlink():
            if objects.exists() or objects.is_symlink():
                _fail()
            value = {"schema_version": 1, "installation_id": local_material.installation_id,
                     "chart_key": base64.b64encode(secrets.token_bytes(32)).decode("ascii"), "initialized": False}
            _write_material(path, value, first=True)
        value = _read_material(path)
        if value["installation_id"] != local_material.installation_id:
            _fail()
        if not objects.exists() and not objects.is_symlink():
            if value["initialized"]:
                _fail()
            objects.mkdir(mode=0o700)
            sync_directory(data_dir)
        _directory(objects)
        if not value["initialized"]:
            # A pending installation may recover mkdir, never adopt objects.
            if any(objects.iterdir()):
                _fail()
            value["initialized"] = True
            _write_material(path, value, first=False)
        return ObjectMaterial(data_dir, objects, local_material.installation_id,
                              base64.b64decode(value["chart_key"], validate=True))
    except LocalObjectError:
        raise
    except Exception:
        raise LocalObjectError() from None


class LocalObjectClient:
    def __init__(self, material, *, bucket, directory, stage, artifact_limit, quota_bytes):
        self._fd = self._lock_fd = None
        self._threads = RLock()
        try:
            if (type(material) is not ObjectMaterial or type(material.signing_key) is not bytes
                    or len(material.signing_key) != 32 or not _IDENT.fullmatch(material.installation_id)
                    or any(type(value) is not int or value <= 0 for value in (artifact_limit, quota_bytes))
                    or type(bucket) is not str or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", bucket)
                    or type(directory) is not str or any(not _SEGMENT.fullmatch(part) for part in directory.split("/"))
                    or type(stage) is not str or not _SEGMENT.fullmatch(stage)):
                _fail()
            _directory(material.data_dir)
            root = _directory(material.object_dir)
            if root != material.data_dir / "objects":
                _fail()
            record = _read_material(material.data_dir / _MATERIAL)
            if (record["installation_id"] != material.installation_id or not record["initialized"]
                    or base64.b64decode(record["chart_key"]) != material.signing_key):
                _fail()
            self.material, self.bucket, self.directory, self.stage = material, bucket, directory, stage
            self.artifact_limit, self.quota_bytes = artifact_limit, quota_bytes
            self.prefix = legacy_layout.stage_prefix(directory, stage)
            self._fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            self._lock_fd = os.open("objects.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                                    0o600, dir_fd=self._fd)
            _file(os.fstat(self._lock_fd))
            os.fsync(self._fd)
        except Exception:
            self.close()
            raise LocalObjectError() from None

    def close(self):
        with self._threads:
            for attribute in ("_lock_fd", "_fd"):
                descriptor = getattr(self, attribute, None)
                if descriptor is not None:
                    os.close(descriptor)
                    setattr(self, attribute, None)

    def ready(self):
        """Check the owned directory/lock only, without listing private objects."""
        try:
            with self._locked():
                return True
        except Exception:
            raise LocalObjectError() from None

    @contextmanager
    def _locked(self):
        with self._threads:
            if self._fd is None or self._lock_fd is None:
                _fail()
            directory_info = os.fstat(self._fd)
            if private_directory_violation(directory_info):
                _fail()
            _file(os.fstat(self._lock_fd))
            # Reject a replaced lock name: all cooperating processes must lock
            # the same inode; checking permissions alone does not prove that.
            linked = os.stat("objects.lock", dir_fd=self._fd, follow_symlinks=False)
            opened = os.fstat(self._lock_fd)
            if (linked.st_dev, linked.st_ino) != (opened.st_dev, opened.st_ino):
                _fail()
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)

    def _key(self, bucket, key):
        if (bucket != self.bucket or type(key) is not str or len(key) > 2048
                or not key.startswith(self.prefix) or "\\" in key
                or any(not re.fullmatch(r"[A-Za-z0-9_.-]{1,256}", part) or part in (".", "..") for part in key.split("/"))):
            _fail("LOCAL_OBJECT_INVALID")
        return hashlib.sha256(typed.json_bytes([self.material.installation_id, bucket, key])).hexdigest()

    def _metadata(self, value):
        if (type(value) is not dict or not set(value) <= _META_KEYS
                or any(type(item) is not str or not _HASH.fullmatch(item) for item in value.values())):
            _fail("LOCAL_OBJECT_INVALID")
        return dict(value)

    def _read(self, ident):
        if type(ident) is not str or not _HASH.fullmatch(ident):
            _fail("LOCAL_OBJECT_INVALID")
        try:
            fd = os.open(ident + ".object", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self._fd)
        except FileNotFoundError:
            raise _ObjectMissing() from None
        try:
            info = os.fstat(fd)
            _file(info)
            if info.st_size > len(_MAGIC) + 4 + _HEADER_LIMIT + self.artifact_limit:
                _fail("LOCAL_OBJECT_INVALID")
            with os.fdopen(fd, "rb", closefd=False) as stream:
                if stream.read(len(_MAGIC)) != _MAGIC:
                    _fail("LOCAL_OBJECT_INVALID")
                encoded_length = stream.read(4)
                if len(encoded_length) != 4:
                    _fail("LOCAL_OBJECT_INVALID")
                length = struct.unpack("!I", encoded_length)[0]
                if not 1 <= length <= _HEADER_LIMIT:
                    _fail("LOCAL_OBJECT_INVALID")
                header = _json(stream.read(length))
                if (type(header) is not dict or set(header) != {"schema_version", "installation_id", "bucket", "key", "size", "sha256", "metadata"}
                        or type(header["schema_version"]) is not int or header["schema_version"] != 1
                        or header["installation_id"] != self.material.installation_id
                        or self._key(header["bucket"], header["key"]) != ident
                        or type(header["size"]) is not int or not 0 <= header["size"] <= self.artifact_limit
                        or type(header["sha256"]) is not str or not _HASH.fullmatch(header["sha256"])):
                    _fail("LOCAL_OBJECT_INVALID")
                self._metadata(header["metadata"])
                body = stream.read(header["size"] + 1)
                if (len(body) != header["size"] or hashlib.sha256(body).hexdigest() != header["sha256"]
                        or info.st_size != len(_MAGIC) + 4 + length + len(body)):
                    _fail("LOCAL_OBJECT_INVALID")
                return header, body
        finally:
            os.close(fd)

    def _used(self):
        total = 0
        for name in os.listdir(self._fd):
            if name != "objects.lock" and not _OBJECT.fullmatch(name) and not _TEMPORARY.fullmatch(name):
                _fail()
            info = os.stat(name, dir_fd=self._fd, follow_symlinks=False)
            _file(info)
            total += info.st_size
        return total

    @staticmethod
    def _client_error(code, operation):
        return ClientError({"Error": {"Code": code, "Message": "Local object operation failed."}}, operation)

    def put_object(self, *, Bucket, Key, Body, Metadata=None):
        try:
            ident = self._key(Bucket, Key)
            metadata = self._metadata({} if Metadata is None else Metadata)
            if type(Body) is not bytes or len(Body) > self.artifact_limit:
                _fail("LOCAL_OBJECT_INVALID")
            header = {"schema_version": 1, "installation_id": self.material.installation_id,
                      "bucket": Bucket, "key": Key, "size": len(Body),
                      "sha256": hashlib.sha256(Body).hexdigest(), "metadata": metadata}
            encoded = typed.json_bytes(header)
            if len(encoded) > _HEADER_LIMIT:
                _fail("LOCAL_OBJECT_INVALID")
            envelope = _MAGIC + struct.pack("!I", len(encoded)) + encoded + Body
            with self._locked():
                try:
                    old, body = self._read(ident)
                except _ObjectMissing:
                    old = None
                if old is not None:
                    if old != header or body != Body:
                        _fail("LOCAL_OBJECT_INVALID")
                    os.fsync(self._fd)
                    return {}
                if self._used() + len(envelope) > self.quota_bytes:
                    _fail("LOCAL_STORAGE_QUOTA_EXCEEDED")
                temporary = ".object-tmp-" + uuid.uuid4().hex
                try:
                    # Same skeleton as the JSON records (create, fstat check,
                    # fsync); the binary envelope, dir_fd and the unlink of a
                    # failed temporary stay here.
                    create_private_file(temporary, envelope, check=_file, dir_fd=self._fd)
                    os.replace(temporary, ident + ".object", src_dir_fd=self._fd, dst_dir_fd=self._fd)
                    os.fsync(self._fd)
                finally:
                    try:
                        os.unlink(temporary, dir_fd=self._fd)
                    except FileNotFoundError:
                        pass
            return {}
        except Exception:
            raise self._client_error("LocalStorageUnavailable", "PutObject") from None

    def get_object(self, *, Bucket, Key):
        try:
            ident = self._key(Bucket, Key)
            with self._locked():
                header, body = self._read(ident)
            return {"Body": io.BytesIO(body), "ContentLength": len(body), "Metadata": dict(header["metadata"])}
        except _ObjectMissing:
            raise self._client_error("NoSuchKey", "GetObject") from None
        except Exception:
            raise self._client_error("LocalStorageUnavailable", "GetObject") from None

    def chart_object(self, *, key=None, ident=None):
        """Read only a generated published chart, never an arbitrary JSON file."""
        try:
            if (key is None) == (ident is None):
                _fail("LOCAL_OBJECT_INVALID")
            if key is not None:
                ident = self._key(self.bucket, key)
            with self._locked():
                header, body = self._read(ident)
            relative = header["key"][len(self.prefix):]
            match = re.fullmatch(_CHART_KEY, relative)
            if (not match or uploader.org_prefix(match[1]) != match[1]
                    or uploader.date_prefix(match[3]) != match[2]
                    or str(uuid.UUID(match[3][-36:])) != match[3][-36:]):
                _fail("LOCAL_OBJECT_INVALID")
            # _no_org is the fallback sentinel, not an organization UUID.
            if type(typed.parse_json(body)) not in (dict, list):
                _fail("LOCAL_OBJECT_INVALID")
            return ident, body
        except Exception:
            raise LocalObjectError("LOCAL_CHART_UNAVAILABLE") from None


class LocalLegacyBindings:
    """Legacy storage layout with an injected local client and chart signer."""

    build_key_stem = staticmethod(uploader.build_key_stem)
    org_prefix = staticmethod(uploader.org_prefix)
    date_prefix = staticmethod(uploader.date_prefix)
    build_raw_input_meta = staticmethod(uploader.build_raw_input_meta)

    def __init__(self, client, chart_service):
        if type(client) is not LocalObjectClient or getattr(chart_service, "client", None) is not client:
            _fail()
        self.client, self.chart_service = client, chart_service
        self.bucket, self.directory = client.bucket, client.directory

    def _base(self, stage, key_stem, org, directory):
        if stage != self.client.stage or directory != self.directory:
            _fail()
        return legacy_layout.object_base(directory, stage, org, self.date_prefix(key_stem), key_stem)

    def upload_raw_input(self, cpr_bytes, aed_bytes, meta, *, stage, key_stem, org, directory):
        base = self._base(stage, key_stem, org, directory)
        self.client.put_object(Bucket=self.bucket, Key=base + legacy_layout.RAW_SUFFIX, Body=cpr_bytes)
        self.client.put_object(Bucket=self.bucket, Key=base + legacy_layout.META_SUFFIX, Body=typed.json_bytes(meta))
        if aed_bytes:
            self.client.put_object(Bucket=self.bucket, Key=base + legacy_layout.AED_SUFFIX, Body=aed_bytes)
        return base

    def upload_json_file(self, data, *, directory, stage, key_stem, org):
        key = self._base(stage, key_stem, org, directory) + legacy_layout.CHART_SUFFIX
        self.client.put_object(Bucket=self.bucket, Key=key, Body=typed.json_bytes(data))
        return key

    def create_signed_url(self, key, *, expires_in=CHART_URL_TTL_SECONDS):
        return self.chart_service.create_signed_url(key, expires_in=expires_in)

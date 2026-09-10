"""Test-only file storage; no deployment defaults or external chart service."""

import base64
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
from threading import RLock

from botocore.exceptions import ClientError

from tests.mock_storage_support import MemoryLegacyBindings


class FileObjects:
    """Persist bytes/metadata across new test application and worker instances."""

    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.lock = RLock()
        self.sign_calls = []

    def _path(self, bucket, key):
        name = hashlib.sha256(json.dumps([bucket, key]).encode()).hexdigest()
        return self.root / (name + ".json")

    def put_object(self, *, Bucket, Key, Body, Metadata=None, **kwargs):
        data = Body.encode() if type(Body) is str else Body
        assert type(data) is bytes
        record = {"bucket": Bucket, "key": Key, "body": base64.b64encode(data).decode(),
                  "metadata": deepcopy(Metadata or {})}
        with self.lock:
            fd, temporary = tempfile.mkstemp(dir=self.root)
            try:
                with os.fdopen(fd, "w") as stream:
                    json.dump(record, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self._path(Bucket, Key))
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        return {}

    def get_object(self, *, Bucket, Key):
        with self.lock:
            try:
                item = json.loads(self._path(Bucket, Key).read_text())
            except FileNotFoundError:
                raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject") from None
            assert item["bucket"] == Bucket and item["key"] == Key
            body = base64.b64decode(item["body"], validate=True)
        return {"Body": io.BytesIO(body), "ContentLength": len(body), "Metadata": item["metadata"]}

    def generate_presigned_url(self, operation, *, Params, ExpiresIn):
        # The test verifies stored chart content and requested lifetime. This
        # URL is deliberately not an implemented local chart download route.
        self.sign_calls.append((operation, deepcopy(Params), ExpiresIn))
        digest = hashlib.sha256(json.dumps(Params, sort_keys=True).encode()).hexdigest()
        return f"https://charts.example.invalid/{digest}?lifetime={ExpiresIn}"

    def keys(self):
        with self.lock:
            return [json.loads(path.read_text())["key"] for path in self.root.glob("*.json")]


FileLegacyBindings = MemoryLegacyBindings

"""Test-only object storage and legacy bindings; never import in runtime."""

from copy import deepcopy
import io
import json
from threading import RLock

from botocore.exceptions import ClientError

from util import uploader


class MemoryS3:
    def __init__(self):
        self.objects = {}
        self.lock = RLock()
        self.fail_put_suffix = None
        self.put_keys = []
        self.sign_calls = []

    def put_object(self, *, Bucket, Key, Body, Metadata=None, **kwargs):
        if self.fail_put_suffix and Key.endswith(self.fail_put_suffix):
            raise ClientError({"Error": {"Code": "InternalError", "Message": "private-storage-error"}}, "PutObject")
        data = Body.encode() if type(Body) is str else Body
        assert type(data) is bytes
        with self.lock:
            self.objects[(Bucket, Key)] = {"Body": data, "Metadata": deepcopy(Metadata or {})}
            self.put_keys.append(Key)
        return {}

    def get_object(self, *, Bucket, Key):
        with self.lock:
            if (Bucket, Key) not in self.objects:
                raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
            item = deepcopy(self.objects[(Bucket, Key)])
        return {"Body": io.BytesIO(item["Body"]), "Metadata": item["Metadata"], "ContentLength": len(item["Body"])}

    def generate_presigned_url(self, operation, *, Params, ExpiresIn):
        self.sign_calls.append((operation, deepcopy(Params), ExpiresIn))
        return f'https://charts.example.invalid/{Params["Key"]}?lifetime={ExpiresIn}&generation={len(self.sign_calls)}'


class MemoryLegacyBindings:
    bucket = uploader.BUCKET
    directory = uploader.RTDATA_DIRECTORY

    def __init__(self, client):
        self.client = client
        self.build_key_stem = uploader.build_key_stem
        self.org_prefix = uploader.org_prefix
        self.date_prefix = uploader.date_prefix
        self.build_raw_input_meta = uploader.build_raw_input_meta

    def upload_raw_input(self, cpr_bytes, aed_bytes, meta, *, stage, key_stem, org, directory):
        if stage == "test":
            return None
        base = f"{directory}/{stage}/{org}/{self.date_prefix(key_stem)}/{key_stem}"
        self.client.put_object(Bucket=self.bucket, Key=base + ".bin", Body=cpr_bytes or b"")
        self.client.put_object(Bucket=self.bucket, Key=base + ".meta.json", Body=json.dumps(meta, default=str))
        if aed_bytes:
            self.client.put_object(Bucket=self.bucket, Key=base + ".aed.bin", Body=aed_bytes)
        return base

    def upload_json_file(self, data, *, directory, stage, key_stem, org):
        if stage == "test":
            return None
        key = f"{directory}/{stage}/{org}/{self.date_prefix(key_stem)}/{key_stem}.json"
        self.client.put_object(Bucket=self.bucket, Key=key, Body=json.dumps(data))
        return key

    def create_signed_url(self, key, *, expires_in=300):
        if not key:
            return None
        return self.client.generate_presigned_url("get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=expires_in)

"""Legacy path/meta/JSON conventions bound to one explicit private S3 client."""

import json

from util import uploader


class AwsLegacyBindings:
    def __init__(self, client, settings):
        if client is None:
            raise ValueError("Invalid AWS storage binding.")
        self.client = client
        self.bucket, self.directory, self.stage = settings.bucket, settings.directory, settings.stage
        self.build_key_stem = uploader.build_key_stem
        self.org_prefix = uploader.org_prefix
        self.date_prefix = uploader.date_prefix
        self.build_raw_input_meta = uploader.build_raw_input_meta

    def _base(self, directory, stage, key_stem, org):
        if directory != self.directory or stage != self.stage:
            raise ValueError("Invalid AWS storage binding.")
        return f"{directory}/{stage}/{org}/{self.date_prefix(key_stem)}/{key_stem}"

    def upload_raw_input(self, cpr_bytes, aed_bytes, meta, *, stage, key_stem, org, directory):
        base = self._base(directory, stage, key_stem, org)
        self.client.put_object(Bucket=self.bucket, Key=base + ".bin", Body=cpr_bytes or b"")
        self.client.put_object(Bucket=self.bucket, Key=base + ".meta.json", Body=json.dumps(meta, default=str))
        if aed_bytes:
            self.client.put_object(Bucket=self.bucket, Key=base + ".aed.bin", Body=aed_bytes)
        return base

    def upload_json_file(self, data, *, directory, stage, key_stem, org):
        key = self._base(directory, stage, key_stem, org) + ".json"
        self.client.put_object(Bucket=self.bucket, Key=key, Body=json.dumps(data))
        return key

    def create_signed_url(self, key, *, expires_in=300):
        if (type(key) is not str or not key.startswith(f"{self.directory}/{self.stage}/")
                or not key.endswith(".json") or expires_in != 300):
            raise ValueError("Invalid AWS chart binding.")
        return self.client.generate_presigned_url("get_object", Params={"Bucket": self.bucket, "Key": key},
                                                  ExpiresIn=expires_in)

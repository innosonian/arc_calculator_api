"""Bound, checksummed S3 artifacts around the existing raw/chart helpers."""

from dataclasses import dataclass
import hashlib
import re
import uuid

from botocore.exceptions import BotoCoreError, ClientError

from mock_journey.errors import JourneyError
from mock_journey.projection import LoadedInput, ProjectedInput, typed_identity
from mock_journey import typed
from util import uploader


_INPUT_BINDING = frozenset(("attempt_id", "epoch", "input_digest", "adapter_version", "projection_version"))
_CALL_BINDING = _INPUT_BINDING | {"job_id", "call_id"}
_VERSIONS = frozenset(("adapter_version", "projection_version"))
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_SEGMENT = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


def _hash(body):
    return hashlib.sha256(body).hexdigest()


def _invalid():
    return JourneyError("STORED_INPUT_INVALID")


@dataclass(frozen=True)
class LegacyBindings:
    bucket: str = uploader.BUCKET
    directory: str = uploader.RTDATA_DIRECTORY
    build_key_stem: object = uploader.build_key_stem
    org_prefix: object = uploader.org_prefix
    date_prefix: object = uploader.date_prefix
    build_raw_input_meta: object = uploader.build_raw_input_meta
    upload_raw_input: object = uploader.upload_raw_input
    upload_json_file: object = uploader.upload_json_file
    create_signed_url: object = uploader.create_signed_url


class JourneyStorage:
    def __init__(self, client, *, legacy_bindings, stage, limits, namespace=None):
        self.client = client
        self.legacy = legacy_bindings
        namespace = namespace or {"bucket": legacy_bindings.bucket, "directory": legacy_bindings.directory}
        if (type(stage) is not str or not _SEGMENT.fullmatch(stage)
                or namespace != {"bucket": legacy_bindings.bucket, "directory": legacy_bindings.directory}
                or type(limits) is not dict or set(limits) != {"input_bytes", "artifact_bytes"}
                or any(type(value) is not int or value <= 0 for value in limits.values())
                or limits["artifact_bytes"] < limits["input_bytes"]):
            raise ValueError("Explicit valid storage configuration is required.")
        self.stage = stage
        self.bucket = namespace["bucket"]
        self.directory = namespace["directory"]
        if type(self.directory) is not str or any(not _SEGMENT.fullmatch(part) for part in self.directory.split("/")):
            raise ValueError("Invalid storage namespace.")
        self.prefix = f"{self.directory}/{stage}/"
        self.input_limit = limits["input_bytes"]
        self.artifact_limit = limits["artifact_bytes"]

    def _binding(self, binding, *, call):
        fields = _CALL_BINDING if call else _INPUT_BINDING
        if type(binding) is not dict or set(binding) != fields or any(
            type(value) is not str or not value or (key not in _VERSIONS and not _SEGMENT.fullmatch(value))
            for key, value in binding.items()
        ) or not _SHA.fullmatch(binding["input_digest"]):
            raise _invalid()
        # Versions bind the metadata but never form a path segment. Preserve
        # their configured spelling (including dots) instead of restricting
        # them with the rule for generated identifiers.
        try:
            typed.canonical_bytes(binding)
        except (ValueError, UnicodeError):
            raise _invalid() from None
        return binding

    def _planned(self, key):
        if type(key) is not str or not key.startswith(self.prefix) or any(
            not part or part in (".", "..") or "\\" in part for part in key.split("/")
        ):
            raise _invalid()
        return {"bucket": self.bucket, "key": key}

    def _ref(self, ref, *, complete):
        fields = {"bucket", "key", "sha256", "size"} if complete else {"bucket", "key"}
        if type(ref) is not dict or set(ref) != fields or ref.get("bucket") != self.bucket:
            raise _invalid()
        self._planned(ref["key"])
        if complete and (type(ref["sha256"]) is not str or not _SHA.fullmatch(ref["sha256"])
                         or type(ref["size"]) is not int or not 0 <= ref["size"] <= self.artifact_limit):
            raise _invalid()
        return ref

    def _read(self, ref, *, complete=True, binding=None, optional=False, extra_metadata=None,
              missing_code="STORED_INPUT_INVALID"):
        self._ref(ref, complete=complete)
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=ref["key"])
            stream = response["Body"]
            try:
                body = stream.read(self.artifact_limit + 1)
            finally:
                stream.close()
            if (type(body) is not bytes or len(body) > self.artifact_limit
                    or response.get("ContentLength") != len(body)):
                raise _invalid()
            checksum = _hash(body)
            if complete and (checksum != ref["sha256"] or len(body) != ref["size"]):
                raise _invalid()
            if binding is not None:
                metadata = response.get("Metadata") or {}
                if metadata.get("arc-binding") != typed.digest(binding) or metadata.get("arc-sha256") != checksum:
                    raise _invalid()
            if extra_metadata and any(response.get("Metadata", {}).get(key) != value for key, value in extra_metadata.items()):
                raise _invalid()
            return body
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") in {"NoSuchKey", "404", "NotFound"}:
                if optional:
                    return None
                # A committed reference must remain readable. A newly written
                # candidate has not yet been accepted and may be retried.
                raise JourneyError(missing_code) from None
            raise JourneyError("TEMPORARILY_UNAVAILABLE") from None
        except (BotoCoreError, KeyError, TypeError, ValueError, UnicodeError):
            raise JourneyError("TEMPORARILY_UNAVAILABLE") from None

    def _put(self, planned, body, binding, *, extra_metadata=None):
        self._ref(planned, complete=False)
        if type(body) is not bytes or len(body) > self.artifact_limit:
            raise _invalid()
        metadata = {"arc-binding": typed.digest(binding), "arc-sha256": _hash(body), **(extra_metadata or {})}
        try:
            self.client.put_object(Bucket=self.bucket, Key=planned["key"], Body=body, Metadata=metadata)
        except (ClientError, BotoCoreError):
            raise JourneyError("TEMPORARILY_UNAVAILABLE") from None
        ref = {**planned, "sha256": _hash(body), "size": len(body)}
        self._read(ref, binding=binding, extra_metadata=extra_metadata,
                   missing_code="TEMPORARILY_UNAVAILABLE")
        return ref

    def _artifact(self, binding, kind, fence=None):
        self._binding(binding, call=True)
        if fence is not None and (type(fence) is not int or fence < 1):
            raise _invalid()
        name = f'{binding["job_id"]}-{binding["call_id"]}-{kind}-{fence or 0}-{uuid.uuid4().hex}.bin'
        return self._planned(self.prefix + "_artifacts/" + name)

    def _call_ref(self, ref, binding, kind, *, complete):
        self._ref(ref, complete=complete)
        expected_start = self.prefix + f'_artifacts/{binding["job_id"]}-{binding["call_id"]}-{kind}-'
        suffix = ref["key"][len(expected_start):] if ref["key"].startswith(expected_start) else ""
        if not re.fullmatch(r"[0-9]+-[0-9a-f]{32}\.bin", suffix):
            raise _invalid()

    def save_input(self, projected, binding):
        self._binding(binding, call=False)
        if typed_identity(projected) != binding["input_digest"]:
            raise _invalid()
        if len(projected.cpr_bytes) + len(projected.aed_bytes) > self.input_limit:
            raise JourneyError("PAYLOAD_TOO_LARGE")
        definition = projected.payload["definition"]
        if any(definition.get(field) != binding[field] for field in ("adapter_version", "projection_version")):
            raise _invalid()
        calculation = projected.payload["calculation_input"]
        organization = projected.payload["response_context"].get("Organization")
        try:
            stem = self.legacy.build_key_stem()
            org = self.legacy.org_prefix((organization or {}).get("org_id"))
            base = f"{self.prefix}{org}/{self.legacy.date_prefix(stem)}/{stem}"
            self._planned(base + ".bin")
            meta = self.legacy.build_raw_input_meta(calculation["condition"], calculation["vp_event_list"], organization)
            actual = self.legacy.upload_raw_input(projected.cpr_bytes, projected.aed_bytes, meta,
                stage=self.stage, key_stem=stem, org=org, directory=self.directory)
            if actual != base:
                raise JourneyError("TEMPORARILY_UNAVAILABLE")
            raw = {}
            bodies = {"cpr": (".bin", projected.cpr_bytes), "meta": (".meta.json", typed.json_bytes(meta))}
            if projected.aed_bytes:
                bodies["aed"] = (".aed.bin", projected.aed_bytes)
            for name, (suffix, body) in bodies.items():
                ref = {**self._planned(base + suffix), "sha256": _hash(body), "size": len(body)}
                self._read(ref, missing_code="TEMPORARILY_UNAVAILABLE")
                raw[name] = ref
            manifest = {"schema": "arc-input-v1", "binding": binding, "raw_base": base,
                        "raw": raw, "payload": projected.payload}
            ref = self._put(self._planned(base + ".request.json"), typed.json_bytes(manifest), binding)
            return {"manifest_ref": ref, "input_digest": binding["input_digest"], "raw_base": base}
        except JourneyError:
            raise
        except (ClientError, BotoCoreError, ValueError, TypeError, KeyError, AttributeError):
            raise JourneyError("TEMPORARILY_UNAVAILABLE") from None

    def load_input(self, manifest_ref, expected_binding):
        self._binding(expected_binding, call=False)
        try:
            manifest = typed.parse_json(self._read(manifest_ref, binding=expected_binding))
            if manifest["schema"] != "arc-input-v1" or manifest["binding"] != expected_binding:
                raise _invalid()
            base = manifest["raw_base"]
            if manifest_ref["key"] != base + ".request.json" or set(manifest["raw"]) not in ({"cpr", "meta"}, {"cpr", "meta", "aed"}):
                raise _invalid()
            bodies = {}
            for name, suffix in (("cpr", ".bin"), ("meta", ".meta.json"), ("aed", ".aed.bin")):
                if name in manifest["raw"]:
                    if manifest["raw"][name]["key"] != base + suffix:
                        raise _invalid()
                    bodies[name] = self._read(manifest["raw"][name])
            projected = ProjectedInput(bodies["cpr"], bodies.get("aed", b""), manifest["payload"])
            if typed_identity(projected) != expected_binding["input_digest"]:
                raise _invalid()
            if any(projected.payload["definition"].get(field) != expected_binding[field] for field in ("adapter_version", "projection_version")):
                raise _invalid()
            return LoadedInput(projected, base)
        except JourneyError:
            raise
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
            raise _invalid() from None

    def planned_calculation(self, binding):
        return self._artifact(binding, "calculation")

    def put_course_blob(self, body):
        """Persist an immutable private course snapshot using existing storage limits."""
        if type(body) is not bytes or len(body) > self.artifact_limit:
            raise _invalid()
        checksum = _hash(body)
        planned = self._planned(self.prefix + "_course/" + checksum + ".json")
        binding = {"kind": "course-blob-v1", "sha256": checksum}
        existing = self._read(planned, complete=False, binding=binding, optional=True)
        if existing is not None:
            if existing != body:
                raise _invalid()
        else:
            self._put(planned, body, binding)
        return checksum

    def get_course_blob(self, checksum):
        if type(checksum) is not str or not _SHA.fullmatch(checksum):
            raise _invalid()
        planned = self._planned(self.prefix + "_course/" + checksum + ".json")
        body = self._read(planned, complete=False,
                          binding={"kind": "course-blob-v1", "sha256": checksum}, optional=True)
        if body is not None and _hash(body) != checksum:
            raise _invalid()
        return body

    def save_calculation(self, planned_ref, raw_bytes, binding):
        self._binding(binding, call=True)
        self._call_ref(planned_ref, binding, "calculation", complete=False)
        existing = self.load_calculation(planned_ref, binding)
        if existing is not None:
            if existing != raw_bytes:
                raise _invalid()
            return {**planned_ref, "sha256": _hash(existing), "size": len(existing)}
        return self._put(planned_ref, raw_bytes, binding)

    def load_calculation(self, planned_ref, expected_binding):
        self._binding(expected_binding, call=True)
        self._call_ref(planned_ref, expected_binding, "calculation", complete=False)
        return self._read(planned_ref, complete=False, binding=expected_binding, optional=True)

    def save_chart_candidate(self, chart_data, source_sha256, binding, fence):
        if type(chart_data) not in (dict, list) or type(source_sha256) is not str or not _SHA.fullmatch(source_sha256):
            raise _invalid()
        try:
            body = typed.json_bytes(chart_data)
        except (ValueError, TypeError, RecursionError, UnicodeError):
            raise _invalid() from None
        ref = self._put(self._artifact(binding, "chart", fence), body, binding)
        return {"kind": "snapshot", "snapshot_ref": ref, "published_body_sha256": _hash(body),
                "source_sha256": source_sha256, **binding}

    def _verify_raw_base(self, base, binding):
        input_binding = {key: binding[key] for key in _INPUT_BINDING}
        body = self._read(self._planned(base + ".request.json"), complete=False, binding=input_binding)
        try:
            manifest = typed.parse_json(body)
            if manifest["binding"] != input_binding or manifest["raw_base"] != base:
                raise _invalid()
        except (ValueError, TypeError, KeyError, RecursionError):
            raise _invalid() from None

    def publish_selected_chart(self, job_id, raw_base, expected_binding, selection_reader):
        self._binding(expected_binding, call=True)
        if expected_binding["job_id"] != job_id:
            raise _invalid()
        selected = selection_reader(job_id)
        if type(selected) is not dict or any(selected.get(key) != value for key, value in expected_binding.items()):
            raise _invalid()
        revision = selected.get("revision")
        if type(revision) is not int or revision < 1:
            raise _invalid()
        common = {**expected_binding, "selection_revision": revision}
        if selected.get("kind") == "no_chart":
            return {"kind": "no_chart", **common}
        if selected.get("kind") != "snapshot":
            raise _invalid()
        self._call_ref(selected["snapshot_ref"], expected_binding, "chart", complete=True)
        body = self._read(selected["snapshot_ref"], binding=expected_binding)
        if _hash(body) != selected.get("published_body_sha256"):
            raise _invalid()
        self._verify_raw_base(raw_base, expected_binding)
        try:
            data = typed.parse_json(body)
            if typed.json_bytes(data) != body:
                raise _invalid()
            stem = raw_base.rsplit("/", 1)[-1]
            org = raw_base[len(self.prefix):].split("/", 1)[0]
            key = self.legacy.upload_json_file(data, directory=self.directory, stage=self.stage, key_stem=stem, org=org)
            if key != raw_base + ".json":
                raise _invalid()
            publication_ref = {**self._planned(key), "sha256": _hash(body), "size": len(body)}
            self._read(publication_ref, missing_code="TEMPORARILY_UNAVAILABLE")
            return {"kind": "snapshot", "key": key, "published_body_sha256": _hash(body), **common}
        except JourneyError:
            raise
        except (ClientError, BotoCoreError, ValueError, TypeError, KeyError, UnicodeError, RecursionError):
            raise JourneyError("TEMPORARILY_UNAVAILABLE") from None

    def save_final(self, response_bytes, binding, fence, chart_publication):
        if type(chart_publication) is not dict or any(chart_publication.get(key) != value for key, value in binding.items()):
            raise _invalid()
        return self._put(self._artifact(binding, "final", fence), response_bytes, binding,
                         extra_metadata={"arc-chart": typed.digest(chart_publication)})

    def read_final(self, ref, expected_binding, chart_publication):
        self._binding(expected_binding, call=True)
        self._call_ref(ref, expected_binding, "final", complete=True)
        if type(chart_publication) is not dict or any(chart_publication.get(key) != value for key, value in expected_binding.items()):
            raise _invalid()
        return self._read(ref, binding=expected_binding, extra_metadata={"arc-chart": typed.digest(chart_publication)})

    def sign_chart(self, publication):
        binding = {key: publication[key] for key in _CALL_BINDING}
        self._binding(binding, call=True)
        if publication.get("kind") == "no_chart":
            return None
        key = publication.get("key")
        checksum = publication.get("published_body_sha256")
        if publication.get("kind") != "snapshot" or type(key) is not str or not key.endswith(".json") or type(checksum) is not str or not _SHA.fullmatch(checksum):
            raise _invalid()
        self._planned(key)
        self._verify_raw_base(key[:-5], binding)
        body = self._read(self._planned(key), complete=False)
        if _hash(body) != checksum:
            raise _invalid()
        try:
            result = self.legacy.create_signed_url(key, expires_in=300)
            if type(result) is not str or not result:
                raise JourneyError("TEMPORARILY_UNAVAILABLE")
            return result
        except (ClientError, BotoCoreError, ValueError, TypeError):
            raise JourneyError("TEMPORARILY_UNAVAILABLE") from None

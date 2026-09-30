"""Seed rows captured from the /mock/v1 API into a journey store (memory or DynamoDB Local).

tests/fixtures/legacy_mock_v1_rows/ was produced once by the /mock/v1 HTTP
routes (mock_journey.handler.handle, build_application, build_relay,
build_worker) against an isolated DynamoDB Local table, before /mock/v1 was
removed. It is the data a deployed table may already hold: USER with slots,
SESSION, SESSION#.../CREATE#... receipts, and created/queued/evaluated
ATTEMPT rows with their JOB/OUTBOX rows. The queued job was accepted but never
relayed or processed; its private input files are in objects.json, so a
current Worker can finish it.

  meta.json     capture conditions: clock, session expiry, environment, key
                version and key fingerprint (no key material), storage
                namespace, attempt labels, source code base, tools.
  rows.json     raw DynamoDB items (DynamoDB JSON) exactly as scanned.
  objects.json  private objects referenced by the rows (bucket, key, metadata,
                base64 body).

SESSION.token_hash in the fixture is the hash of a discarded capture-time
token. ``issue_session_token()`` replaces it with the hash of a fresh random
test token, so no usable bearer token is stored in the repository. Resume
credentials are recomputed from the synthetic harness key and checked against
the stored resume_digest.
"""

import base64
import hashlib
import json
from pathlib import Path
import secrets
from types import SimpleNamespace

from tests.journey_support import (
    HARNESS_ENVIRONMENT, HARNESS_KEY_VERSION, HARNESS_RESUME_KEYS, HARNESS_STAGE, decode_item,
)


FIXTURE = Path(__file__).parent / "fixtures" / "legacy_mock_v1_rows"


def key_fingerprint(material):
    return hashlib.sha256(b"journey-harness-key-fingerprint\0" + material).hexdigest()


def load_legacy_fixture():
    meta = json.loads((FIXTURE / "meta.json").read_text())
    rows = json.loads((FIXTURE / "rows.json").read_text())
    objects = json.loads((FIXTURE / "objects.json").read_text())
    return meta, rows, objects


def _check_harness(meta):
    """The fixture is only meaningful with the configuration it was captured with."""
    expected = {
        "environment": HARNESS_ENVIRONMENT, "resume_key_version": HARNESS_KEY_VERSION,
        "resume_key_fingerprint": key_fingerprint(HARNESS_RESUME_KEYS[HARNESS_KEY_VERSION]),
        "storage_stage": HARNESS_STAGE,
    }
    actual = {key: meta[key] for key in expected}
    assert actual == expected, f"legacy fixture was captured with different harness settings: {actual}"


def seed_legacy_rows(store, objects=None):
    """Write every captured row (create-only) and private object; return labels and helpers.

    ``objects`` is a MemoryS3-compatible client (a new MemoryS3 by default).
    """
    from tests.mock_storage_support import MemoryLegacyBindings, MemoryS3

    meta, rows, stored = load_legacy_fixture()
    _check_harness(meta)
    objects = objects if objects is not None else MemoryS3()
    bindings = MemoryLegacyBindings(objects)
    assert (bindings.bucket, bindings.directory) == (meta["storage_bucket"], meta["storage_directory"])
    for item in rows:
        store.client.put_item(TableName=store.table, Item=item, ConditionExpression="attribute_not_exists(PK)")
    for entry in stored:
        objects.put_object(Bucket=entry["bucket"], Key=entry["key"], Body=base64.b64decode(entry["body"]),
                           Metadata=entry["metadata"])
    decoded = {(row["PK"], row["SK"]): row for row in (decode_item(item) for item in rows)}
    attempts = {}
    for label, attempt_id in meta["attempts"].items():
        row = decoded[(f"ATTEMPT#{attempt_id}", "META")]
        attempts[label] = {"attempt_id": attempt_id, "job_id": row.get("job_id"), "row": row}

    def issue_session_token():
        """Bind a fresh random test token to the captured legacy session (no other field changes)."""
        session_id = meta["session_id"]
        token = f"s1.{session_id}.{secrets.token_urlsafe(32)}"
        raw = next(item for item in rows
                   if item["PK"] == {"S": f"SESSION#{session_id}"} and item["SK"] == {"S": "AUTH"})
        store.client.put_item(
            TableName=store.table,
            Item={**raw, "token_hash": {"S": hashlib.sha256(token.encode()).hexdigest()}},
            ConditionExpression="#h = :h", ExpressionAttributeNames={"#h": "token_hash"},
            ExpressionAttributeValues={":h": raw["token_hash"]},
        )
        return token

    return SimpleNamespace(meta=meta, rows=decoded, raw_rows=rows, objects=objects, attempts=attempts,
                           session_id=meta["session_id"], principal=meta["principal"],
                           issue_session_token=issue_session_token)


def legacy_resume_credential(seeded, label):
    """The resume credential the /mock/v1 create response returned for this attempt."""
    from mock_journey.auth import AuthManager
    row = seeded.attempts[label]["row"]
    manager = AuthManager(None, seeded.meta["environment"], HARNESS_RESUME_KEYS, seeded.meta["resume_key_version"])
    credential = manager.resume_credential(row)
    assert hashlib.sha256(credential.encode()).hexdigest() == row["resume_digest"]
    return credential

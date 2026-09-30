"""Worker, file, lease and log boundary helpers on the public /api/v2 harness.

Only public entry points are used: the V2Journey harness (tests/journey_support.py)
drives ``mock_journey.handler.handle`` and the ``build_course_application`` /
``build_worker`` factories on a real DynamoDB Local table. Legacy (course_binding
absent) attempts come from the captured row fixture (tests/legacy_rows_support.py),
never from a v1 API route or a v1-only service method.

Two calculator adapters are available (tests/calculator_doubles.py):

* ``TrackedCalculator`` is the bundled internal calculator (real scoring) with
  call recording and explicit fault hooks around the actual calculation.
* ``ScriptedCalculator`` is a test-only adapter registered under the current
  adapter/projection versions. Its values test state handling, not scoring; it
  still obeys the pending-goal contract (cycles -> pending_policy).

``OneTransactionInterruption`` is tests/dynamodb_doubles.OneTransactionInterruption.
"""

from contextlib import contextmanager
import hashlib
import time
from types import SimpleNamespace
import uuid

import pytest

from tests.calculator_doubles import TYPE_MARKER, ScriptedCalculator, TrackedCalculator  # noqa: F401 (re-export)
from tests.dynamodb_doubles import OneTransactionInterruption  # noqa: F401 (re-export)
from tests.journey_support import (
    HARNESS_START, MULTIPART_CONTENT_TYPE, StorageLimits, V2Journey, dummy_course, dynamodb_local_store,
    measurement_for, multipart,
)


DUMMY_PRINCIPAL = "dummy-tester"
# The course submission receipt of a Dummy learner: excluded, never sent (D99).
DUMMY_SUBMISSION = {"status": "excluded", "ok": False, "error": None, "exclusionReasons": ["dummy"]}
# The legacy (no course binding) submission receipt stays disabled.
LEGACY_SUBMISSION = {"status": "disabled", "ok": False, "error": "arc_contract_pending", "exclusionReasons": []}


@pytest.fixture
def store(dynamodb_client):
    """A fresh GSI journey table on the explicit DynamoDB Local endpoint."""
    with dynamodb_local_store(dynamodb_client) as created:
        yield created


# ---------------------------------------------------------------------------
# Calculator adapters
# ---------------------------------------------------------------------------

def journey(store, adapter=None, **options):
    """V2Journey whose Worker uses exactly one adapter (h.calculator)."""
    adapter = adapter if adapter is not None else TrackedCalculator()
    h = V2Journey(store, adapters=[adapter], **options)
    h.calculator = adapter
    return h


# ---------------------------------------------------------------------------
# Requests and stored rows
# ---------------------------------------------------------------------------

def upload_event(h, token, attempt, *, data=None, extra=None):
    """The multipart measurement event for a started attempt (optionally with ignored extra fields)."""
    from tests.vcc_support import event as proxy_event
    upload = proxy_event("POST", f"/api/v2/attempts/{attempt['attemptId']}/calculation/", token=token,
                         content_type=MULTIPART_CONTENT_TYPE)
    payload = data if data is not None else measurement_for(attempt["condition"])
    upload.update(body=multipart(attempt["condition"], extra=extra, data=payload), isBase64Encoded=True)
    return upload


def submit(h, token, attempt, *, data=None, extra=None):
    """POST the measurement; returns the Reply (202 while pending, 200 once evaluated)."""
    event = upload_event(h, token, attempt, data=data, extra=extra)
    return h.call("POST", event["path"], event=event)


def calculation(h, token, attempt_id):
    return h.call("GET", f"/api/v2/attempts/{attempt_id}/calculation/", token=token)


def chart_link(h, token, attempt_id):
    return h.call("GET", f"/api/v2/attempts/{attempt_id}/chart-link/", token=token)


def start(h, token, course, *, role="practice", client_request_id=None, expected=201):
    link = course.final_link_id if role == "final" else course.practice_link_id
    return h.start(token, course, link, client_request_id=client_request_id, expected=expected)


def start_reply(h, token, course, *, role="practice"):
    """Unasserted start: the Reply of a new clientRequestId."""
    link = course.final_link_id if role == "final" else course.practice_link_id
    return h.call("POST", "/api/v2/attempts/", token=token, body={
        "clientRequestId": str(uuid.uuid4()), "courseId": course.course_id, "enrollmentId": course.enrollment_id,
        "courseItemLinkId": link, "definitionHash": h.course(token, course)["definitionHash"],
    })


def accepted(h, course, *, token=None, role="practice", data=None, extra=None):
    """Start and upload one attempt; returns (token, started attempt, job_id)."""
    token = token or h.login().token
    attempt = start(h, token, course, role=role)
    reply = submit(h, token, attempt, data=data, extra=extra)
    assert reply.status == 202, (reply.status, reply.body)
    return token, attempt, h.job_id(attempt["attemptId"])


def attempt_row(h, attempt_id):
    return h.store.row(f"ATTEMPT#{attempt_id}", "META")


def user_row(h, principal=DUMMY_PRINCIPAL):
    return h.store.row(f"USER#{principal}", "STATE")


def job_row(h, job_id):
    return h.worker.jobs.get_job(job_id)


def course_rows(h, attempt_id):
    """HEAD, ITEM and FINAL rows of the attempt's original course scope and epoch."""
    attempt = attempt_row(h, attempt_id)
    binding = attempt["course_binding"]
    pk, epoch = f"COURSE#{binding['scope_key']}", attempt["epoch"]
    return SimpleNamespace(
        head=h.store.row(pk, f"EPOCH#{epoch}#HEAD"),
        item=h.store.row(pk, f"EPOCH#{epoch}#ITEM#{binding['placement_key']}"),
        final=h.store.row(pk, f"EPOCH#{epoch}#FINAL"),
    )


def progress_facts(rows):
    """Course progress facts without revisions (deferred recovery bumps revisions only)."""
    item = rows.item or {}
    return {
        "head": {key: rows.head.get(key) for key in ("progress_json", "completed_placements", "definition_hash")},
        "item": {key: item.get(key) for key in ("completed", "passed", "completed_by_attempt", "completed_at")},
        "final": {key: rows.final.get(key) for key in ("phase", "active_attempt_id")},
    }


def item_view(h, token, course, link_id):
    detail = h.course(token, course)
    return next(item for item in detail["courseItems"] if item["courseItemLinkId"] == link_id)


def recovery_facts(row):
    """Attempt fields that a repeated deferral must not change."""
    return {key: row.get(key) for key in ("state", "error_code", "evaluation", "progress_application",
                                         "active_counted", "result_ref", "job_id", "input_digest")}


def jobs_over(h, client):
    """A job repository of the Worker's table and storage over a wrapped DynamoDB client."""
    from mock_journey.course_storage import CourseBlobStore
    from mock_journey.jobs import DynamoJobRepository
    from mock_journey.state import DynamoStateRepository
    return DynamoJobRepository(DynamoStateRepository(client, h.store.table, clock=h.clock),
                               course_blobs=CourseBlobStore(h.worker.storage))


def seeded_legacy(store, adapter=None, **options):
    """Legacy fixture rows + a harness on the same table while the legacy session is still active.

    The legacy fixture attempts are bound to the retained pending-v3 adapter (D136, 3A), so the
    harness's hooked adapter (h.calculator) is built under that version; pass an adapter of that
    version to replace it.
    """
    from mock_journey.contracts import PENDING_GOAL_ADAPTER_VERSION
    from tests.legacy_rows_support import seed_legacy_rows
    seeded = seed_legacy_rows(store)
    adapter = adapter if adapter is not None else TrackedCalculator(version=PENDING_GOAL_ADAPTER_VERSION)
    h = journey(store, adapter, objects=seeded.objects, start=seeded.meta["capture_clock"] + 60, **options)
    return h, seeded, seeded.issue_session_token()


# ---------------------------------------------------------------------------
# Real local files
# ---------------------------------------------------------------------------

ARTIFACT_LIMIT = 2_000_000
QUOTA_BYTES = 16_000_000
FILES_BUCKET = "local-filesystem-integration"
FILES_DIRECTORY = "calculator_result/interpreted_rtdata/arc"
CHART_BASE_URL = "http://127.0.0.1:8000"


class FilesJourney(V2Journey):
    """The /api/v2 harness on real private local files (LocalObjectClient + signed local charts).

    ``now`` may be shared between reopened instances. ``realtime_from()`` makes
    the clock follow actual elapsed time (lease renewal tests).
    """

    def __init__(self, store, data_dir, *, now=None, quota=QUOTA_BYTES, adapter=None, operations=None,
                 opened=None, **options):
        from local_server.charts import LocalChartService
        from local_server.database import prepare_material
        from local_server.object_storage import LocalLegacyBindings, LocalObjectClient, prepare_object_material

        self._operations_override = operations
        self._elapsed_from = None
        self.now = now if now is not None else [HARNESS_START]  # clock() reads it while the roles are built
        self.material = prepare_material(data_dir)
        objects = LocalObjectClient(prepare_object_material(self.material), bucket=FILES_BUCKET,
                                    directory=FILES_DIRECTORY, stage="development",
                                    artifact_limit=ARTIFACT_LIMIT, quota_bytes=quota)
        self._opened = opened if opened is not None else []
        self._opened.append(objects)
        adapter = adapter if adapter is not None else TrackedCalculator()
        # Authentication is keyed from the installation (as the local server composes it), so a
        # reopened process must accept tokens and resume credentials issued before the restart.
        options.setdefault("environment", self.material.environment)
        options.setdefault("resume_keys", {self.material.key_version: self.material.resume_key})
        options.setdefault("key_version", self.material.key_version)
        try:
            # The local file bindings (bucket/directory of the object client) and signed local charts
            # replace the memory-style bindings, so both roles are composed exactly once (E-10).
            self.charts = LocalChartService(objects, base_url=CHART_BASE_URL, clock=self.clock)
            super().__init__(store, objects=objects, bindings=LocalLegacyBindings(objects, self.charts),
                             adapters=[adapter], start=self.now[0],
                             storage_limits=StorageLimits(1_000_000, ARTIFACT_LIMIT, 2_000_000), **options)
            self.now = now if now is not None else self.now
            self.calculator = adapter
        except BaseException:
            self._opened.remove(objects)
            objects.close()
            raise

    def clock(self):
        if self._elapsed_from is None:
            return self.now[0]
        return self.now[0] + time.monotonic() - self._elapsed_from

    def realtime_from(self, started):
        self._elapsed_from = started

    def _operations(self, role):
        if self._operations_override is not None:
            return self._operations_override
        return super()._operations(role)

    def lease_worker(self, guard_factory, *, lease_seconds=2, retry_seconds=1):
        """A Worker with the given lease guard factory (None: no periodic renewal)."""
        return self.composition.build_worker(
            [self.calculator], lease_seconds=lease_seconds, retry_seconds=retry_seconds,
            lease_guard_factory=guard_factory, operations=self._operations("worker"),
        )

    def reopen(self, *, quota=QUOTA_BYTES):
        """Close the object client and open the same installation as a new process with a new adapter."""
        from local_server.database import prepare_material
        self.objects.close()
        self._opened.remove(self.objects)
        assert prepare_material(self.material.data_dir) == self.material
        return FilesJourney(self.store, self.material.data_dir, now=self.now, quota=quota, opened=self._opened)

    # -- file reads ----------------------------------------------------------
    def body(self, key):
        response = self.objects.get_object(Bucket=FILES_BUCKET, Key=key)
        stream = response["Body"]
        try:
            value = stream.read(ARTIFACT_LIMIT + 1)
        finally:
            stream.close()
        assert len(value) == response["ContentLength"] <= ARTIFACT_LIMIT
        return value

    def chart_bytes(self, url):
        from urllib.parse import urlsplit
        parts = urlsplit(url)
        assert parts.scheme == "http" and parts.netloc == "127.0.0.1:8000" and not parts.query and not parts.fragment
        return self.charts.read_path(parts.path)

    def corrupt_file(self, key):
        """Flip one byte of the stored object file; returns (path, original bytes) for exact restoration."""
        ident = self.objects._key(FILES_BUCKET, key)
        path = self.objects.material.object_dir / (ident + ".object")
        original = path.read_bytes()
        assert original
        path.write_bytes(original[:-1] + bytes((original[-1] ^ 1,)))
        return path, original

    def file_hashes(self):
        root = self.material.data_dir
        return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in root.rglob("*") if path.is_file()}


@contextmanager
def files_journey_at(store, directory, **options):
    """Open a FilesJourney in a new private directory; every object client it or a reopen opens is closed."""
    directory.mkdir(mode=0o700)
    opened = []
    world = FilesJourney(store, directory, opened=opened, **options)
    try:
        yield world
    finally:
        for client in reversed(opened):
            client.close()


# Moved unchanged from test_local_filesystem_journey.py; a test module uses it by
# importing it together with ``store``.
@pytest.fixture
def files_journey(store, tmp_path):
    with files_journey_at(store, tmp_path.resolve() / "private-installation") as world:
        yield world


__all__ = [
    "ARTIFACT_LIMIT", "DUMMY_PRINCIPAL", "DUMMY_SUBMISSION", "FILES_BUCKET", "FilesJourney", "LEGACY_SUBMISSION",
    "OneTransactionInterruption", "ScriptedCalculator", "TYPE_MARKER", "TrackedCalculator", "accepted",
    "attempt_row", "calculation", "chart_link", "course_rows", "dummy_course", "files_journey", "files_journey_at",
    "item_view", "job_row", "jobs_over", "journey", "progress_facts", "recovery_facts", "seeded_legacy", "start",
    "start_reply", "store", "submit", "upload_event", "user_row",
]

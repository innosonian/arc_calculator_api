"""Authenticated /api/v2 calculation boundary harness shared by the two boundary test files (E-04).

The application is the real course_v2 composition (``build_course_application``
through tests/journey_support.V2Journey) on the in-memory DynamoDB client,
with the real AuthManager/state/CalculationService/JourneyStorage and the
DummyDevCourseProvider. Two Dummy sessions log in and the owner starts one
practice attempt on ``COURSE``. "Nothing accepted" is checked on the stored
rows and private objects (no JOB, no input_digest, no new input object) and
"never read" on the requests the table client received, instead of counters on
hand-written doubles.

Fixture scope (E-11): the logins, course read and start run once per module
(``prepared_world``). Each test gets its own fork: a fresh in-memory table
seeded with copies of the prepared rows, a MemoryS3 with copies of the
prepared objects, a copy of the operational records and the same clock value,
under a new application and Worker (the same public entry points the harness
always uses; the stored session rows keep the tokens valid, as they would
across a process restart). Nothing a test changes reaches another test.

    from tests.v2_boundary_support import prepared_world, world  # noqa: F401 (fixtures)

``world`` fields: ``h`` (V2Journey), ``events``, ``owner``/``other`` (login
results), ``started`` (start response), ``attempt_id``, ``condition``,
``measurement``, ``objects_before`` and ``requests`` (the table requests since
the last ``call``).
"""

from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from tests.journey_support import (
    MULTIPART_CONTENT_TYPE, EventLog, JourneyStore, V2Journey, dummy_course, measurement_for, multipart,
)


COURSE = dummy_course("mock-compression-only", "adult")
SECRET = "T1_PRIVATE_MARKER_DO_NOT_LOG"
WRITE_OPERATIONS = ("PutItem", "UpdateItem", "DeleteItem", "TransactWriteItems", "BatchWriteItem")
READ_OPERATIONS = ("GetItem", "Query", "Scan", "TransactGetItems")


def prepare_world():
    """Log two Dummy sessions in and start the owner's practice attempt on a fresh harness."""
    h = V2Journey(JourneyStore.memory(), events=EventLog())
    owner, other = h.login(), h.login()
    started = h.start(owner.token, COURSE, COURSE.practice_link_id)
    return SimpleNamespace(
        raw_items=h.store.raw_items(), objects=deepcopy(h.objects.objects), records=deepcopy(h.events.records),
        now=h.now[0], owner=owner, other=other, started=started,
    )


def fork_world(prepared):
    """A test's own copy of the prepared state under a new application and Worker."""
    from tests.mock_storage_support import MemoryS3

    store = JourneyStore.memory()
    for item in prepared.raw_items:
        store.put_raw(item)
    objects = MemoryS3()
    objects.objects = deepcopy(prepared.objects)
    events = EventLog()
    events.records = deepcopy(prepared.records)
    h = V2Journey(store, objects=objects, events=events, start=prepared.now)
    started = deepcopy(prepared.started)
    requests = []
    h.store.client.before_call = lambda operation, request: requests.append((operation, deepcopy(request)))
    return SimpleNamespace(
        h=h, events=events, owner=prepared.owner, other=prepared.other, started=started,
        attempt_id=started["attemptId"], condition=started["condition"],
        measurement=measurement_for(started["condition"]), objects_before=set(h.objects.objects),
        requests=requests,
    )


@pytest.fixture(scope="module")
def prepared_world():
    return prepare_world()


@pytest.fixture
def world(prepared_world):
    return fork_world(prepared_world)


# -- requests ------------------------------------------------------------------

def calculation_path(world, attempt_id=None):
    return f"/api/v2/attempts/{attempt_id or world.attempt_id}/calculation/"


def upload_event(world, *, token=None, attempt_id=None, condition=None, data=None, method="POST"):
    """Single-value REST proxy headers only; each test adds the representation it attacks.

    A GET carries no Content-Type or body (the read-only result request).
    """
    headers = {"Authorization": "Bearer " + (world.owner.token if token is None else token)}
    value = {"httpMethod": method, "path": calculation_path(world, attempt_id), "headers": headers}
    if method == "POST":
        headers["Content-Type"] = MULTIPART_CONTENT_TYPE
        value.update(isBase64Encoded=True, body=multipart(
            world.condition if condition is None else condition,
            data=world.measurement if data is None else data,
        ))
    return value


def call(world, value):
    """Send one event through the handler; ``world.requests`` holds only this call's table requests."""
    world.requests.clear()
    return world.h.call(value.get("httpMethod"), value.get("path"), event=value)


# -- assertions ------------------------------------------------------------------

def assert_error(reply, status, code, *, secret=SECRET):
    assert (reply.status, reply.error["code"]) == (status, code), reply.body
    assert secret not in json.dumps(reply.raw)


def assert_invalid_request(reply):
    assert (reply.status, reply.error["code"]) == (400, "INVALID_REQUEST"), reply.body
    assert reply.body["success"] is False and reply.error["details"] is None
    assert reply.headers["X-Request-Id"]


def attempt_reads(world, attempt_id=None):
    marker = f"ATTEMPT#{attempt_id or world.attempt_id}"
    return [operation for operation, request in world.requests if marker in repr(request)]


def writes(world):
    """Write requests the table client received (tests/memory_dynamodb records CamelCase names)."""
    return [operation for operation, _ in world.requests if operation in WRITE_OPERATIONS]


def attempt_row(world):
    return world.h.store.row(f"ATTEMPT#{world.attempt_id}", "META")


def job_rows(world):
    return [row for row in world.h.store.rows() if row["PK"].startswith("JOB#")]


def new_objects(world):
    """Private objects written since the fork: {(bucket, key): body}."""
    return {key: world.h.objects.objects[key]["Body"] for key in set(world.h.objects.objects) - world.objects_before}


def saved_measurement(world):
    """The one private CPR input (.bin) this request saved, checked against its request manifest."""
    saved = new_objects(world)
    inputs = [(key, body) for key, body in saved.items()
              if key[1].endswith(".bin") and not key[1].endswith(".aed.bin")]
    assert len(inputs) == 1, sorted(saved)
    (bucket, key), body = inputs[0]
    manifest = json.loads(next(body for (_, name), body in saved.items() if name.endswith(".request.json")))
    assert manifest["raw"]["cpr"]["key"] == key and manifest["raw"]["cpr"]["size"] == len(body)
    return body


def assert_nothing_accepted(world, *, input_saved=False):
    row = attempt_row(world)
    assert row["state"] == "created" and "input_digest" not in row and "job_id" not in row
    assert job_rows(world) == []
    if not input_saved:
        assert new_objects(world) == {}


def rejected_events(world):
    return [record for record in world.events.operations() if record["event"] == "request_rejected"]


# -- parser seams ----------------------------------------------------------------

def forbid_parser(monkeypatch, message="An ambiguous Content-Type reached the measurement parser."):
    import mock_journey.legacy_bridge as bridge

    def forbidden(*args, **kwargs):
        pytest.fail(message)

    monkeypatch.setattr(bridge, "parse_measurement", forbidden)


def parser_spy(monkeypatch):
    """Record the event each parser call receives; the real parser still runs."""
    import mock_journey.legacy_bridge as bridge

    seen = []
    real = bridge.parse_measurement

    def spy(event):
        seen.append((event, deepcopy(event)))
        return real(event)

    monkeypatch.setattr(bridge, "parse_measurement", spy)
    return seen

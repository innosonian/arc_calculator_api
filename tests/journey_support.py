"""Shared journey test helpers. Never imported by runtime code.

1. Neutral helpers: measurement bytes, the multipart body used by every
   calculation upload test, and the LocalDefinitions/LocalCalculator test
   adapter (values test state handling, not actual scoring).
2. ``compose``/``Composition``: the one place test code assembles the
   application and Worker roles (settings, keys, environment, storage limits,
   provider, bindings) from ``build_course_application``/``build_worker``.
   Every assembly root (V2Journey, tests/vcc_runtime_support.runtime,
   tests/vcc_application_support.application, http_pipeline_tests
   test_internal_http.world) calls it with its own ``HARNESS_*`` values, which
   are listed together below (E-01). The legacy row fixture was captured with
   the ``journey-harness`` values, so they must not change.
3. ``V2Journey``: an in-process ``/api/v2`` driver that uses only public entry
   points -- ``mock_journey.handler.handle`` with REST proxy events, and the
   composition's factories. It runs on ``JourneyStore.memory()``
   (tests/memory_dynamodb.py) or on a DynamoDB Local table
   (``JourneyStore(dynamodb_client, create_journey_table(...))``).
   Default course provider: DummyDevCourseProvider (15 Dummy Dev courses) with
   the real internal calculator, so accepted uploads are scored by real code.

The former legacy /mock/v1 helpers were removed with /mock/v1 (D103). Stored
legacy rows come from raw row seeds (tests/legacy_rows_support.py,
tests/legacy_attempt_seeds.py).

Harness quick reference (section 3)::

    h = V2Journey(JourneyStore.memory(), events=EventLog())
    session = h.login()                                   # POST /api/v2/sessions/
    course = dummy_course("mock-compression-only", "adult")
    detail = h.course(session.token, course)              # GET course progress (definitionHash)
    started = h.start(session.token, course, course.practice_link_id)
    h.upload(session.token, started["attemptId"], started["condition"])   # 202
    h.work(started["attemptId"])                          # Worker.process on the stored job
    result = h.result(session.token, started["attemptId"])                # 200 data
    h.chart_link(session.token, started["attemptId"])
    h.reauthorize(token, attempt_id, credential); h.cancel(token, attempt_id)
    h.logout(session.token)
    h.call(method, path, token=..., body=..., query=...)  # any route -> Reply
    h.relay(); h.deliver()                                # Outbox relay -> queue -> Worker.handle
    h.restart(); h.advance(seconds); h.store.rows(); h.store.row(pk, sk)
"""

import base64
from collections import namedtuple
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
from types import SimpleNamespace
import uuid

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

from tests._synth import BREATH_SHAPE, comp_session, packet
from tests.calculator_doubles import ScriptedCalculator


# ---------------------------------------------------------------------------
# 1. Neutral helpers
# ---------------------------------------------------------------------------

MEASUREMENT = (Path(__file__).parent / "dataset" / "cpr_1.bin").read_bytes()
MULTIPART_BOUNDARY = "arc-integration-boundary"
MULTIPART_CONTENT_TYPE = "multipart/form-data; boundary=" + MULTIPART_BOUNDARY


def with_registry_adapters(adapters, *, stage):
    """``adapters`` plus the bundled InternalCalculator of every registry version they do not cover.

    Registry = contracts.CURRENT_ADAPTER_VERSION then RETAINED_ADAPTER_VERSIONS
    under execution_definitions.PROJECTION_VERSION, the pairs the real Worker
    registers (D127). An adapter given by a test wins for its (version,
    projection); the rest are plain bundled calculators, so a harness Worker
    resolves the current catalog binding and every retained fixture binding.
    """
    from mock_journey.assembly import internal_calculator
    from mock_journey.contracts import CURRENT_ADAPTER_VERSION, RETAINED_ADAPTER_VERSIONS
    from mock_journey.execution_definitions import PROJECTION_VERSION

    result = list(adapters)
    present = {(adapter.version, adapter.projection_version) for adapter in result}
    for version in (CURRENT_ADAPTER_VERSION, *RETAINED_ADAPTER_VERSIONS):
        if (version, PROJECTION_VERSION) not in present:
            result.append(internal_calculator(version, projection=PROJECTION_VERSION, stage=stage))
    return result


class LocalDefinitions:
    def get_definition(self, program_id, target):
        training = {"mock-compression-only": "compression_only", "mock-ventilation-only": "ventilation_only"}.get(program_id, "cpr")
        return {"condition": {
            "mode": "training", "target": target, "training_type": training,
            "guideline": "ARC2025", "cpr_cycle_type": "152" if target == "infant" else "302",
            "is_2rescuers": program_id in ("mock-two-rescuer-cpr", "mock-two-rescuer-aed"),
        }, "calculation_profile": {}, "profile_version": "test-profile", "adapter_version": "test-adapter",
            "projection_version": "test-projection"}


class LocalCalculator(ScriptedCalculator):
    """Only tests inject this adapter; values test state, not actual scoring.

    The ScriptedCalculator variant registered under the LocalDefinitions test
    versions: every call must carry ``MEASUREMENT`` and a cycles goal is
    reported as observed (no pending goal policy).
    """
    version = "test-adapter"
    projection_version = "test-projection"
    expected_measurement = MEASUREMENT


def multipart(condition, *, extra=None, data=MEASUREMENT):
    parts = [("rawHexBPfile", data), ("condition", json.dumps(condition).encode())]
    parts += [(name, json.dumps(value).encode()) for name, value in (extra or {}).items()]
    body = b"".join(b'--arc-integration-boundary\r\nContent-Disposition: form-data; name="' + name.encode() +
                    b'"\r\n\r\n' + value + b"\r\n" for name, value in parts) + b"--arc-integration-boundary--\r\n"
    return base64.b64encode(body).decode()


def synthetic_measurement(*, ventilation, count=None):
    """Synthetic packets the real calculator scores as a pass for the Dummy definitions.

    Compression: ``comp_session(60)``. Ventilation: adult ~520 mL breaths, one
    per 6 seconds (the vo_session helper packs breaths too fast for a passing
    score; calculator thresholds stay untouched).
    """
    if not ventilation:
        return comp_session(60 if count is None else count)
    samples = [packet(timestamp=0)]
    for number in range(8 if count is None else count):
        base = number * 6000 + 100
        samples.extend(packet(vent_raw=value, timestamp=base + index * 250)
                       for index, value in enumerate(BREATH_SHAPE))
        samples.append(packet(timestamp=base + 5900))
    return b"".join(samples)


def measurement_for(condition, *, count=None):
    return synthetic_measurement(ventilation=condition["training_type"] == "ventilation_only", count=count)


# ---------------------------------------------------------------------------
# 2. Assembly: harness values and the one composition root
# ---------------------------------------------------------------------------

# Every value below is an explicit test setting, not a deployment default.
HARNESS_START = 1_800_000_000
HARNESS_ENVIRONMENT = "journey-harness"
# Storage identifier "v1" is a key *version*, not the /mock/v1 API. Synthetic,
# test-only key material; the legacy row fixture was captured with it
# (tests/fixtures/legacy_mock_v1_rows/meta.json checks its fingerprint).
HARNESS_KEY_VERSION = "v1"
HARNESS_RESUME_KEYS = {"v1": b"journey-harness-synthetic-test-key-v1"}
HARNESS_STAGE = "development"
HARNESS_QUEUE_URL = "https://queue.example.invalid/journey-harness"
HARNESS_CONFLICT_RETRIES = 4
HARNESS_LEASE_SECONDS = 60
HARNESS_RETRY_SECONDS = 5
StorageLimits = namedtuple("StorageLimits", "input_bytes artifact_bytes payload_bytes")
HARNESS_LIMITS = StorageLimits(input_bytes=1_000_000, artifact_bytes=8_000_000, payload_bytes=2_000_000)
# tests/vcc_runtime_support.runtime: the assigned fixture learner over the real control DB.
HARNESS_VCC_ENVIRONMENT = "vcc-hardening-test"
HARNESS_VCC_KEYS = {"v1": b"synthetic-test-only-key-material!!"}
# tests/vcc_application_support.application: course application on a DynamoDB Local table.
HARNESS_DDB_ENVIRONMENT = "vcc-ddb"
HARNESS_DDB_KEYS = {"v1": b"K" * 32}
HARNESS_DDB_LIMITS = StorageLimits(input_bytes=1_000_000, artifact_bytes=2_000_000, payload_bytes=2_000_000)
# http_pipeline_tests/test_internal_http.world: real TCP over a test file store.
HARNESS_LOCAL_ENVIRONMENT = "local-validation"
HARNESS_LOCAL_STAGE = "local-validation"
HARNESS_LOCAL_KEY_VERSION = "test-v1"
HARNESS_LOCAL_KEYS = {"test-v1": b"T" * 32}
DUMMY_LOGIN = {"loginId": "test@test.com", "password": "2222"}
_SERIALIZER, _DESERIALIZER = TypeSerializer(), TypeDeserializer()


def create_journey_table(client, name=None):
    """PK/SK table with the GSI1 due index, as the runtime writes it."""
    name = name or "arc_journey_" + uuid.uuid4().hex
    client.create_table(
        TableName=name,
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": key, "AttributeType": kind} for key, kind in (
            ("PK", "S"), ("SK", "S"), ("GSI1PK", "S"), ("GSI1SK", "N"),
        )],
        GlobalSecondaryIndexes=[{
            "IndexName": "GSI1", "KeySchema": [
                {"AttributeName": "GSI1PK", "KeyType": "HASH"}, {"AttributeName": "GSI1SK", "KeyType": "RANGE"},
            ], "Projection": {"ProjectionType": "ALL"},
        }], BillingMode="PAY_PER_REQUEST",
    )
    return name


def decode_item(item):
    """DynamoDB JSON -> Python; integral numbers become int (the repository stores no floats)."""
    def plain(value):
        if isinstance(value, Decimal):
            if value != value.to_integral_value():
                raise AssertionError("Unexpected non-integral stored number")
            return int(value)
        if isinstance(value, dict):
            return {key: plain(nested) for key, nested in value.items()}
        if isinstance(value, list):
            return [plain(nested) for nested in value]
        return value
    return {key: plain(_DESERIALIZER.deserialize(value)) for key, value in item.items()}


def encode_item(row):
    return {key: _SERIALIZER.serialize(value) for key, value in row.items()}


class JourneyStore:
    """One journey table behind a DynamoDB-shaped client (memory or DynamoDB Local)."""

    def __init__(self, client, table):
        self.client, self.table = client, table

    @classmethod
    def memory(cls):
        from tests.memory_dynamodb import MemoryDynamoDB
        client = MemoryDynamoDB()
        return cls(client, create_journey_table(client))

    def raw_items(self):
        items, start = [], None
        while True:
            request = {"TableName": self.table, "ConsistentRead": True}
            if start is not None:
                request["ExclusiveStartKey"] = start
            page = self.client.scan(**request)
            items.extend(page.get("Items", []))
            start = page.get("LastEvaluatedKey")
            if not start:
                break
        return sorted(items, key=lambda item: (item["PK"]["S"], item["SK"]["S"]))

    def rows(self):
        return [decode_item(item) for item in self.raw_items()]

    def row(self, pk, sk):
        found = self.client.get_item(TableName=self.table, Key={"PK": {"S": pk}, "SK": {"S": sk}},
                                     ConsistentRead=True).get("Item")
        return decode_item(found) if found is not None else None

    def put_raw(self, item):
        """Unconditional raw write for seeding a fixture into an empty table."""
        self.client.put_item(TableName=self.table, Item=deepcopy(item))


@contextmanager
def dynamodb_local_store(client):
    """A fresh GSI table on the explicit DynamoDB Local client; only this table is deleted."""
    table = create_journey_table(client)
    try:
        yield JourneyStore(client, table)
    finally:
        client.delete_table(TableName=table)


class Composition:
    """Settings, bindings and role factories of one test application on one store and clock.

    ``build_api``/``build_worker`` are the only calls test code makes into
    ``mock_journey.assembly``; extra keyword options go to the factory
    unchanged (``uuid_factory``, ``dummy_learner``, ``lease_guard_factory``).
    """

    def __init__(self, store, *, objects, bindings, provider, execution, course_settings, mapping_document,
                 keys, key_version, environment, stage, limits, clock, lease_seconds, retry_seconds):
        from mock_journey.settings import ApiSettings, StateSettings, StorageSettings, WorkerSettings

        self.store = store
        self.objects = objects
        self.bindings = bindings
        self.provider = provider
        self.execution = execution
        self.course_settings = course_settings
        self.mapping_document = mapping_document
        self.keys = dict(keys)
        self.key_version = key_version
        self.environment = environment
        self.clock = clock
        state = StateSettings(store.table, HARNESS_CONFLICT_RETRIES)
        storage = StorageSettings(stage, bindings.bucket, bindings.directory, limits.input_bytes, limits.artifact_bytes)
        self.api_settings = ApiSettings(state, storage, environment, limits.payload_bytes)
        self.worker_settings = WorkerSettings(state, storage, lease_seconds, retry_seconds)

    @property
    def stage(self):
        return self.api_settings.storage.stage

    def build_api(self, *, operations=None, **options):
        from mock_journey.assembly import build_course_application
        return build_course_application(
            self.api_settings, dynamodb_client=self.store.client, s3_client=self.objects,
            legacy_bindings=self.bindings, resume_keys=self.keys, current_key_version=self.key_version,
            execution=self.execution, provider=self.provider, course_settings=self.course_settings,
            mapping_document=self.mapping_document, clock=self.clock, operations=operations, **options,
        )

    def default_adapters(self):
        """The bundled internal calculators of the whole code registry on this composition's stage (real scoring).

        Current first, then the retained versions, as the real local/AWS Worker
        registers them (D127): a legacy fixture job bound to the retained
        pending-v3 adapter is calculated by that adapter (D136, 3A).
        """
        return with_registry_adapters([], stage=self.stage)

    def build_worker(self, adapters=None, *, required_bindings=None, operations=None, lease_seconds=None,
                     retry_seconds=None, **options):
        from mock_journey.assembly import build_worker
        from mock_journey.settings import WorkerSettings
        settings = self.worker_settings
        if lease_seconds is not None or retry_seconds is not None:
            settings = WorkerSettings(settings.state, settings.storage,
                                      settings.lease_seconds if lease_seconds is None else lease_seconds,
                                      settings.retry_seconds if retry_seconds is None else retry_seconds)
        return build_worker(
            settings, dynamodb_client=self.store.client, s3_client=self.objects, legacy_bindings=self.bindings,
            # The given adapters keep their versions; every other registry version is
            # registered as the bundled calculator so retained-version jobs resolve.
            adapters=with_registry_adapters(list(adapters or ()), stage=self.stage),
            required_bindings=required_bindings or self.execution.required_bindings, clock=self.clock,
            operations=operations, **options,
        )


def compose(store, *, provider=None, objects=None, bindings=None, keys=None, key_version=None,
            environment=HARNESS_ENVIRONMENT, storage_limits=HARNESS_LIMITS, stage=HARNESS_STAGE, execution=None,
            course_settings=None, mapping_document=None, clock=None, lease_seconds=HARNESS_LEASE_SECONDS,
            retry_seconds=HARNESS_RETRY_SECONDS):
    """Assemble the test application/Worker composition on ``store`` (E-01).

    Defaults are the journey harness values: MemoryS3 objects with memory
    legacy bindings, DummyDevCourseProvider over ``execution_catalog()`` and
    ``fixture_course_settings()``, ``HARNESS_RESUME_KEYS``, a clock fixed at
    ``HARNESS_START``. ``key_version`` defaults to the only key in ``keys``.
    ``mapping_document`` defaults to the provider's own document.
    """
    from mock_journey.course_settings import fixture_course_settings
    from mock_journey.dev_course import DummyDevCourseProvider
    from mock_journey.execution_definitions import execution_catalog
    from tests.mock_storage_support import MemoryLegacyBindings, MemoryS3

    objects = objects if objects is not None else MemoryS3()
    bindings = bindings if bindings is not None else MemoryLegacyBindings(objects)
    execution = execution if execution is not None else execution_catalog()
    course_settings = course_settings or fixture_course_settings()
    provider = provider or DummyDevCourseProvider(settings=course_settings, execution=execution)
    if mapping_document is None:
        mapping_document = getattr(provider, "mapping_document", None)
    keys = dict(HARNESS_RESUME_KEYS if keys is None else keys)
    if key_version is None:
        (key_version,) = keys
    return Composition(
        store, objects=objects, bindings=bindings, provider=provider, execution=execution,
        course_settings=course_settings, mapping_document=mapping_document, keys=keys, key_version=key_version,
        environment=environment, stage=stage, limits=storage_limits,
        clock=clock if clock is not None else (lambda: HARNESS_START), lease_seconds=lease_seconds,
        retry_seconds=retry_seconds,
    )


# ---------------------------------------------------------------------------
# 3. /api/v2 in-process harness
# ---------------------------------------------------------------------------

class EventLog:
    """Synchronous operational recorder that accepts only what the real validator accepts."""

    def __init__(self, clock=None):
        self.records = []
        self.clock = clock  # V2Journey binds its own clock when this is None.

    def recorder(self, role):
        log = self

        class _Recorder:
            def record(self, category, event, fields):
                from services.operational_logs import validate_record
                now = log.clock() if log.clock is not None else HARNESS_START
                stamp = datetime.fromtimestamp(int(now), timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
                raw = validate_record({"schema": 1, "log_id": str(uuid.uuid4()), "occurred_at": stamp,
                                       "role": role, "category": category, "event": event, "fields": fields})
                value = json.loads(raw)
                log.records.append({key: value[key] for key in ("role", "category", "event", "fields")})
                return True
        return _Recorder()

    def operations(self):
        return [record for record in self.records if record["category"] == "operation"]


class Reply(SimpleNamespace):
    """status, headers, body (parsed JSON or None for 204) and the raw proxy response."""

    @property
    def data(self):
        return self.body["data"]

    @property
    def error(self):
        return self.body["error"]


class QueueCapture:
    """SQS double for build_relay: records job wake bodies, never sends anywhere."""

    def __init__(self):
        self.messages = []

    def send_message(self, *, QueueUrl, MessageBody):
        assert QueueUrl == HARNESS_QUEUE_URL
        self.messages.append(MessageBody)
        return {"MessageId": str(uuid.uuid4())}


def dummy_course(program_id, target):
    """Public IDs of a DummyDevCourseProvider course (mock_journey/dev_course.py numbering)."""
    from mock_journey.catalog import PROGRAMS, TARGETS
    programs = [program[0] for program in PROGRAMS]
    number = programs.index(program_id) * len(TARGETS) + TARGETS.index(target) + 1
    return SimpleNamespace(program_id=program_id, target=target, course_id=910000 + number,
                           enrollment_id=920000 + number, practice_link_id=940000 + number * 10 + 1,
                           final_link_id=940000 + number * 10 + 2)


class V2Journey:
    """In-process /api/v2 application + Worker (+ optional Relay) on one store and clock."""

    def __init__(self, store, *, objects=None, bindings=None, start=HARNESS_START, provider=None,
                 course_settings=None, mapping_document=None, events=None, adapters=None,
                 environment=HARNESS_ENVIRONMENT, resume_keys=None, key_version=HARNESS_KEY_VERSION,
                 uuid_factory=None, lease_seconds=HARNESS_LEASE_SECONDS, retry_seconds=HARNESS_RETRY_SECONDS,
                 stage=HARNESS_STAGE, storage_limits=HARNESS_LIMITS, execution=None):
        self.store = store
        self.now = [start]
        self.events = events
        if events is not None and events.clock is None:
            events.clock = self.clock
        self.uuid_factory = uuid_factory
        self.adapters = adapters
        self.composition = compose(
            store, provider=provider, objects=objects, bindings=bindings,
            keys=HARNESS_RESUME_KEYS if resume_keys is None else resume_keys, key_version=key_version,
            environment=environment, storage_limits=storage_limits, stage=stage, execution=execution,
            course_settings=course_settings, mapping_document=mapping_document, clock=self.clock,
            lease_seconds=lease_seconds, retry_seconds=retry_seconds,
        )
        self.queue = QueueCapture()
        self.api = self.build_api()
        self.worker = self.build_worker()

    # -- composition -------------------------------------------------------
    # The composition's parts are attributes of the harness (tests replace h.execution or
    # h.provider and rebuild a role); reading and assigning them goes to the composition.
    def _part(name):  # noqa: N805 (descriptor factory)
        return property(lambda self: getattr(self.composition, name),
                        lambda self, value: setattr(self.composition, name, value))

    objects, bindings, execution = _part("objects"), _part("bindings"), _part("execution")
    course_settings, provider, mapping_document = _part("course_settings"), _part("provider"), _part("mapping_document")
    environment, resume_keys, key_version = _part("environment"), _part("keys"), _part("key_version")
    api_settings, worker_settings = _part("api_settings"), _part("worker_settings")
    del _part

    def clock(self):
        return self.now[0]

    def advance(self, seconds):
        self.now[0] += seconds
        return self.now[0]

    def _operations(self, role):
        return self.events.recorder(role) if self.events is not None else None

    def build_api(self):
        options = {"uuid_factory": self.uuid_factory} if self.uuid_factory is not None else {}
        return self.composition.build_api(operations=self._operations("api"), **options)

    def default_adapters(self):
        return self.composition.default_adapters()

    def build_worker(self, adapters=None, *, required_bindings=None, **options):
        return self.composition.build_worker(adapters or self.adapters or self.default_adapters(),
                                             required_bindings=required_bindings,
                                             operations=self._operations("worker"), **options)

    def build_relay(self, *, page_size=25, max_pages=4):
        from mock_journey.assembly import build_relay
        from mock_journey.settings import RelaySettings
        settings = RelaySettings(self.api_settings.state, HARNESS_QUEUE_URL, self.worker_settings.lease_seconds,
                                 self.worker_settings.retry_seconds, page_size, max_pages)
        return build_relay(settings, dynamodb_client=self.store.client, sqs_client=self.queue, clock=self.clock)

    def restart(self):
        """New API and Worker instances on the same table, objects and clock (process restart)."""
        self.api = self.build_api()
        self.worker = self.build_worker()
        return self

    # -- HTTP ----------------------------------------------------------------
    def call(self, method, path, *, token=None, body=None, query=None, content_type="application/json",
             event=None, request_id=None):
        """Send one REST proxy event through mock_journey.handler.handle."""
        from mock_journey.handler import handle
        from tests.vcc_support import event as proxy_event
        if event is None:
            event = proxy_event(method, path, body=body, token=token, query=query, content_type=content_type)
        context = SimpleNamespace(aws_request_id=request_id or str(uuid.uuid4()))
        response = handle(event, context, self.api)
        parsed = json.loads(response["body"]) if response["body"] else None
        return Reply(status=response["statusCode"], headers=response["headers"], body=parsed, raw=response)

    def expect(self, status, method, path, **kwargs):
        reply = self.call(method, path, **kwargs)
        assert reply.status == status, (method, path, reply.status, reply.body)
        if status == 204:
            return None
        return reply.data if status < 400 else reply.error

    def login(self, expected=201):
        data = self.expect(expected, "POST", "/api/v2/sessions/", body=DUMMY_LOGIN)
        return SimpleNamespace(token=data["accessToken"], session_id=data["sessionId"], data=data)

    def session(self, token, expected=200):
        return self.expect(expected, "GET", "/api/v2/session/", token=token)

    def refresh(self, token, expected=200):
        return self.expect(expected, "POST", "/api/v2/session/refresh/", token=token, body={})

    def logout(self, token, expected=204):
        return self.expect(expected, "DELETE", "/api/v2/session/", token=token)

    def courses(self, token, expected=200, **query):
        return self.expect(expected, "GET", "/api/v2/courses/progress/", token=token,
                           query={key: str(value) for key, value in query.items()} or None)

    def course(self, token, course, expected=200):
        return self.expect(expected, "GET", f"/api/v2/courses/{course.course_id}/progress/", token=token,
                           query={"enrollmentId": str(course.enrollment_id)})

    def start(self, token, course, link_id, *, client_request_id=None, definition_hash=None, expected=201):
        if definition_hash is None:
            definition_hash = self.course(token, course)["definitionHash"]
        return self.expect(expected, "POST", "/api/v2/attempts/", token=token, body={
            "clientRequestId": client_request_id or str(uuid.uuid4()), "courseId": course.course_id,
            "enrollmentId": course.enrollment_id, "courseItemLinkId": link_id, "definitionHash": definition_hash,
        })

    def attempt(self, token, attempt_id, expected=200):
        return self.expect(expected, "GET", f"/api/v2/attempts/{attempt_id}/", token=token)

    def reauthorize(self, token, attempt_id, credential, expected=200):
        return self.expect(expected, "POST", f"/api/v2/attempts/{attempt_id}/reauthorize/", token=token,
                           body={"resumeCredential": credential})

    def cancel(self, token, attempt_id, reason="user_cancelled", expected=204):
        return self.expect(expected, "POST", f"/api/v2/attempts/{attempt_id}/cancel/", token=token,
                           body={"reason": reason})

    def upload_event(self, token, attempt_id, condition, *, data=None, count=None):
        from tests.vcc_support import event as proxy_event
        upload = proxy_event("POST", f"/api/v2/attempts/{attempt_id}/calculation/", token=token,
                             content_type=MULTIPART_CONTENT_TYPE)
        payload = data if data is not None else measurement_for(condition, count=count)
        upload.update(body=multipart(condition, data=payload), isBase64Encoded=True)
        return upload

    def upload(self, token, attempt_id, condition, *, data=None, count=None, expected=202):
        event = self.upload_event(token, attempt_id, condition, data=data, count=count)
        return self.expect(expected, "POST", event["path"], event=event)

    def result(self, token, attempt_id, expected=200):
        return self.expect(expected, "GET", f"/api/v2/attempts/{attempt_id}/calculation/", token=token)

    def chart_link(self, token, attempt_id, expected=200):
        return self.expect(expected, "GET", f"/api/v2/attempts/{attempt_id}/chart-link/", token=token)

    # -- Worker / Relay ----------------------------------------------------
    def job_id(self, attempt_id):
        row = self.store.row(f"ATTEMPT#{attempt_id}", "META")
        assert row is not None and type(row.get("job_id")) is str, "attempt has no accepted job"
        return row["job_id"]

    def work(self, attempt_id=None, *, job_id=None):
        """Worker.process on the attempt's stored job (the queue-message body)."""
        return self.worker.process(job_id or self.job_id(attempt_id))

    def relay(self, **options):
        """One Relay reconcile pass into self.queue; returns the woken job ids in send order.

        An Outbox wake and a due-Job wake can name the same job in one pass.
        """
        before = len(self.queue.messages)
        self.build_relay(**options).reconcile()
        return [json.loads(body)["job_id"] for body in self.queue.messages[before:]]

    def deliver(self):
        """Hand every captured wake to worker.handle as one SQS batch, then clear the queue."""
        from mock_journey.worker import handle
        records = [{"messageId": str(uuid.uuid4()), "body": body} for body in self.queue.messages]
        self.queue.messages = []
        if not records:
            return {"batchItemFailures": []}
        return handle({"Records": records}, SimpleNamespace(aws_request_id=str(uuid.uuid4())), self.worker)

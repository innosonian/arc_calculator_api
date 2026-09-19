"""Assigned synthetic learner with real control DB and original binary calculator.

No external authentication, ARC request, or canned calculation result is used.
"""

import hashlib
import json
import secrets
from types import SimpleNamespace
import uuid

from mock_journey.assembly import build_course_application, build_worker
from mock_journey.catalog import Catalog
from mock_journey.contracts import PENDING_GOAL_ADAPTER_VERSION
from mock_journey.course_contracts import StartCommand
from mock_journey.course_fixture import FixtureCourseProvider
from mock_journey.course_settings import fixture_course_settings
from mock_journey.execution_definitions import execution_catalog
from mock_journey.internal_calculator import InternalCalculator
from mock_journey.settings import ApiSettings, StateSettings, StorageSettings, WorkerSettings
from tests.mock_storage_support import MemoryS3, MemoryLegacyBindings
from tests.vcc_support import load_fixture, mapping_document
from tests._synth import comp_session, packet, BREATH_SHAPE
from integration_tests.test_mock_journey import multipart


def runtime(client, table, *, objects=None, legacy_bindings=None, existing_token=None):
    now = [1_800_000_000]
    clock = lambda: now[0]
    objects = objects if objects is not None else MemoryS3()
    legacy = legacy_bindings if legacy_bindings is not None else MemoryLegacyBindings(objects)
    settings = ApiSettings(StateSettings(table, 4),
        StorageSettings("development", legacy.bucket, legacy.directory, 1_000_000, 8_000_000),
        "vcc-hardening-test", 2_000_000)
    execution = execution_catalog()
    provider = FixtureCourseProvider(document=load_fixture("course_bundle.json"),
        settings=fixture_course_settings(), mapping_document=mapping_document())
    def rebuild():
        return build_course_application(settings, dynamodb_client=client, s3_client=objects,
            legacy_bindings=legacy, resume_keys={"v1": b"synthetic-test-only-key-material!!"},
            current_key_version="v1", execution=execution, provider=provider,
            course_settings=fixture_course_settings(), mapping_document=mapping_document(), clock=clock)
    app = rebuild()
    token = existing_token
    if token is None:
        session_id = str(uuid.uuid4())
        token = f"s1.{session_id}.{secrets.token_urlsafe(32)}"
        principal = load_fixture("course_bundle.json")["learners"]["real"]["principal"]
        app.state.create_session({"session_id": session_id, "principal": principal,
            "token_hash": hashlib.sha256(token.encode()).hexdigest(), "issued_at": now[0],
            "expires_at": now[0]+86400, "status": "active", "revision": 0}, Catalog().slot_keys)
    auth = app.auth.authenticate(token)
    if existing_token is None:
        app.course_service.refresh_for_session(auth)
    adapter = InternalCalculator(version=PENDING_GOAL_ADAPTER_VERSION,
        projection_version="arc-local-projection-v1", stage="development", allow_pending_cycle_goal=True)
    def make_worker(adapters=None):
        return build_worker(WorkerSettings(settings.state, settings.storage, 60, 5),
            dynamodb_client=client, s3_client=objects, legacy_bindings=legacy,
            adapters=adapters or [adapter], required_bindings=execution.required_bindings, clock=clock)
    return SimpleNamespace(app=app, auth=auth, token=token, adapter=adapter, now=now,
        clock=clock, objects=objects, rebuild=rebuild, make_worker=make_worker,
        worker=make_worker(), table=table, client=client)


def start_attempt(env, link_id):
    view = env.app.course_service.get_course(env.auth, course_id=101, enrollment_id=501)
    receipt = env.app.course_service.start_attempt(env.auth,
        StartCommand(str(uuid.uuid4()), 501, 101, link_id, view.bundle.definition_hash))
    return env.app.state.get_attempt(env.auth, receipt.attempt_id)


def measurement_event(attempt, *, count=None):
    definition = json.loads(attempt["definition_json"])
    ventilation = definition["goal"]["kind"] == "ventilations"
    if ventilation:
        # Adult volume 520mL; ~1 second inflation, one breath per 6 seconds.
        # The existing vo_session helper intentionally packs breaths too fast
        # for this passing-score scenario; calculator thresholds stay untouched.
        samples = [packet(timestamp=0)]
        for number in range(8 if count is None else count):
            base = number * 6000 + 100
            samples.extend(packet(vent_raw=value, timestamp=base + index*250)
                           for index, value in enumerate(BREATH_SHAPE))
            samples.append(packet(timestamp=base+5900))
        data = b"".join(samples)
    else:
        data = comp_session(60 if count is None else count)
    return {"httpMethod": "POST", "headers": {"Content-Type": "multipart/form-data; boundary=arc-integration-boundary"},
            "body": multipart(definition["condition"], data=data), "isBase64Encoded": True}


def submit(env, attempt, *, count=None):
    env.app.calculation.submit(env.auth, attempt["attempt_id"], measurement_event(attempt, count=count))
    return env.app.state.get_attempt(env.auth, attempt["attempt_id"])["job_id"]

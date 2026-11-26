"""Assigned synthetic learner with real control DB and original binary calculator.

No external authentication, ARC request, or canned calculation result is used.
The fixture learner cannot use the Dummy login, so its SESSION and USER rows
are seeded directly (``seed_session``) instead of through a login API.
"""

import hashlib
import json
import secrets
from types import SimpleNamespace
import uuid

from mock_journey.course_contracts import StartCommand
from mock_journey.projection import DEFINITION_SETTING_KEYS
from mock_journey.course_fixture import FixtureCourseProvider
from mock_journey.course_settings import fixture_course_settings
from tests.vcc_support import load_fixture, mapping_document
from tests.journey_support import (
    HARNESS_START, HARNESS_VCC_ENVIRONMENT, HARNESS_VCC_KEYS, JourneyStore, compose, encode_item, multipart,
    synthetic_measurement,
)


def seed_session(client, table, *, session_id, principal, token, now, expires_at, legacy_slots=None):
    """Create-only USER (kept when it exists) and SESSION rows for a bearer token.

    The USER row has the shape DynamoStateRepository.create_session writes now:
    no slots (design Q10). ``legacy_slots`` instead seeds the USER row of a
    stored legacy (pre-D103 v1) learner with that slots map (R1). A USER row
    seeded earlier, such as a legacy row with slots, is kept unchanged by the
    conditional put.
    """
    from botocore.exceptions import ClientError
    user = {"PK": f"USER#{principal}", "SK": "STATE", "principal": principal, "epoch": str(uuid.uuid4()),
            "revision": 0, "updated_at": now}
    if legacy_slots is not None:
        user["slots"] = legacy_slots
    try:
        client.put_item(TableName=table, Item=encode_item(user), ConditionExpression="attribute_not_exists(PK)")
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
    session = {"PK": f"SESSION#{session_id}", "SK": "AUTH", "session_id": session_id, "principal": principal,
               "token_hash": hashlib.sha256(token.encode()).hexdigest(), "issued_at": now,
               "expires_at": expires_at, "status": "active", "revision": 0,
               "organizations": [{"id": "fixture-org", "name": "Fixture organization"}]}
    client.put_item(TableName=table, Item=encode_item(session), ConditionExpression="attribute_not_exists(PK)")


def current_mapping_document():
    """Keep frozen VCC IDs/DTOs while new runtime calculations use the full current definition."""
    from tests.legacy_dummy_catalog import LegacyCatalog, legacy_execution_catalog

    document = mapping_document()
    catalog = LegacyCatalog(legacy_execution_catalog())
    for mapping in document["mappings"].values():
        previous = mapping["execution"]
        definition = json.loads(catalog.definition(mapping["program_id"], mapping["target"]))
        definition["catalog_version"] = previous["catalog_version"]
        assert all(key in definition for key in DEFINITION_SETTING_KEYS)
        mapping["execution"] = definition
    return document


def runtime(client, table, *, objects=None, legacy_bindings=None, existing_token=None, legacy_slots=None):
    """The assigned fixture learner's application on ``HARNESS_VCC_*`` values (tests/journey_support.compose)."""
    now = [HARNESS_START]
    clock = lambda: now[0]  # noqa: E731
    mapping = current_mapping_document()
    document = load_fixture("course_bundle.json")
    for placement in document["placements"]:
        if placement["source_id"] in mapping["mappings"]:
            placement["execution"] = mapping["mappings"][placement["source_id"]]["execution"]
    provider = FixtureCourseProvider(document=document,
        settings=fixture_course_settings(), mapping_document=mapping)
    composition = compose(
        JourneyStore(client, table), provider=provider, objects=objects, bindings=legacy_bindings,
        keys=HARNESS_VCC_KEYS, environment=HARNESS_VCC_ENVIRONMENT, mapping_document=mapping,
        clock=clock,
    )
    rebuild = composition.build_api
    app = rebuild()
    token = existing_token
    if token is None:
        session_id = str(uuid.uuid4())
        token = f"s1.{session_id}.{secrets.token_urlsafe(32)}"
        principal = load_fixture("course_bundle.json")["learners"]["real"]["principal"]
        seed_session(client, table, session_id=session_id, principal=principal, token=token,
                     now=now[0], expires_at=now[0] + 86400, legacy_slots=legacy_slots)
    auth = app.auth.authenticate(token)
    if existing_token is None:
        app.course_service.refresh_for_session(auth)
    # The frozen mapping stays a compatibility fixture; this runtime explicitly selects current definitions.
    fixture_version = next(iter(mapping["mappings"].values()))["execution"]["adapter_version"]
    adapter = next(a for a in composition.default_adapters() if a.version == fixture_version)

    def make_worker(adapters=None):
        return composition.build_worker(adapters or [adapter])
    return SimpleNamespace(app=app, auth=auth, token=token, adapter=adapter, now=now,
        clock=clock, objects=composition.objects, rebuild=rebuild, make_worker=make_worker,
        worker=make_worker(), table=table, client=client, composition=composition)


def start_attempt(env, link_id):
    view = env.app.course_service.get_course(env.auth, course_id=101, enrollment_id=501)
    receipt = env.app.course_service.start_attempt(env.auth,
        StartCommand(str(uuid.uuid4()), 501, 101, link_id, view.bundle.definition_hash))
    return env.app.state.get_attempt(env.auth, receipt.attempt_id)


def measurement_event(attempt, *, count=None):
    definition = json.loads(attempt["definition_json"])
    # Use the current definition minimum; explicit counts still exercise undersized attempts.
    ventilation = definition["goal"]["kind"] == "ventilations"
    if count is None and "minimumQuantity" in definition:
        field = "ventilations" if ventilation else "compressions"
        count = max(definition["goal"]["required"], definition["minimumQuantity"][field])
    data = synthetic_measurement(ventilation=ventilation, count=count)
    return {"httpMethod": "POST", "headers": {"Content-Type": "multipart/form-data; boundary=arc-integration-boundary"},
            "body": multipart(definition["condition"], data=data), "isBase64Encoded": True}


def submit(env, attempt, *, count=None):
    env.app.calculation.submit(env.auth, attempt["attempt_id"], measurement_event(attempt, count=count))
    return env.app.state.get_attempt(env.auth, attempt["attempt_id"])["job_id"]

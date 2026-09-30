"""Legacy-seeded job repository world on a fresh GSI table in DynamoDB Local.

Moved unchanged from integration_tests/test_mock_jobs_dynamodb.py so other test
modules no longer import a test module. A test module uses the ``jobs_world``
fixture by importing it.
"""

import json
import uuid

import pytest

from mock_journey.jobs import DynamoJobRepository
from mock_journey.state import DynamoStateRepository
from tests.journey_support import create_journey_table, decode_item, encode_item
from tests.legacy_attempt_seeds import Clock, seed_legacy_attempt, seed_legacy_session


PRINCIPAL = "local-dummy-principal"
SLOT = "mock-cpr:adult"
# Synthetic interface fixture, not a configured runtime definition.
DEFINITION_JSON = json.dumps({
    "condition": {"target": "adult"}, "goal": {"kind": "cycles", "required": 3},
    "calculation_profile": {"unchanged_types": [None, False, 80, 80.0, "80"]},
    "catalog_version": "local-test", "profile_version": "local-test",
    "adapter_version": "local-test", "projection_version": "local-test",
})


def blob(key, letter="a"):
    return {"bucket": "private-test-bucket", "key": key, "sha256": letter * 64, "size": 10}


def binding(job):
    return {key: job[key] for key in (
        "attempt_id", "epoch", "input_digest", "adapter_version", "projection_version", "job_id", "call_id",
    )}


def evaluation(*, observed=3, passed=True):
    met = observed >= 3
    return {
        "goal": {"kind": "cycles", "required": 3, "observed": observed, "met": met},
        "score": {"decision": "pass" if passed else "fail"}, "program_completed": met and passed,
        "reason_codes": ([] if met else ["GOAL_NOT_MET"]) + ([] if passed else ["SCORE_NOT_PASS"]),
    }


class JobWorld:
    def __init__(self, client, table):
        self.client = client
        self.table = table
        self.clock = Clock()
        self.repository = DynamoStateRepository(client, table, clock=self.clock)
        self.jobs = DynamoJobRepository(self.repository)

    def repo(self, client):
        return DynamoStateRepository(client, self.table, clock=self.clock)

    def job_repo(self, client):
        return DynamoJobRepository(self.repo(client))

    def session(self, *, principal=PRINCIPAL, expires_at=None):
        return seed_legacy_session(self.client, self.table, clock=self.clock, principal=principal,
                                   expires_at=expires_at)

    def create(self, auth, *, program_id="mock-cpr", target="adult"):
        return seed_legacy_attempt(self.client, self.table, auth, clock=self.clock, program_id=program_id,
                                   target=target, definition_json=DEFINITION_JSON)

    def item(self, pk, sk):
        found = self.client.get_item(TableName=self.table, Key=encode_item({"PK": pk, "SK": sk}),
                                     ConsistentRead=True).get("Item")
        return decode_item(found) if found is not None else None

    def progress(self):
        return self.item("USER#" + PRINCIPAL, "STATE")

    def all_items(self):
        rows, request = [], {"TableName": self.table, "ConsistentRead": True}
        while True:
            response = self.client.scan(**request)
            rows.extend(decode_item(item) for item in response["Items"])
            if "LastEvaluatedKey" not in response:
                return rows
            request["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    def submit(self, auth, *, attempt=None, jobs=None, digest="a", manifest=None):
        attempt = attempt or self.create(auth)
        return (jobs or self.jobs).accept_input(
            auth, attempt["attempt_id"], digest * 64, manifest or blob("input/" + uuid.uuid4().hex),
            job_id=str(uuid.uuid4()), adapter_version="local-test", next_due_at=self.clock(),
        )

    def started(self, attempt, *, owner="worker-a"):
        action, job = self.jobs.claim(attempt["job_id"], owner, 60)
        assert action == "execute"
        permitted, job = self.jobs.begin_calculation(
            job["job_id"], owner, job["fence"], str(uuid.uuid4()),
            {"bucket": "private-test-bucket", "key": "calculation/" + uuid.uuid4().hex},
        )
        assert permitted
        return job

    def candidate(self, job, *, owner="worker-a"):
        ref = {**job["planned_candidate_ref"], "sha256": "b" * 64, "size": 10}
        return self.jobs.mark_calculation_saved(job["job_id"], owner, job["fence"], ref)

    def chart(self, job, *, owner="worker-a", letter=None):
        selection = {**binding(job), "kind": "no_chart"}
        if letter:
            selection.update(kind="snapshot", snapshot_ref=blob("chart/" + letter, letter),
                             source_sha256=letter * 64, published_body_sha256=letter * 64)
        return self.jobs.pin_chart(job["job_id"], owner, job["fence"], selection)

    def publication(self, selection):
        result = {**binding(selection), "kind": selection["kind"], "selection_revision": selection["revision"]}
        if selection["kind"] == "snapshot":
            result.update(key="legacy/chart.json", published_body_sha256=selection["published_body_sha256"])
        return result

    def ready(self, auth, *, owner="worker-a", chart=None):
        attempt = self.submit(auth)
        job = self.candidate(self.started(attempt, owner=owner), owner=owner)
        chosen = self.chart(job, owner=owner, letter=chart)
        return attempt, self.jobs.get_job(job["job_id"]), self.publication(chosen)

    def finalize(self, job, publication, *, owner="worker-a", jobs=None, assessment=None):
        return (jobs or self.jobs).finalize(
            job["job_id"], owner, job["fence"], blob("final/" + uuid.uuid4().hex, "c"),
            assessment or evaluation(), publication,
        )


@pytest.fixture
def jobs_world(dynamodb_client):
    table = create_journey_table(dynamodb_client, "arc_mock_p3_" + uuid.uuid4().hex)
    try:
        yield JobWorld(dynamodb_client, table)
    finally:
        dynamodb_client.delete_table(TableName=table)

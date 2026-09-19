"""Real AWS role assembly with a conditional fake DB, never actual AWS."""

from copy import deepcopy
import json
import uuid

import pytest

from mock_journey.aws_runtime import build_runtime
from mock_journey.dispatch import run
from mock_journey.errors import JourneyError
from tests.relay_progress_support import RelaySdk
from tests.test_aws_runtime import configuration, context, environment


def assembled(monkeypatch, *, config=None, initialize=True):
    import mock_journey.worker_runtime as runtime_module
    factory = RelaySdk()
    runtime = build_runtime("relay", environment("relay", config), client_factory=factory)
    if initialize:
        factory.db.put_item(**runtime.target.progress.initialization_request())
    factory.db.calls.clear()
    monkeypatch.setattr(runtime_module, "get_relay", lambda: runtime.target)
    return runtime, factory


def test_aws_composition_requires_shared_persistent_progress_without_constructor_io(monkeypatch):
    runtime, factory = assembled(monkeypatch, initialize=False)
    assert factory.db.calls == [] and factory.db.rows == {}
    assert runtime.target.progress.state is runtime.target.jobs.state
    assert runtime.target.progress.state.client.client is factory.db
    assert runtime.target.relay_budget == runtime.settings.relay_budget
    with pytest.raises(JourneyError) as error:
        run({"source": "aws.events"}, context())
    assert error.value.code == "TEMPORARILY_UNAVAILABLE"
    assert factory.db.rows == {} and factory.sent == []
    assert [name for name, _ in factory.db.calls] == ["get_item"]


@pytest.mark.parametrize("damage", ["schema", "binding", "extra", "cursor", "lease"])
def test_missing_or_invalid_initialized_state_is_not_reset(monkeypatch, damage):
    runtime, factory = assembled(monkeypatch)
    row = factory.db.rows[factory.db.key(runtime.target.progress.key)]
    if damage == "schema":
        row["schema"] = 2
    elif damage == "binding":
        row["binding_sha256"] = "a" * 64
    elif damage == "extra":
        row["PRIVATE-MARKER"] = "PRIVATE-MARKER"
    elif damage == "cursor":
        row["scans"]["JOB"]["cursor"] = {"PK": "PRIVATE-MARKER"}
    else:
        row["lease_until"] = True
    before = deepcopy(factory.db.rows)
    with pytest.raises(JourneyError) as error:
        run({"source": "aws.events"}, context())
    assert error.value.code == "TEMPORARILY_UNAVAILABLE" and "PRIVATE-MARKER" not in str(error.value)
    assert factory.db.rows == before
    assert [name for name, _ in factory.db.calls] == ["get_item"] and factory.sent == []


def test_queue_rebind_cannot_reuse_same_environment_progress(monkeypatch):
    runtime, factory = assembled(monkeypatch)
    config = configuration("relay")
    config["relay"]["queue_url"] += "-changed"
    before = deepcopy(factory.db.rows)
    replacement_factory = RelaySdk(factory.db)
    replacement = build_runtime("relay", environment("relay", config), client_factory=replacement_factory)
    import mock_journey.worker_runtime as runtime_module
    monkeypatch.setattr(runtime_module, "get_relay", lambda: replacement.target)
    with pytest.raises(JourneyError):
        run({"source": "aws.events"}, context())
    assert factory.db.rows == before and replacement_factory.sent == []


def test_stream_metadata_does_not_require_or_initialize_progress(monkeypatch):
    runtime, factory = assembled(monkeypatch, initialize=False)
    record = {"eventName": "INSERT", "dynamodb": {"SequenceNumber": "1", "Keys": {
        key: {"S": value} for key, value in runtime.target.progress.key.items()}}}
    assert run({"Records": [record]}, context()) == {"batchItemFailures": []}
    assert factory.db.calls == [] and factory.db.rows == {} and factory.sent == []


def test_scheduled_progress_is_shared_across_new_role_instances(monkeypatch):
    runtime, factory = assembled(monkeypatch)
    assert run({"source": "aws.events"}, context()) == {"outbox_wakes": 0, "job_wakes": 0, "failures": 0}
    key = factory.db.key(runtime.target.progress.key)
    first = deepcopy(factory.db.rows[key])
    assert first["revision"] > 0 and first["owner"] is None
    replacement_factory = RelaySdk(factory.db)
    replacement = build_runtime("relay", environment("relay"), client_factory=replacement_factory)
    import mock_journey.worker_runtime as runtime_module
    monkeypatch.setattr(runtime_module, "get_relay", lambda: replacement.target)
    run({"source": "aws.events"}, context())
    second = factory.db.rows[key]
    assert second["fence"] == first["fence"] + 1 and second["revision"] > first["revision"]
    assert second["owner"] is None


def test_sdk_boundary_deadline_after_query_does_not_consume_unread_job(monkeypatch):
    runtime, factory = assembled(monkeypatch)
    runtime.target.clock = lambda: 100
    runtime.target.progress.state.clock = lambda: 100
    for index in (1, 2):
        ident = str(uuid.UUID(int=index))
        row = {"PK": "JOB#" + ident, "SK": "STATE", "GSI1PK": "DUE#JOB", "GSI1SK": 1,
               "job_id": ident, "next_due_at": 1, "lease_until": 0, "state": "queued"}
        factory.db.rows[factory.db.key(row)] = row
    business_before = {key: deepcopy(row) for key, row in factory.db.rows.items() if key[0].startswith("JOB#")}
    remaining = [10000]
    ctx = context()
    ctx.get_remaining_time_in_millis = lambda: remaining[0]
    def after_query():
        if sum(name == "query" for name, _ in factory.db.calls) == 2:
            remaining[0] = 100
    factory.db.after_query = after_query
    result = run({"source": "aws.events"}, ctx)
    assert result["job_wakes"] == 0 and factory.sent == []
    saved = factory.db.rows[factory.db.key(runtime.target.progress.key)]
    assert saved["next_kind"] == "JOB" and saved["scans"]["JOB"] == {"cutoff": 100, "cursor": None}
    assert not any(name == "get_item" and request["Key"]["PK"]["S"].startswith("JOB#")
                   for name, request in factory.db.calls)
    assert {key: row for key, row in factory.db.rows.items() if key[0].startswith("JOB#")} == business_before
    record = json.loads(factory.logs[0]["Item"]["record"]["S"])
    assert record["fields"]["scan_exhausted"] is True

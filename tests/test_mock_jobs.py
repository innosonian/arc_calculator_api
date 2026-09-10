"""Job error/schema boundaries; real transaction predicates have separate tests."""
from copy import deepcopy
import hashlib
import json

from botocore.exceptions import ReadTimeoutError
import pytest

from mock_journey.errors import JourneyError
from mock_journey.jobs import DynamoJobRepository, JobLeaseLost
from mock_journey.state import DynamoStateRepository
from tests.test_mock_state import ScriptedClient, item, snapshot


DEFINITION = json.dumps({"goal": {"kind": "cycles", "required": 3}, "adapter_version": "v1", "projection_version": "v1"})
JOB = {
    "PK": "JOB#j", "SK": "STATE", "job_id": "j", "attempt_id": "a", "principal": "tester",
    "epoch": "e", "input_digest": "a" * 64, "adapter_version": "v1", "projection_version": "v1",
    "definition_sha256": hashlib.sha256(DEFINITION.encode()).hexdigest(),
    "state": "running", "call_phase": "not_started", "revision": 1, "owner": "worker",
    "lease_until": 100, "fence": 1, "call_id": None, "planned_candidate_ref": None,
    "candidate_ref": None, "chart_snapshot": {"kind": "unset", "revision": 0},
    "final_ref": None, "chart_publication": None,
}


def repo(calls):
    client = ScriptedClient(calls)
    return DynamoJobRepository(DynamoStateRepository(client, "local-table", clock=lambda: 20)), client


def expect(code, operation):
    with pytest.raises(JourneyError) as raised:
        operation()
    assert raised.value.code == code


def binding(job):
    return {key: job[key] for key in ("attempt_id", "epoch", "input_digest", "adapter_version", "projection_version", "job_id", "call_id")}


def assessment():
    return {"goal": {"kind": "cycles", "required": 3, "observed": 3, "met": True},
            "score": {"decision": "pass"}, "program_completed": True, "reason_codes": []}


@pytest.mark.parametrize("reference", [
    None, {"bucket": "x", "key": "x", "token": "PRIVATE"},
    {"bucket": "", "key": "x"}, {"bucket": "x", "key": 1},
])
def test_planned_reference_rejects_unknown_fields_before_database(reference):
    jobs, client = repo([])
    expect("STORED_INPUT_INVALID", lambda: jobs.begin_calculation("j", "worker", 1, "call", reference))
    assert not client.calls


def test_only_new_execution_transition_returns_calculation_permission():
    jobs, client = repo([("get", {"Item": item(JOB)}), ("write", {})])
    permission, result = jobs.begin_calculation("j", "worker", 1, "call", {"bucket": "b", "key": "k"})
    assert permission is True
    assert result["call_phase"] == "started" and result["planned_candidate_ref"] == {"bucket": "b", "key": "k"}
    assert JOB["call_id"] is None
    jobs, client = repo([("get", {"Item": item(result)})])
    permission, repeated = jobs.begin_calculation("j", "worker", 1, "call", {"bucket": "b", "key": "k"})
    assert permission is False and repeated == result
    assert len(client.calls) == 1


def test_execution_commit_response_loss_cannot_report_calculation_permission():
    jobs, _ = repo([("get", {"Item": item(JOB)}), ("write", ReadTimeoutError(endpoint_url="PRIVATE-ENDPOINT"))])
    expect("TEMPORARILY_UNAVAILABLE", lambda: jobs.begin_calculation("j", "worker", 1, "call", {"bucket": "b", "key": "k"}))


@pytest.mark.parametrize("change", [{"owner": "other"}, {"fence": 2}, {"lease_until": 20}, {"state": "done"}])
def test_stale_lease_cannot_issue_a_call_or_extend_ownership(change):
    for command in ("send", "renew"):
        jobs, client = repo([("get", {"Item": item({**JOB, **change})})])
        with pytest.raises(JobLeaseLost):
            if command == "send":
                jobs.begin_calculation("j", "worker", 1, "call", {"bucket": "b", "key": "k"})
            else:
                jobs.renew_lease("j", "worker", 1, 30)
        assert len(client.calls) == 1


def test_chart_binding_and_extra_secret_payload_do_not_enter_state():
    job = {**JOB, "call_id": "call", "call_phase": "candidate_saved"}
    for selection in ({**binding(job), "kind": "no_chart", "password": "PRIVATE"},
                      {**binding(job), "kind": "no_chart", "call_id": "different"}):
        jobs, client = repo([("get", {"Item": item(job)})])
        expect("STORED_INPUT_INVALID", lambda: jobs.pin_chart("j", "worker", 1, selection))
        assert len(client.calls) == 1


def test_chart_winner_is_reused_without_rewriting_another_candidate():
    chosen = {**binding({**JOB, "call_id": "call"}), "kind": "no_chart", "revision": 1}
    job = {**JOB, "call_id": "call", "call_phase": "candidate_saved", "chart_snapshot": chosen}
    jobs, client = repo([("get", {"Item": item(job)})])
    assert jobs.pin_chart("j", "worker", 1, {**binding(job), "kind": "no_chart"}) == chosen
    assert len(client.calls) == 1


@pytest.mark.parametrize("change", [
    {"program_completed": False}, {"reason_codes": ["PRIVATE"]},
    {"goal": {"kind": "cycles", "required": 1, "observed": 3, "met": True}},
    {"goal": {"kind": "cycles", "required": 3, "observed": True, "met": True}},
    {"goal": {"kind": "cycles", "required": 3, "observed": 2, "met": True}},
    {"score": {"decision": []}}, {"score": {"decision": "pass", "password": "PRIVATE"}},
])
def test_completion_evaluation_rejects_type_and_policy_substitution(change):
    value = {**assessment(), **change}
    expect("CALCULATOR_CONTRACT_MISMATCH", lambda: DynamoJobRepository._evaluation(value, {"definition_json": DEFINITION}))


def test_valid_evaluation_is_detached_from_mutable_caller_data():
    original = assessment()
    checked = DynamoJobRepository._evaluation(original, {"definition_json": DEFINITION})
    checked["goal"]["observed"] = 999
    assert original["goal"]["observed"] == 3


def test_job_detects_mutated_attempt_definition_before_running_or_closing_activity():
    attempt = {
        "attempt_id": "a", "principal": "tester", "epoch": "e", "job_id": "j", "input_digest": "a" * 64,
        "definition_json": DEFINITION + " ", "state": "queued", "revision": 1,
    }
    jobs, client = repo([("get", {"Item": item(JOB)}), ("read", snapshot(JOB, attempt))])
    expect("STORED_INPUT_INVALID", lambda: jobs.claim("j", "worker", 60))
    assert len(client.calls) == 2


def test_real_failure_classification_never_persists_an_arbitrary_exception_message():
    jobs, client = repo([])
    expect("TEMPORARILY_UNAVAILABLE", lambda: jobs.mark_failed("j", "worker", 1, "PRIVATE-ERROR"))
    expect("TEMPORARILY_UNAVAILABLE", lambda: jobs.mark_unknown("j", "worker", 1, "PRIVATE-ERROR", next_due_at=20))
    assert not client.calls


def test_future_due_job_cannot_be_reclaimed_by_duplicate_queue_delivery():
    job = {**JOB, "state": "outcome_unknown", "call_phase": "started", "owner": None,
           "lease_until": 0, "next_due_at": 21}
    attempt = {"attempt_id": "a", "principal": "tester", "epoch": "e", "job_id": "j",
               "input_digest": "a" * 64, "definition_json": DEFINITION}
    jobs, client = repo([("get", {"Item": item(job)}), ("read", snapshot(job, attempt))])
    action, found = jobs.claim("j", "new-worker", 60)
    assert action == "busy" and found["fence"] == job["fence"]
    assert len(client.calls) == 2


@pytest.mark.parametrize("phase", ["never_sent", "sent", "response_saved", "unrecognized", None])
def test_unrecognized_execution_protocol_is_not_automatically_migrated(phase):
    job = {**JOB, "call_phase": phase}
    attempt = {"attempt_id": "a", "principal": "tester", "epoch": "e", "job_id": "j",
               "input_digest": "a" * 64, "definition_json": DEFINITION}
    jobs, client = repo([("get", {"Item": item(job)}), ("read", snapshot(job, attempt))])
    expect("TEMPORARILY_UNAVAILABLE", lambda: jobs.claim("j", "new-worker", 60))
    assert len(client.calls) == 2


def test_missing_candidate_can_rotate_only_under_a_newer_execution_fence():
    old = {**JOB, "call_phase": "started", "call_id": "old-call", "execution_fence": 1,
           "planned_candidate_ref": {"bucket": "b", "key": "old-key"}}
    reference = {"bucket": "b", "key": "new-key"}
    for change, previous in (({}, "old-call"), ({"fence": 2}, "wrong-call"),
                             ({"fence": 2, "candidate_ref": {"stored": True}}, "old-call"),
                             ({"fence": 2, "execution_fence": True}, "old-call")):
        row = {**old, **change}
        jobs, client = repo([("get", {"Item": item(row)})])
        allowed, found = jobs.begin_calculation("j", "worker", row["fence"], "new-call", reference,
                                               previous_call_id=previous)
        assert not allowed and found == row and len(client.calls) == 1
    row = {**old, "fence": 2}
    jobs, client = repo([("get", {"Item": item(row)}), ("write", {})])
    allowed, current = jobs.begin_calculation("j", "worker", 2, "new-call", reference,
                                             previous_call_id="old-call")
    assert allowed and current["call_id"] == "new-call" and current["execution_fence"] == 2
    assert current["planned_candidate_ref"] == reference and old["call_id"] == "old-call"


def test_future_due_outbox_cannot_be_reclaimed_by_retried_stream_delivery():
    outbox = {"state": "pending", "owner": None, "lease_until": 0, "next_due_at": 21, "fence": 2}
    jobs, client = repo([("get", {"Item": item(outbox)})])
    assert jobs.claim_outbox("j", "relay", 60) == ("busy", outbox)
    assert len(client.calls) == 1


@pytest.mark.parametrize("kind", ["no_chart", "snapshot"])
@pytest.mark.parametrize("revision", [None, True, 1.0, 0, 2])
def test_chart_publication_revision_requires_the_exact_selected_integer(kind, revision):
    chosen = {**binding({**JOB, "call_id": "call"}), "kind": kind, "revision": 1}
    publication = {**binding(chosen), "kind": kind, "selection_revision": 1}
    if kind == "snapshot":
        chosen["published_body_sha256"] = "f" * 64
        publication.update(key="chart.json", published_body_sha256="f" * 64)
    job = {**JOB, "call_id": "call", "chart_snapshot": chosen}
    DynamoJobRepository._publication(publication, job)
    if revision is None:
        publication.pop("selection_revision")
    else:
        publication["selection_revision"] = revision
    expect("STORED_INPUT_INVALID", lambda: DynamoJobRepository._publication(publication, job))

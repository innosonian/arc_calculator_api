"""Queue acknowledgements, reconciliation and public response boundaries."""

from copy import deepcopy
import json
from types import SimpleNamespace
import uuid

import pytest

from mock_journey.contracts import CalculatorRegistry, VerifiedCalculation
from mock_journey.dispatch import OutboxRelay, QueueSender, handle_stream
from mock_journey.errors import JourneyError
from mock_journey.handler import handle as api_handle
from mock_journey.worker import evaluate, handle as queue_handle


JOB = str(uuid.uuid4())


@pytest.mark.parametrize("score,observed,completed,reasons", [
    (90, 3, True, []), (90, 2, False, ["GOAL_NOT_MET"]),
    (70, 3, False, ["SCORE_NOT_PASS"]),
    (70, 2, False, ["GOAL_NOT_MET", "SCORE_NOT_PASS"]),
    (None, 3, False, ["SCORE_NOT_PASS"]),
])
def test_completion_is_goal_and_existing_tester_pass(score, observed, completed, reasons):
    result = {"cpr_score": {"total_score": {"overall": score}}, "certification": {"Target": "N/A"}}
    definition = {"condition": {"target": "infant"}, "goal": {"kind": "cycles", "required": 3},
                  "calculation_profile": {"Open_Skill": {"Passing_Score": 0}}}
    verified = VerifiedCalculation({}, "cycles", observed, "no_chart")
    outcome = evaluate(result, definition, verified)
    assert outcome["program_completed"] is completed and outcome["reason_codes"] == reasons


def test_goal_evidence_types_and_missing_old_adapters_are_not_score_failures():
    for count in (True, 3.0, -1, None):
        with pytest.raises(JourneyError) as error:
            VerifiedCalculation({}, "cycles", count, "no_chart")
        assert error.value.code == "CALCULATOR_CONTRACT_MISMATCH"
    with pytest.raises(JourneyError) as error:
        CalculatorRegistry([]).resolve("retained-version", "retained-schema")
    assert error.value.code == "TEMPORARILY_UNAVAILABLE"
    with pytest.raises(JourneyError) as error:
        VerifiedCalculation({"score": float("nan")}, "cycles", 3, "no_chart")
    assert error.value.code == "CALCULATOR_CONTRACT_MISMATCH"


def test_queue_busy_is_not_acknowledged_and_malformed_body_does_not_leak(capsys):
    calls = []
    worker = SimpleNamespace(process=lambda ident: calls.append(ident) or False)
    records = [
        {"messageId": "busy", "body": json.dumps({"job_id": JOB})},
        {"messageId": "extra", "body": json.dumps({"job_id": JOB, "token": "PRIVATE-QUEUE-MARKER"})},
        {"messageId": "invalid", "body": "not-json"},
    ]
    result = queue_handle({"Records": records}, None, worker)
    assert result == {"batchItemFailures": [{"itemIdentifier": ident} for ident in ("busy", "extra", "invalid")]}
    assert calls == [JOB] and "PRIVATE-QUEUE-MARKER" not in capsys.readouterr().out
    worker.process = lambda _: True
    assert queue_handle({"Records": records[:1]}, None, worker) == {"batchItemFailures": []}


def test_queue_sender_contains_only_job_reference():
    calls = []
    client = SimpleNamespace(send_message=lambda **kwargs: calls.append(kwargs) or {"MessageId": "sent"})
    QueueSender(client, "https://sqs.example.invalid/local-test").send(JOB)
    assert json.loads(calls[0]["MessageBody"]) == {"job_id": JOB}
    assert set(calls[0]) == {"QueueUrl", "MessageBody"}


def test_send_success_then_outbox_commit_loss_remains_retryable():
    actions = []
    def lost_commit(*args):
        raise JourneyError("TEMPORARILY_UNAVAILABLE")
    jobs = SimpleNamespace(
        claim_outbox=lambda *args: ("send", {"fence": 4}),
        mark_outbox_sent=lost_commit,
        release_outbox=lambda *args, **kwargs: actions.append((args, kwargs)),
    )
    sent = []
    relay = OutboxRelay(jobs, SimpleNamespace(send=lambda ident: sent.append(ident)),
                        lease_seconds=30, retry_seconds=5, page_size=10, max_pages=2, clock=lambda: 1000)
    assert relay.dispatch(JOB) is False
    assert sent == [JOB] and actions[0][0][2] == 4
    assert actions[0][1] == {"next_due_at": 1005, "error_code": "TEMPORARILY_UNAVAILABLE"}


def test_reconciliation_wakes_jobs_independently_of_sent_outbox_and_pages():
    calls = []
    def due_jobs(*, limit, cursor):
        calls.append((limit, cursor))
        if cursor is None:
            return [{"job_id": "unknown-after-dlq"}], {"PK": "continue"}
        return [{"job_id": "response-saved-without-wake"}], None
    jobs = SimpleNamespace(due_outbox=lambda **kwargs: ([], None), due_jobs=due_jobs)
    sent = []
    relay = OutboxRelay(jobs, SimpleNamespace(send=lambda ident: sent.append(ident)),
                        lease_seconds=30, retry_seconds=5, page_size=10, max_pages=2)
    assert relay.reconcile() == {"outbox_wakes": 0, "job_wakes": 2, "failures": 0}
    assert sent == ["unknown-after-dlq", "response-saved-without-wake"]
    assert calls == [(10, None), (10, {"PK": "continue"})]


def test_stream_reads_outbox_keys_without_enqueuing_new_image(capsys):
    called = []
    relay = SimpleNamespace(dispatch=lambda ident: called.append(ident) or True)
    record = {"eventName": "INSERT", "dynamodb": {"SequenceNumber": "1", "Keys": {
        "PK": {"S": "OUTBOX#" + JOB}, "SK": {"S": "DISPATCH"},
    }, "NewImage": {"private": "PRIVATE-STREAM-MARKER"}}}
    result = handle_stream({"Records": [record]}, None, relay)
    assert result == {"batchItemFailures": []} and called == [JOB]
    assert "PRIVATE-STREAM-MARKER" not in capsys.readouterr().out
    record["eventName"] = "MODIFY"
    assert handle_stream({"Records": [record]}, None, relay) == {"batchItemFailures": []}
    assert called == [JOB]  # Retry timestamp/lease writes do not trigger a hot loop.


def test_calculation_http_preserves_calculated_values_and_authenticates_before_body():
    frozen = b'{ "integer":80, "float":80.0, "null":null, "string":"80" }'
    calls = []
    calculation = SimpleNamespace(result=lambda auth, ident: (200, frozen),
                                  submit=lambda *args: calls.append(args) or (202, {"wait_expired": False}))
    def authenticate(token, **kwargs):
        if token != "valid-test-token":
            raise JourneyError("SESSION_REQUIRED")
        return "authenticated"
    service = SimpleNamespace(auth=SimpleNamespace(authenticate=authenticate), require_calculation=lambda: calculation)
    request = {"httpMethod": "GET", "path": f"/mock/v1/attempts/{JOB}/calculation",
               "headers": {"Authorization": "Bearer valid-test-token"}}
    result = api_handle(request, None, service)
    assert result["statusCode"] == 200
    decoded = json.loads(result["body"])
    assert decoded.pop("submit_arc") == {"status": "disabled", "ok": False, "error": "arc_contract_pending"}
    assert json.dumps(decoded) == json.dumps(json.loads(frozen))
    assert frozen == b'{ "integer":80, "float":80.0, "null":null, "string":"80" }'
    request.update(httpMethod="POST", body="PRIVATE-MEASUREMENT-MARKER", headers={})
    assert api_handle(request, None, service)["statusCode"] == 401 and not calls

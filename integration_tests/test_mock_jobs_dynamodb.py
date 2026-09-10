"""P3 transaction/crash boundaries against explicitly configured DynamoDB Local."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import uuid

import pytest

from integration_tests.test_mock_state_dynamodb import World, OneTransactionInterruption, SLOT
from mock_journey.errors import JourneyError
from mock_journey.jobs import DynamoJobRepository, JobLeaseLost


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


class JobWorld(World):
    def __init__(self, client, table):
        super().__init__(client, table)
        self.jobs = DynamoJobRepository(self.repository)

    def job_repo(self, client):
        return DynamoJobRepository(self.repo(client))

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
    table = "arc_mock_p3_" + uuid.uuid4().hex
    dynamodb_client.create_table(
        TableName=table,
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
    try:
        yield JobWorld(dynamodb_client, table)
    finally:
        dynamodb_client.delete_table(TableName=table)


def parallel(*calls):
    with ThreadPoolExecutor(max_workers=len(calls)) as executor:
        futures = [executor.submit(call) for call in calls]
        results = []
        for future in futures:
            try:
                results.append(future.result(timeout=20))
            except (JourneyError, JobLeaseLost) as error:
                results.append(error)
        return results


def test_different_input_candidates_select_exactly_one_job_and_manifest(jobs_world):
    w = jobs_world
    auth = w.session()
    attempt = w.create(auth)
    barrier = Barrier(2)
    repos = [w.job_repo(OneTransactionInterruption(w.client, before=lambda: barrier.wait(timeout=10))) for _ in range(2)]
    refs = [blob("candidate/a", "a"), blob("candidate/b", "b")]
    results = parallel(*[
        lambda index=index: w.submit(auth, attempt=attempt, jobs=repos[index], digest="ab"[index], manifest=refs[index])
        for index in range(2)
    ])
    success = [result for result in results if isinstance(result, dict)]
    failures = [result for result in results if isinstance(result, JourneyError)]
    assert len(success) == len(failures) == 1
    assert failures[0].code == "ATTEMPT_INPUT_CONFLICT"
    stored = w.repository.get_attempt(auth, attempt["attempt_id"])
    index = 0 if stored["input_digest"] == "a" * 64 else 1
    assert stored["input_manifest_ref"] == refs[index]
    assert len([r for r in w.all_items() if r["PK"].startswith("JOB#")]) == 1
    assert len([r for r in w.all_items() if r["PK"].startswith("OUTBOX#")]) == 1


def test_same_input_race_reuses_job_despite_different_candidate_addresses(jobs_world):
    w = jobs_world
    auth = w.session()
    attempt = w.create(auth)
    barrier = Barrier(2)
    repos = [w.job_repo(OneTransactionInterruption(w.client, before=lambda: barrier.wait(timeout=10))) for _ in range(2)]
    results = parallel(*[lambda repo=repo: w.submit(auth, attempt=attempt, jobs=repo) for repo in repos])
    assert all(isinstance(result, dict) for result in results), results
    assert results[0]["job_id"] == results[1]["job_id"]
    assert results[0]["input_manifest_ref"] == results[1]["input_manifest_ref"]


@pytest.mark.parametrize("interruption,code", [("logout", "SESSION_REVOKED"), ("cancel", "INVALID_STATE")])
def test_accept_cannot_cross_logout_or_cancel(jobs_world, interruption, code):
    w = jobs_world
    auth = w.session()
    attempt = w.create(auth)
    interrupt = (lambda: w.repository.logout(auth)) if interruption == "logout" else (
        lambda: w.repository.cancel_attempt(auth, attempt["attempt_id"], "user_stopped"))
    jobs = w.job_repo(OneTransactionInterruption(w.client, before=interrupt))
    with pytest.raises(JourneyError) as error:
        w.submit(auth, attempt=attempt, jobs=jobs)
    assert error.value.code == code
    assert not [r for r in w.all_items() if r["PK"].startswith(("JOB#", "OUTBOX#"))]


def test_accepted_commit_response_loss_does_not_create_a_second_job(jobs_world):
    w = jobs_world
    auth = w.session()
    attempt = w.create(auth)
    with pytest.raises(JourneyError) as error:
        w.submit(auth, attempt=attempt, jobs=w.job_repo(OneTransactionInterruption(w.client, lose_response=True)))
    assert error.value.code == "TEMPORARILY_UNAVAILABLE"
    winner = w.repository.get_attempt(auth, attempt["attempt_id"])
    assert w.submit(auth, attempt=attempt)["job_id"] == winner["job_id"]
    assert len([r for r in w.all_items() if r["PK"].startswith("JOB#")]) == 1


def test_execution_intent_response_loss_does_not_reissue_under_the_same_lease(jobs_world):
    w = jobs_world
    attempt = w.submit(w.session())
    _, job = w.jobs.claim(attempt["job_id"], "owner", 60)
    jobs = w.job_repo(OneTransactionInterruption(w.client, lose_response=True))
    args = (job["job_id"], "owner", job["fence"], "call-one", {"bucket": "private-test-bucket", "key": "calculation/one"})
    with pytest.raises(JourneyError):
        jobs.begin_calculation(*args)
    allowed, stored = w.jobs.begin_calculation(*args)
    assert allowed is False and stored["call_phase"] == "started"
    w.clock.now += 61
    action, recovered = w.jobs.claim(job["job_id"], "new-owner", 60)
    assert action == "recover" and recovered["fence"] > job["fence"]
    assert recovered["call_id"] == "call-one" and recovered["planned_candidate_ref"] == args[-1]


def test_missing_candidate_rotation_fences_previous_writer_and_repeated_rotation(jobs_world):
    w = jobs_world
    old = w.started(w.submit(w.session()))
    w.clock.now += 61
    _, current = w.jobs.claim(old["job_id"], "new-owner", 60)
    args = (old["job_id"], "new-owner", current["fence"], "new-calculation",
            {"bucket": "private-test-bucket", "key": "calculation/new-candidate"})
    allowed, chosen = w.jobs.begin_calculation(*args, previous_call_id=old["call_id"])
    assert allowed and chosen["call_id"] != old["call_id"]
    assert chosen["execution_fence"] == current["fence"]
    with pytest.raises(JobLeaseLost):
        w.candidate(old)
    with pytest.raises(JourneyError) as error:
        w.jobs.mark_calculation_saved(old["job_id"], "new-owner", current["fence"],
                                      {**old["planned_candidate_ref"], "sha256": "b" * 64, "size": 10})
    assert error.value.code == "STORED_INPUT_INVALID"
    allowed, repeated = w.jobs.begin_calculation(*args, previous_call_id=old["call_id"])
    assert not allowed and repeated["call_id"] == chosen["call_id"]


def test_unknown_retry_due_cannot_be_bypassed_by_duplicate_queue_wake(jobs_world):
    w = jobs_world
    job = w.started(w.submit(w.session()))
    due = w.clock() + 30
    unknown = w.jobs.mark_unknown(job["job_id"], "worker-a", job["fence"],
                                 "CALCULATION_OUTCOME_UNKNOWN", next_due_at=due)
    assert w.jobs.claim(job["job_id"], "early", 60) == ("busy", unknown)
    assert not w.jobs.due_jobs(limit=10)[0]
    w.clock.now = due
    action, recovered = w.jobs.claim(job["job_id"], "due", 60)
    assert action == "recover" and recovered["fence"] == job["fence"] + 1
    assert recovered["call_id"] == job["call_id"] and recovered["call_phase"] == "started"


def test_outbox_retry_due_cannot_be_bypassed_by_stream_or_explicit_wake(jobs_world):
    w = jobs_world
    attempt = w.submit(w.session())
    _, outbox = w.jobs.claim_outbox(attempt["job_id"], "relay-a", 60)
    due = w.clock() + 30
    released = w.jobs.release_outbox(attempt["job_id"], "relay-a", outbox["fence"],
                                    next_due_at=due, error_code="TEMPORARILY_UNAVAILABLE")
    assert w.jobs.claim_outbox(attempt["job_id"], "early", 60) == ("busy", released)
    assert not w.jobs.due_outbox(limit=10)[0]
    w.clock.now = due
    action, next_claim = w.jobs.claim_outbox(attempt["job_id"], "relay-b", 60)
    assert action == "send" and next_claim["delivery_attempts"] == 2


@pytest.mark.parametrize("chart", [None, "d"])
def test_finalize_requires_explicit_typed_chart_revision_without_partial_progress(jobs_world, chart):
    w = jobs_world
    auth = w.session()
    attempt, job, publication = w.ready(auth, chart=chart)
    for revision in (None, True, 1.0):
        invalid = dict(publication)
        if revision is None:
            invalid.pop("selection_revision")
        else:
            invalid["selection_revision"] = revision
        with pytest.raises(JourneyError) as error:
            w.finalize(job, invalid)
        assert error.value.code == "STORED_INPUT_INVALID"
        assert w.jobs.get_job(job["job_id"])["state"] == "running"
        assert w.repository.get_attempt(auth, attempt["attempt_id"])["state"] == "processing"
        assert not w.repository.get_progress(auth)["slots"][SLOT]["completed"]
    assert w.finalize(job, publication)["state"] == "evaluated"


def test_busy_lease_and_stale_worker_cannot_finalize_or_renew(jobs_world):
    w = jobs_world
    _, job, publication = w.ready(w.session())
    assert w.jobs.claim(job["job_id"], "other", 60)[0] == "busy"
    w.clock.now += 61
    action, new = w.jobs.claim(job["job_id"], "other", 60)
    assert action == "recover"
    with pytest.raises(JobLeaseLost):
        w.jobs.renew_lease(job["job_id"], "worker-a", job["fence"], 60)
    with pytest.raises(JobLeaseLost):
        w.finalize(job, publication)
    assert w.finalize(new, publication, owner="other")["state"] == "evaluated"


def test_candidate_address_is_predeclared_and_saved_content_cannot_be_swapped(jobs_world):
    w = jobs_world
    job = w.started(w.submit(w.session()))
    with pytest.raises(JourneyError):
        w.jobs.mark_calculation_saved(job["job_id"], "worker-a", job["fence"], blob("wrong/address"))
    saved = w.candidate(job)
    with pytest.raises(JourneyError):
        w.jobs.mark_calculation_saved(job["job_id"], "worker-a", job["fence"], {**saved["candidate_ref"], "sha256": "f" * 64})


def test_chart_race_returns_the_same_immutable_selection(jobs_world):
    w = jobs_world
    job = w.candidate(w.started(w.submit(w.session())))
    barrier = Barrier(2)
    repos = [w.job_repo(OneTransactionInterruption(w.client, before=lambda: barrier.wait(timeout=10))) for _ in range(2)]
    choices = [{**binding(job), "kind": "snapshot", "snapshot_ref": blob("chart/" + letter, letter),
                "published_body_sha256": letter * 64, "source_sha256": letter * 64} for letter in "cd"]
    results = parallel(*[
        lambda index=index: repos[index].pin_chart(job["job_id"], "worker-a", job["fence"], choices[index])
        for index in range(2)
    ])
    assert all(isinstance(result, dict) for result in results), results
    assert results[0] == results[1] and results[0]["revision"] == 1
    assert w.jobs.get_job(job["job_id"])["chart_snapshot"] == results[0]


def test_unselected_or_mismatched_chart_prevents_false_final_result(jobs_world):
    w = jobs_world
    job = w.candidate(w.started(w.submit(w.session())))
    with pytest.raises(JourneyError):
        w.finalize(job, {**binding(job), "kind": "no_chart"})
    publication = w.publication(w.chart(job, letter="c"))
    for field, value in (("published_body_sha256", "d" * 64), ("selection_revision", 2), ("call_id", "another")):
        with pytest.raises(JourneyError):
            w.finalize(job, {**publication, field: value})
    assert w.jobs.get_job(job["job_id"])["final_ref"] is None
    assert not w.progress()["slots"][SLOT]["completed"]


def test_two_passes_race_records_one_shared_completion_and_zero_open_count(jobs_world):
    w = jobs_world
    auth = w.session()
    ready = [w.ready(auth, owner=owner) for owner in ("worker-a", "worker-b")]
    barrier = Barrier(2)
    repos = [w.job_repo(OneTransactionInterruption(w.client, before=lambda: barrier.wait(timeout=10))) for _ in range(2)]
    results = parallel(*[
        lambda index=index: w.finalize(ready[index][1], ready[index][2], owner=("worker-a", "worker-b")[index], jobs=repos[index])
        for index in range(2)
    ])
    assert all(isinstance(result, dict) for result in results), results
    assert {r["progress_application"]["reason"] for r in results} == {"APPLIED", "ALREADY_COMPLETED"}
    assert all(r["evaluation"]["program_completed"] for r in results)
    assert w.progress()["slots"][SLOT]["completed"] and w.progress()["slots"][SLOT]["open_attempts"] == 0
    with pytest.raises(JourneyError) as error:
        w.create(auth)
    assert error.value.code == "PROGRAM_ALREADY_COMPLETED"


@pytest.mark.parametrize("assessment", [evaluation(passed=False), evaluation(observed=2)])
def test_later_fail_or_short_goal_cannot_erase_another_completion(jobs_world, assessment):
    w = jobs_world
    auth = w.session()
    _, first, pub1 = w.ready(auth)
    _, second, pub2 = w.ready(auth, owner="worker-b")
    w.finalize(first, pub1)
    failed = w.finalize(second, pub2, owner="worker-b", assessment=assessment)
    assert failed["progress_application"]["reason"] == "REQUIREMENTS_NOT_MET"
    assert w.progress()["slots"][SLOT]["completed"]


def test_logout_during_final_commit_saves_result_without_new_epoch_completion(jobs_world):
    w = jobs_world
    auth = w.session()
    _, job, publication = w.ready(auth)
    jobs = w.job_repo(OneTransactionInterruption(w.client, before=lambda: w.repository.logout(auth)))
    result = w.finalize(job, publication, jobs=jobs)
    assert result["evaluation"]["program_completed"] is True
    assert result["progress_application"] == {"applied": False, "applied_epoch": None, "reason": "PROGRESS_RESET"}
    assert not w.progress()["slots"][SLOT]["completed"] and w.progress()["slots"][SLOT]["open_attempts"] == 0
    assert w.jobs.get_job(job["job_id"])["final_ref"] == result["result_ref"]


def test_late_result_after_session_expiry_and_final_pointer_immutability(jobs_world):
    w = jobs_world
    auth = w.session()
    _, job, publication = w.ready(auth)
    w.clock.now += 86401
    action, recovered = w.jobs.claim(job["job_id"], "recoverer", 60)
    assert action == "recover"
    result = w.finalize(recovered, publication, owner="recoverer")
    repeated = w.finalize(recovered, publication, owner="another-worker", assessment=evaluation(passed=False))
    assert repeated == result and result["evaluation"]["program_completed"]


def test_unknown_closes_activity_once_and_recovers_existing_candidate(jobs_world):
    w = jobs_world
    auth = w.session()
    attempt = w.submit(auth)
    job = w.started(attempt)
    unknown = w.jobs.mark_unknown(job["job_id"], "worker-a", job["fence"],
                                  "CALCULATION_OUTCOME_UNKNOWN", next_due_at=w.clock())
    assert unknown["GSI1PK"] == "DUE#JOB" and w.progress()["slots"][SLOT]["open_attempts"] == 0
    action, recovered = w.jobs.claim(job["job_id"], "recoverer", 60)
    assert action == "recover"
    current = w.repository.get_attempt(auth, attempt["attempt_id"])
    assert current["state"] == "outcome_unknown" and current["evaluation"] is None
    assert w.progress()["slots"][SLOT]["open_attempts"] == 0
    w.candidate(recovered, owner="recoverer")
    chosen = w.chart(recovered, owner="recoverer")
    result = w.finalize(recovered, w.publication(chosen), owner="recoverer")
    assert result["evaluation"]["program_completed"] and "error_code" not in result
    assert w.progress()["slots"][SLOT]["open_attempts"] == 0


def test_processing_failure_is_not_a_score_failure_or_completion(jobs_world):
    w = jobs_world
    auth = w.session()
    attempt = w.submit(auth)
    _, job = w.jobs.claim(attempt["job_id"], "worker", 60)
    failed = w.jobs.mark_failed(job["job_id"], "worker", job["fence"], "STORED_INPUT_INVALID")
    assert failed["state"] == "failed" and "GSI1PK" not in failed
    stored = w.repository.get_attempt(auth, attempt["attempt_id"])
    assert stored["evaluation"] is stored["progress_application"] is None
    assert not w.progress()["slots"][SLOT]["completed"] and w.progress()["slots"][SLOT]["open_attempts"] == 0
    assert w.jobs.claim(job["job_id"], "other", 60)[0] == "done"


def test_outbox_receipt_loss_allows_duplicate_delivery_but_job_remains_due(jobs_world):
    w = jobs_world
    attempt = w.submit(w.session())
    action, first = w.jobs.claim_outbox(attempt["job_id"], "relay-one", 10)
    assert action == "send"
    assert w.jobs.claim_outbox(attempt["job_id"], "relay-two", 10)[0] == "busy"
    w.clock.now += 11  # Queue send succeeded before the relay lost its receipt.
    action, second = w.jobs.claim_outbox(attempt["job_id"], "relay-two", 10)
    assert action == "send" and second["fence"] > first["fence"]
    with pytest.raises(JobLeaseLost):
        w.jobs.mark_outbox_sent(attempt["job_id"], "relay-one", first["fence"])
    w.jobs.mark_outbox_sent(attempt["job_id"], "relay-two", second["fence"])
    assert w.jobs.due_outbox(limit=20)[0] == []
    assert [j["job_id"] for j in w.jobs.due_jobs(limit=20)[0]] == [attempt["job_id"]]


def test_due_page_rechecks_base_rows_instead_of_trusting_stale_index(jobs_world):
    w = jobs_world
    attempt = w.submit(w.session())
    original = w.client.query

    class StaleIndex:
        def __getattr__(self, name):
            return getattr(w.client, name)

        def query(self, **kwargs):
            response = original(**kwargs)
            action, outbox = w.jobs.claim_outbox(attempt["job_id"], "relay", 10)
            assert action == "send"
            w.jobs.mark_outbox_sent(attempt["job_id"], "relay", outbox["fence"])
            return response

    assert w.job_repo(StaleIndex()).due_outbox(limit=20)[0] == []


def test_due_pagination_keeps_all_outbox_references(jobs_world):
    w = jobs_world
    auth = w.session()
    expected = {w.submit(auth)["job_id"] for _ in range(3)}
    found, cursor = set(), None
    while True:
        rows, cursor = w.jobs.due_outbox(limit=1, cursor=cursor)
        found.update(row["job_id"] for row in rows)
        if cursor is None:
            break
    assert found == expected


def test_profile_or_evaluation_substitution_cannot_finalize(jobs_world):
    w = jobs_world
    auth = w.session()
    attempt = w.create(auth)
    with pytest.raises(JourneyError) as error:
        w.jobs.accept_input(auth, attempt["attempt_id"], "a" * 64, blob("input"),
                            job_id=str(uuid.uuid4()), adapter_version="unverified", next_due_at=w.clock())
    assert error.value.code == "PROFILE_MISMATCH"
    _, job, publication = w.ready(auth)
    forged = evaluation(observed=2)
    forged["program_completed"] = True
    with pytest.raises(JourneyError) as error:
        w.finalize(job, publication, assessment=forged)
    assert error.value.code == "CALCULATOR_CONTRACT_MISMATCH"
    assert w.jobs.get_job(job["job_id"])["state"] != "done"

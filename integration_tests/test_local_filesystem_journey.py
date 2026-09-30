"""Real local files + DynamoDB + internal worker; no default CLI/socket claim.

Course attempts through the public /api/v2 harness (FilesJourney: LocalObjectClient
and signed local charts on a private installation directory). No v1 API route
or v1-only service method is used.
"""

import hashlib
import json
from pathlib import Path

from botocore.exceptions import ClientError
import pytest

from integration_tests.worker_journey_support import (  # noqa: F401 (store, files_journey fixtures)
    ARTIFACT_LIMIT, DUMMY_SUBMISSION, FILES_BUCKET, accepted, attempt_row, calculation, chart_link, course_rows,
    files_journey, item_view, job_row, start, store, submit,
)
from mock_journey.errors import JourneyError
from mock_journey.worker import call_binding, input_binding
from tests._synth import comp_session, cpr_session
from tests.journey_support import V2Journey, dummy_course


CPR = dummy_course("mock-cpr", "adult")
COMPRESSION = dummy_course("mock-compression-only", "adult")
CPR_DATA = cpr_session([(30, 2)] * 3)


def unavailable(*args, **kwargs):
    raise JourneyError("TEMPORARILY_UNAVAILABLE")


def forbid_calculation(monkeypatch, world, message):
    monkeypatch.setattr(world.calculator, "calculate", lambda *a, **k: pytest.fail(message))


@pytest.mark.parametrize("course,data,completed,status", [
    (CPR, CPR_DATA, True, "evaluated"),
    (COMPRESSION, comp_session(60), True, "evaluated"),
], ids=("cpr-completed", "only-completed"))
def test_real_file_pipeline_reopens_without_recalculation(files_journey, course, data, completed, status, monkeypatch):
    world = files_journey
    token, attempt, job_id = accepted(world, course, data=data)
    assert world.work(job_id=job_id)
    job = job_row(world, job_id)
    storage = world.worker.storage
    loaded = storage.load_input(job["input_manifest_ref"], input_binding(job))
    assert loaded.projected.cpr_bytes == world.body(loaded.raw_base + ".bin") == data
    assert json.loads(world.body(loaded.raw_base + ".request.json"))["raw_base"] == loaded.raw_base
    assert type(json.loads(world.body(loaded.raw_base + ".meta.json"))) is dict
    raw = storage.load_calculation(job["planned_candidate_ref"], call_binding(job))
    assert json.loads(raw)["goal"]["status"] == status
    publication = job["chart_publication"]
    published = world.body(publication["key"])
    assert hashlib.sha256(published).hexdigest() == publication["published_body_sha256"]
    final = storage.read_final(job["final_ref"], call_binding(job), publication)
    assert "submit_arc" not in json.loads(final)
    response = calculation(world, token, attempt["attemptId"])
    assert response.status == 200
    assert response.data["submit_arc"] == DUMMY_SUBMISSION
    url = response.data["calculation"]["chart_dataset_url"]
    assert world.chart_bytes(url) == published
    saved = attempt_row(world, attempt["attemptId"])
    assert saved["evaluation"]["program_completed"] is completed
    assert saved["evaluation"]["goal"]["status"] == status
    rows = course_rows(world, attempt["attemptId"])
    before = world.file_hashes()
    restarted = world.reopen()
    forbid_calculation(monkeypatch, restarted, "A committed file result was recalculated.")
    assert restarted.work(job_id=job_id)
    assert calculation(restarted, token, attempt["attemptId"]).body == response.body
    assert submit(restarted, token, attempt, data=data).body == response.body
    assert restarted.chart_bytes(url) == published
    assert attempt_row(restarted, attempt["attemptId"]) == saved
    assert course_rows(restarted, attempt["attemptId"]) == rows
    assert item_view(restarted, token, course, course.practice_link_id)["isCompleted"] is completed
    assert restarted.file_hashes() == before


def test_link_expiry_refresh_and_logout_do_not_rewrite_the_calculation(files_journey):
    world = files_journey
    token, attempt, job_id = accepted(world, CPR, data=CPR_DATA)
    assert world.work(job_id=job_id)
    response = calculation(world, token, attempt["attemptId"])
    original_url = response.data["calculation"]["chart_dataset_url"]
    published = world.chart_bytes(original_url)
    world.advance(299)
    assert world.chart_bytes(original_url) == published
    world.advance(1)
    with pytest.raises(JourneyError) as expired:
        world.chart_bytes(original_url)
    assert expired.value.code == "NOT_FOUND"
    refreshed = chart_link(world, token, attempt["attemptId"])
    assert refreshed.status == 200
    fresh_url = refreshed.data["url"]
    assert fresh_url != original_url and world.chart_bytes(fresh_url) == published
    # The committed calculation (and its original chart link) is frozen; only the envelope time moves.
    assert calculation(world, token, attempt["attemptId"]).data == response.data
    world.logout(token)
    assert world.chart_bytes(fresh_url) == published
    denied = chart_link(world, token, attempt["attemptId"])
    assert denied.status == 403 and denied.error["code"] == "SESSION_REVOKED"
    restarted = world.reopen()
    assert restarted.chart_bytes(fresh_url) == published
    restarted.advance(300)
    with pytest.raises(JourneyError) as expired:
        restarted.chart_bytes(fresh_url)
    assert expired.value.code == "NOT_FOUND"


def test_installation_keyed_auth_survives_reopen_and_other_keys_are_refused(files_journey, store):
    world = files_journey
    material = world.material
    assert (world.api.auth.environment, world.api.auth.current_key_version) == (material.environment,
                                                                                 material.key_version)
    first = world.login()
    created = start(world, first.token, CPR)
    credential = created["resumeCredential"]
    restarted = world.reopen()
    assert restarted.material == material
    assert (restarted.api.auth.environment, restarted.api.auth.current_key_version) == (material.environment,
                                                                                         material.key_version)
    # A token issued before the restart is still honoured; ending it lets another session resume.
    restarted.session(first.token)
    restarted.logout(first.token)
    # A process with the same key version but not this installation's key cannot resume the attempt.
    stranger = V2Journey(store, resume_keys={material.key_version: b"not-this-installation-resume-key"},
                         key_version=material.key_version)
    refused = stranger.reauthorize(stranger.login().token, created["attemptId"], credential, expected=404)
    assert refused["code"] == "NOT_FOUND"
    assert attempt_row(restarted, created["attemptId"])["bound_session_id"] == first.session_id
    # The reopened installation accepts the credential it issued before the restart.
    second = restarted.login()
    resumed = restarted.reauthorize(second.token, created["attemptId"], credential)
    assert resumed["attemptId"] == created["attemptId"]
    row = attempt_row(restarted, created["attemptId"])
    assert row["state"] == "created" and row["bound_session_id"] == second.session_id


def test_full_quota_preserves_committed_reads_retries_and_existing_chart(files_journey):
    world = files_journey
    token, attempt, job_id = accepted(world, CPR, data=CPR_DATA)
    assert world.work(job_id=job_id)
    response = calculation(world, token, attempt["attemptId"])
    url = response.data["calculation"]["chart_dataset_url"]
    published = world.chart_bytes(url)
    # Lower only total quota; an artifact read must retain its original bound.
    restarted = world.reopen(quota=1)
    assert calculation(restarted, token, attempt["attemptId"]).body == response.body
    assert submit(restarted, token, attempt, data=CPR_DATA).body == response.body
    assert restarted.chart_bytes(url) == published
    ref = job_row(restarted, job_id)["planned_candidate_ref"]
    old = restarted.objects.get_object(Bucket=FILES_BUCKET, Key=ref["key"])
    stream = old["Body"]
    try:
        raw = stream.read(ARTIFACT_LIMIT + 1)
    finally:
        stream.close()
    restarted.objects.put_object(Bucket=FILES_BUCKET, Key=ref["key"], Body=raw, Metadata=old["Metadata"])
    new = start(restarted, token, CPR)
    denied = submit(restarted, token, new, data=CPR_DATA)
    assert denied.status == 503 and denied.error["code"] == "TEMPORARILY_UNAVAILABLE"
    created = attempt_row(restarted, new["attemptId"])
    assert created["state"] == "created" and created.get("job_id") is None
    assert calculation(restarted, token, attempt["attemptId"]).body == response.body


def test_selected_candidate_recovers_using_reopened_real_files(files_journey, monkeypatch):
    world = files_journey
    token, attempt, job_id = accepted(world, CPR, data=CPR_DATA)
    monkeypatch.setattr(world.worker.jobs, "mark_calculation_saved", unavailable)
    assert not world.work(job_id=job_id)
    old = job_row(world, job_id)
    assert world.worker.storage.load_calculation(old["planned_candidate_ref"], call_binding(old)) is not None
    restarted = world.reopen()
    restarted.now[0] = old["next_due_at"]
    forbid_calculation(monkeypatch, restarted, "A durable file candidate was recalculated.")
    assert restarted.work(job_id=job_id)
    assert job_row(restarted, job_id)["call_id"] == old["call_id"]
    response = calculation(restarted, token, attempt["attemptId"])
    assert response.status == 200
    assert restarted.chart_bytes(response.data["calculation"]["chart_dataset_url"])
    assert attempt_row(restarted, attempt["attemptId"])["evaluation"]["goal"] == {
        "kind": "cycles", "required": 3, "observed": 3, "met": True, "status": "evaluated"}


def test_reusing_selected_chart_after_final_commit_interruption_is_idempotent(files_journey, monkeypatch):
    world = files_journey
    token, attempt, job_id = accepted(world, CPR, data=CPR_DATA)
    monkeypatch.setattr(world.worker.jobs, "finalize", unavailable)
    assert not world.work(job_id=job_id)
    old = job_row(world, job_id)
    assert old["chart_snapshot"]["kind"] == "snapshot"
    raw_base = world.worker.storage.load_input(old["input_manifest_ref"], input_binding(old)).raw_base
    published = world.body(raw_base + ".json")
    restarted = world.reopen()
    restarted.now[0] = old["next_due_at"]
    forbid_calculation(monkeypatch, restarted, "A selected chart forced recalculation.")
    assert restarted.work(job_id=job_id)
    current = job_row(restarted, job_id)
    assert current["chart_snapshot"] == old["chart_snapshot"]
    assert restarted.body(current["chart_publication"]["key"]) == published
    assert calculation(restarted, token, attempt["attemptId"]).status == 200


def test_conflicting_write_cannot_replace_committed_result_or_chart(files_journey):
    world = files_journey
    token, attempt, job_id = accepted(world, COMPRESSION, data=comp_session(60))
    assert world.work(job_id=job_id)
    job = job_row(world, job_id)
    response = calculation(world, token, attempt["attemptId"])
    rows = course_rows(world, attempt["attemptId"])
    for key in (job["final_ref"]["key"], job["chart_publication"]["key"]):
        original = world.objects.get_object(Bucket=FILES_BUCKET, Key=key)
        stream = original["Body"]
        try:
            content = stream.read(ARTIFACT_LIMIT + 1)
        finally:
            stream.close()
        with pytest.raises(ClientError) as caught:
            world.objects.put_object(Bucket=FILES_BUCKET, Key=key, Body=content + b" ", Metadata=original["Metadata"])
        assert caught.value.response["Error"]["Code"] == "LocalStorageUnavailable"
        assert world.body(key) == content
    assert calculation(world, token, attempt["attemptId"]).body == response.body
    assert course_rows(world, attempt["attemptId"]) == rows


@pytest.mark.parametrize("kind", ["final", "chart", "raw"])
def test_file_corruption_fails_closed_without_rewriting_committed_progress(files_journey, kind, monkeypatch):
    world = files_journey
    token, attempt, job_id = accepted(world, COMPRESSION, data=comp_session(60))
    assert world.work(job_id=job_id)
    saved_attempt, saved_rows = attempt_row(world, attempt["attemptId"]), course_rows(world, attempt["attemptId"])
    job = job_row(world, job_id)
    loaded = world.worker.storage.load_input(job["input_manifest_ref"], input_binding(job))
    response = calculation(world, token, attempt["attemptId"])
    url = response.data["calculation"]["chart_dataset_url"]
    key = {"final": job["final_ref"]["key"], "chart": job["chart_publication"]["key"],
           "raw": loaded.raw_base + ".bin"}[kind]
    path, original = world.corrupt_file(key)
    forbid_calculation(monkeypatch, world, "Corrupt committed data triggered recalculation.")
    if kind == "final":
        failed = calculation(world, token, attempt["attemptId"])
        assert failed.status == 503 and failed.error["code"] == "TEMPORARILY_UNAVAILABLE"
    elif kind == "chart":
        with pytest.raises(JourneyError) as denied:
            world.chart_bytes(url)
        assert denied.value.code == "NOT_FOUND"
        assert chart_link(world, token, attempt["attemptId"]).status == 503
        assert calculation(world, token, attempt["attemptId"]).body == response.body
    else:
        with pytest.raises(JourneyError) as invalid:
            world.worker.storage.load_input(job["input_manifest_ref"], input_binding(job))
        assert invalid.value.code == "TEMPORARILY_UNAVAILABLE"
        assert calculation(world, token, attempt["attemptId"]).body == response.body
    assert world.work(job_id=job_id)
    assert attempt_row(world, attempt["attemptId"]) == saved_attempt
    assert course_rows(world, attempt["attemptId"]) == saved_rows
    # A test-only exact restoration demonstrates that no automatic replacement
    # calculation or new progress was required to recover the original bytes.
    path.write_bytes(original)
    assert calculation(world, token, attempt["attemptId"]).body == response.body
    assert world.chart_bytes(url)


def test_corrupt_saved_candidate_is_not_treated_as_absent_and_recalculated(files_journey, monkeypatch):
    world = files_journey
    token, attempt, job_id = accepted(world, CPR, data=CPR_DATA)
    monkeypatch.setattr(world.worker.jobs, "mark_calculation_saved", unavailable)
    assert not world.work(job_id=job_id)
    old = job_row(world, job_id)
    path, original = world.corrupt_file(old["planned_candidate_ref"]["key"])
    monkeypatch.undo()
    forbid_calculation(monkeypatch, world, "Corrupt candidate was treated as missing input.")
    world.now[0] = old["next_due_at"]
    assert not world.work(job_id=job_id)
    blocked = job_row(world, job_id)
    assert blocked["call_id"] == old["call_id"] and blocked["planned_candidate_ref"] == old["planned_candidate_ref"]
    assert blocked.get("calculation_restarts", 0) == 0
    evidence = world.worker.course_recovery.inspect(job_id, None, blocked["fence"])
    assert (evidence.candidate_state, evidence.action) == ("unreadable", "wait_integrity")
    stored = attempt_row(world, attempt["attemptId"])
    assert stored["state"] == "outcome_unknown" and stored["evaluation"] is None
    assert course_rows(world, attempt["attemptId"]).item["completed"] is False
    path.write_bytes(original)
    monkeypatch.undo()
    world.now[0] = blocked["next_due_at"]
    assert world.work(job_id=job_id)
    assert calculation(world, token, attempt["attemptId"]).status == 200
    assert len(world.calculator.calls) == 1


def test_product_storage_does_not_import_test_storage_or_global_sdk_clients():
    import ast

    root = Path(__file__).parents[1]
    for name in ("object_storage.py", "charts.py"):
        source = (root / "local_server" / name).read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(not item.name.startswith(("tests", "integration_tests", "http_pipeline_tests", "boto3")) for item in node.names)
            elif isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith(("tests", "integration_tests", "http_pipeline_tests", "boto3"))
        assert "MemoryLegacyBindings" not in source and "FileObjects" not in source

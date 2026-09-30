"""Separate API/worker processes reopen real files and a mixed durable job queue.

Legacy jobs come from rows the removed /mock/v1 API stored
(tests/fixtures/legacy_mock_v1_rows): its accepted-but-never-processed job, its
created attempt measured now, and one more attempt seeded in the same captured
shape (tests/legacy_attempt_seeds.py). The course job uses the fixture learner,
whose USER row is seeded in the legacy shape (with ``slots``) before the runtime
opens, and who also owns one legacy attempt: after migration a legacy user can
run course work, so legacy slot finalize and course finalize meet on one USER
row and both must survive the restart.
"""

import hashlib
import json
import os
import subprocess
import sys

from tests.journey_support import HARNESS_ENVIRONMENT, JourneyStore
from tests.legacy_attempt_seeds import seed_legacy_attempt, seed_legacy_session
from tests.legacy_rows_support import seed_legacy_rows
from tests.mixed_queue_process_support import open_files
from tests.vcc_runtime_support import measurement_event, runtime, start_attempt, submit
from tests.vcc_support import load_fixture


def raw_user(client, table, principal):
    return JourneyStore(client, table).row(f"USER#{principal}", "STATE")


def submit_as(env, auth, attempt):
    env.app.calculation.submit(auth, attempt["attempt_id"], measurement_event(attempt))
    return env.app.state.get_attempt(auth, attempt["attempt_id"])["job_id"]


def test_process_restart_preserves_legacy_and_course_jobs(dynamodb_client, dynamodb_table, tmp_path, monkeypatch):
    directory = tmp_path / "owned-private-installation"
    directory.mkdir(mode=0o700)
    objects, bindings = open_files(directory)
    try:
        seeded = seed_legacy_rows(JourneyStore(dynamodb_client, dynamodb_table), objects=objects)
        learner = load_fixture("course_bundle.json")["learners"]["real"]["principal"]
        # The learner's USER row predates course work: the legacy shape with slots.
        seed_legacy_session(dynamodb_client, dynamodb_table, clock=lambda: seeded.meta["capture_clock"],
                            principal=learner)
        env = runtime(dynamodb_client, dynamodb_table, objects=objects, legacy_bindings=bindings)
        env.now[0] = seeded.meta["clock_at_end"]
        legacy_token = seeded.issue_session_token()
        legacy_auth = env.app.auth.authenticate(legacy_token)
        bundle_hash = env.app.course_service.get_course(env.auth, course_id=101, enrollment_id=501).bundle.definition_hash
        queued = env.app.state.get_attempt(legacy_auth, seeded.attempts["queued"]["attempt_id"])
        created = env.app.state.get_attempt(legacy_auth, seeded.attempts["created"]["attempt_id"])
        another = seed_legacy_attempt(dynamodb_client, dynamodb_table, legacy_auth, clock=env.clock,
                                      program_id=queued["program_id"], target=queued["target"],
                                      definition_json=queued["definition_json"], environment=HARNESS_ENVIRONMENT)
        assert env.auth.principal == learner and "slots" in raw_user(dynamodb_client, dynamodb_table, learner)
        learner_legacy = seed_legacy_attempt(dynamodb_client, dynamodb_table, env.auth, clock=env.clock,
                                             program_id=queued["program_id"], target=queued["target"],
                                             definition_json=queued["definition_json"],
                                             environment=HARNESS_ENVIRONMENT)
        attempts = [queued, created, another, learner_legacy]
        assert all(attempt.get("course_binding") is None for attempt in attempts)
        jobs = [queued["job_id"], submit_as(env, legacy_auth, created), submit_as(env, legacy_auth, another),
                submit_as(env, env.auth, learner_legacy)]
        course = start_attempt(env, 1003)
        attempts.append(course)
        jobs.append(submit(env, course))
        action, _ = env.worker.jobs.claim(jobs[1], "before-restart", 60)
        assert action == "execute"
        finalize = env.worker.jobs.finalize
        def stop_before_commit(*args, **kwargs):
            raise OSError("test-owned process boundary")
        monkeypatch.setattr(env.worker.jobs, "finalize", stop_before_commit)
        for job_id in jobs[2:]:
            assert env.worker.process(job_id) is False
        monkeypatch.setattr(env.worker.jobs, "finalize", finalize)
        assert env.worker.jobs.get_job(jobs[0])["state"] == "queued"
        assert env.worker.jobs.get_job(jobs[1])["state"] == "running"
        assert all(env.worker.jobs.get_job(job_id)["candidate_ref"] is not None for job_id in jobs[2:])
        # Neither interrupted finalize touched the shared learner USER row.
        learner_before = raw_user(dynamodb_client, dynamodb_table, learner)
        slot_key = f"{queued['program_id']}:{queued['target']}"
        assert learner_before["slots"][slot_key]["open_attempts"] == 1
        originals = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                     for path in objects.material.object_dir.glob("*.object")}
        object_directory = objects.material.object_dir
        payload = {"endpoint": dynamodb_client.meta.endpoint_url, "table": dynamodb_table,
            "directory": str(directory), "now": env.now[0]+61, "token": env.token, "legacy_token": legacy_token,
            "legacy_principal": seeded.principal,
            "attempts": [[a["attempt_id"], "legacy"] for a in attempts[:3]]
                        + [[learner_legacy["attempt_id"], "learner"], [course["attempt_id"], "learner"]],
            "jobs": jobs}
    finally:
        objects.close()
    # Credentials are not placed in argv or emitted in subprocess output.
    results = []
    for _ in range(2):
        child = subprocess.run([sys.executable, "-m", "tests.mixed_queue_process_support"],
            input=json.dumps(payload), capture_output=True, text=True, timeout=60,
            env={**os.environ, "STAGE": "test", "AWS_EC2_METADATA_DISABLED": "true"})
        assert child.returncode == 0, child.stderr
        assert legacy_token not in child.stdout and env.token not in child.stdout
        results.append(json.loads(child.stdout.splitlines()[-1]))
    first, second = results
    assert first["pid"] != second["pid"] and first["pid"] != os.getpid()
    assert first["calls"] == 2 and second["calls"] == 0
    assert first["states"] == second["states"] == ["evaluated"]*5
    assert first["course_bound"] == second["course_bound"] == [False, False, False, False, True]
    assert first["result_hashes"] == second["result_hashes"]
    assert first["bundle_hash"] == second["bundle_hash"] == bundle_hash
    assert first["course_completed"] is second["course_completed"] is True
    # Legacy finalize kept its slot bookkeeping across the restart (D103).
    assert first["legacy_slots"] == second["legacy_slots"]
    assert all(slot["open_attempts"] == 0 for slot in first["legacy_slots"].values())
    assert first["legacy_slots"]["mock-compression-only:adult"]["completed"] is True
    # On the learner's shared USER row both finalizes landed: the legacy slot is
    # completed by the learner's legacy attempt and the course item is completed.
    assert first["learner_user"] == second["learner_user"]
    learner_slot = first["learner_user"]["slots"][slot_key]
    assert learner_slot == {"completed": True, "open_attempts": 0,
                            "completed_by_attempt": learner_legacy["attempt_id"]}
    assert all(slot["open_attempts"] == 0 for slot in first["learner_user"]["slots"].values())
    # The course finalize never wrote a legacy slot: every other learner slot is
    # exactly as seeded on this slots-bearing USER row.
    assert set(first["learner_user"]["slots"]) == set(learner_before["slots"])
    for key, slot in first["learner_user"]["slots"].items():
        if key != slot_key:
            seeded_slot = learner_before["slots"][key]
            assert slot == {"completed": seeded_slot["completed"], "open_attempts": seeded_slot["open_attempts"],
                            "completed_by_attempt": seeded_slot["completed_by_attempt"]}, key
            assert slot["completed"] is False and slot["completed_by_attempt"] is None
    assert first["learner_user"]["epoch"] == learner_before["epoch"]
    assert first["learner_user"]["revision"] > learner_before["revision"]
    assert all(hashlib.sha256((object_directory / name).read_bytes()).hexdigest() == checksum
               for name, checksum in originals.items())

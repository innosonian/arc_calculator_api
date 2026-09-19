"""Separate API/worker processes reopen real files and a mixed durable job queue."""

import hashlib
import json
import os
import subprocess
import sys
import uuid

from tests.vcc_process_support import open_files
from tests.vcc_runtime_support import runtime, start_attempt, submit


def test_process_restart_preserves_legacy_and_course_jobs(dynamodb_client, dynamodb_table, tmp_path, monkeypatch):
    directory = tmp_path / "owned-private-installation"
    directory.mkdir(mode=0o700)
    objects, bindings = open_files(directory)
    try:
        env = runtime(dynamodb_client, dynamodb_table, objects=objects, legacy_bindings=bindings)
        bundle_hash = env.app.course_service.get_course(env.auth, course_id=101, enrollment_id=501).bundle.definition_hash
        attempts, jobs = [], []
        for _ in range(3):
            status, created = env.app.journey.create_attempt(env.auth, {
                "client_request_id": str(uuid.uuid4()), "catalog_version": "mock-catalog-v1",
                "program_id": "mock-compression-only", "target": "adult",
            })
            assert status == 201
            attempt = env.app.state.get_attempt(env.auth, created["attempt_id"])
            assert attempt.get("course_binding") is None
            attempts.append(attempt)
            jobs.append(submit(env, attempt))
        course = start_attempt(env, 1003)
        attempts.append(course)
        jobs.append(submit(env, course))
        action, _ = env.worker.jobs.claim(jobs[1], "before-restart", 60)
        assert action == "execute"
        finalize = env.worker.jobs.finalize
        def stop_before_commit(*args, **kwargs):
            raise OSError("test-owned process boundary")
        monkeypatch.setattr(env.worker.jobs, "finalize", stop_before_commit)
        assert env.worker.process(jobs[2]) is False
        assert env.worker.process(jobs[3]) is False
        monkeypatch.setattr(env.worker.jobs, "finalize", finalize)
        assert env.worker.jobs.get_job(jobs[0])["state"] == "queued"
        assert env.worker.jobs.get_job(jobs[1])["state"] == "running"
        assert env.worker.jobs.get_job(jobs[2])["candidate_ref"] is not None
        assert env.worker.jobs.get_job(jobs[3])["candidate_ref"] is not None
        originals = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                     for path in objects.material.object_dir.glob("*.object")}
        object_directory = objects.material.object_dir
        payload = {"endpoint": dynamodb_client.meta.endpoint_url, "table": dynamodb_table,
            "directory": str(directory), "now": env.now[0]+61, "token": env.token,
            "attempts": [a["attempt_id"] for a in attempts], "jobs": jobs}
    finally:
        objects.close()
    # Credentials are not placed in argv or emitted in subprocess output.
    results = []
    for _ in range(2):
        child = subprocess.run([sys.executable, "-m", "tests.vcc_process_support"],
            input=json.dumps(payload), capture_output=True, text=True, timeout=60,
            env={**os.environ, "STAGE": "test", "AWS_EC2_METADATA_DISABLED": "true"})
        assert child.returncode == 0, child.stderr
        results.append(json.loads(child.stdout.splitlines()[-1]))
    first, second = results
    assert first["pid"] != second["pid"] and first["pid"] != os.getpid()
    assert first["calls"] == 2 and second["calls"] == 0
    assert first["states"] == second["states"] == ["evaluated"]*4
    assert first["result_hashes"] == second["result_hashes"]
    assert first["bundle_hash"] == second["bundle_hash"] == bundle_hash
    assert first["course_completed"] is second["course_completed"] is True
    assert all(hashlib.sha256((object_directory / name).read_bytes()).hexdigest() == checksum
               for name, checksum in originals.items())

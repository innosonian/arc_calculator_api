"""Only the log destination fails; real DB, files and scoring remain in use."""

import json
import threading

import pytest

from integration_tests.test_local_filesystem_journey import files_journey, accepted, result, stored_attempt, submit
from services.operational_logs import AsyncLogRecorder
from tests._synth import comp_session


@pytest.mark.parametrize("blocked", [False, True], ids=("write-error", "write-blocked-and-full"))
def test_log_failure_cannot_veto_real_calculation_or_progress(files_journey, blocked):
    world = files_journey
    entered, release = threading.Event(), threading.Event()
    class Unavailable:
        def write(self, raw):
            entered.set()
            if blocked:
                assert release.wait(10)
            raise OSError("PRIVATE-LOG-STORE-ERROR")
        def close(self): pass
    log = AsyncLogRecorder(Unavailable, role="api", capacity=2, warning=lambda _: None)
    world.service.operations = world.worker.operations = log
    try:
        token, attempt, job_id, data = accepted(world, program="mock-compression-only", data=comp_session(60))
        assert entered.wait(2)
        assert world.worker.process(job_id) is True
        reply = result(world, token, attempt)
        assert reply["statusCode"] == 200
        saved = stored_attempt(world, token, attempt)
        assert saved["state"] == "evaluated" and saved["evaluation"]["program_completed"] is True
        assert saved["progress_application"]["applied"] is True
        assert world.jobs.get_job(job_id)["state"] == "done"
        assert submit(world, token, attempt, data=data) == reply
        assert result(world, token, attempt) == reply
        assert json.loads(reply["body"])["submit_arc"]["status"] == "disabled"
        if blocked:
            assert log.status()["dropped"] > 0
            assert log.close(timeout=0.01) is False
    finally:
        release.set(); log.close(timeout=2)
    assert log.status()["unconfirmed"] > 0

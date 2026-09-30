"""Only the log destination fails; real DB, files and scoring remain in use.

The API and Worker built by the public factories share one asynchronous
recorder whose writer always fails (course attempt through /api/v2).
"""

import threading

import pytest

from integration_tests.worker_journey_support import (  # noqa: F401 (store fixture)
    DUMMY_SUBMISSION, accepted, attempt_row, calculation, files_journey_at, item_view, job_row, store, submit,
)
from services.operational_logs import AsyncLogRecorder
from tests._synth import comp_session
from tests.journey_support import dummy_course


@pytest.mark.parametrize("blocked", [False, True], ids=("write-error", "write-blocked-and-full"))
def test_log_failure_cannot_veto_real_calculation_or_progress(store, tmp_path, blocked):
    entered, release = threading.Event(), threading.Event()

    class Unavailable:
        def write(self, raw):
            entered.set()
            if blocked:
                assert release.wait(10)
            raise OSError("PRIVATE-LOG-STORE-ERROR")

        def close(self):
            pass
    log = AsyncLogRecorder(Unavailable, role="api", capacity=2, warning=lambda _: None)
    course = dummy_course("mock-compression-only", "adult")
    data = comp_session(60)
    try:
        with files_journey_at(store, tmp_path.resolve() / "private-installation", operations=log) as world:
            assert world.api.operations is log and world.worker.operations is log
            token, attempt, job_id = accepted(world, course, data=data)
            assert entered.wait(2)
            assert world.work(job_id=job_id) is True
            reply = calculation(world, token, attempt["attemptId"])
            assert reply.status == 200
            saved = attempt_row(world, attempt["attemptId"])
            assert saved["state"] == "evaluated" and saved["evaluation"]["program_completed"] is True
            assert saved["progress_application"]["applied"] is True
            assert job_row(world, job_id)["state"] == "done"
            assert item_view(world, token, course, course.practice_link_id)["isCompleted"] is True
            assert submit(world, token, attempt, data=data).body == reply.body
            assert calculation(world, token, attempt["attemptId"]).body == reply.body
            assert reply.data["submit_arc"] == DUMMY_SUBMISSION
            if blocked:
                assert log.status()["dropped"] > 0
                assert log.close(timeout=0.01) is False
    finally:
        release.set(); log.close(timeout=2)
    assert log.status()["unconfirmed"] > 0

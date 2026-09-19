"""V12/V19 unit checks for CourseRecovery.inspect. Reader is a fake; no DB writes."""

from copy import deepcopy
import hashlib

import pytest

from mock_journey.course_contracts import CONTRACT_VERSION, RecoveryEvidence
from mock_journey.course_errors import CourseError
from mock_journey.course_recovery import CourseRecovery
from mock_journey.course_submission import HOOK_REQUESTS, CourseCompletionPlan
from mock_journey.typed import parse_json, json_bytes
from tests.test_vcc_submission import LATER, _evaluation, _rows, _template


JOB = "job-1"
OWNER = "worker-1"
FENCE = 4
ATTEMPT = "50000000-0000-4000-8000-000000000001"
EPOCH = "80000000-0000-4000-8000-000000000001"
INPUT_DIGEST = "a" * 64
LATER_EPOCH = LATER
BINDING = _template(1005).binding
DEFINITION_BODY = _template(1005).existing_template_json
DEFINITION = parse_json(DEFINITION_BODY)
PRINCIPAL = "fixture-learner@example.test"


class RecordingReader:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.calls = []
        self.writes = []

    def load_consistent(self, job_id, owner, fence):
        self.calls.append((job_id, owner, fence, self.snapshot.get("attempt", {}).get("epoch")))
        return self.snapshot

    def write(self, *_args, **_kwargs):
        self.writes.append((_args, _kwargs))
        raise AssertionError("recovery inspect must not write")


def _job(**overrides):
    row = {
        "job_id": JOB, "attempt_id": ATTEMPT, "revision": 2, "state": "running",
        "call_phase": "started", "call_id": "call-1", "fence": FENCE, "owner": OWNER,
        "lease_until": 100, "input_digest": INPUT_DIGEST, "epoch": EPOCH,
        "candidate_ref": None, "chart_snapshot": {"kind": "unset", "revision": 0},
        "final_ref": None, "error_code": None, "principal": PRINCIPAL,
        "definition_sha256": hashlib.sha256(DEFINITION_BODY).hexdigest(),
        "adapter_version": DEFINITION["adapter_version"], "projection_version": DEFINITION["projection_version"],
        "planned_candidate_ref": {"bucket": "b", "key": "planned"},
    }
    row.update(overrides)
    return row


def _attempt(**overrides):
    row = {
        "attempt_id": ATTEMPT, "revision": 1, "state": "processing", "epoch": EPOCH,
        "input_digest": INPUT_DIGEST, "result_ref": None, "evaluation": None, "active_counted": False,
        "job_id": JOB, "principal": PRINCIPAL, "course_binding": BINDING,
        "definition_json": DEFINITION_BODY.decode(),
    }
    row.update(overrides)
    return row


def _snap(job=None, attempt=None, *, lookup=None, adapter=None, terminal=None, now=50, user_epoch=EPOCH):
    return {
        "job": job if job is not None else _job(),
        "attempt": attempt if attempt is not None else _attempt(),
        "user": {"epoch": user_epoch, "revision": 9, "principal": PRINCIPAL},
        "head": {"epoch": EPOCH, "revision": 1, "scope_key": BINDING.scope_key, "definition_hash": BINDING.definition_hash},
        "final": {"epoch": EPOCH, "phase": "recovery_required", "revision": 1, "scope_key": BINDING.scope_key, "active_attempt_id": ATTEMPT},
        "candidate_lookup": lookup if lookup is not None else {
            "status": "ok", "committed_present": False, "uncommitted_present": False,
            "hash_matches": None, "bytes_readable": False,
        },
        "adapter": {"present": True, "can_calculate": True, "can_verify": True,
                    "adapter_version": DEFINITION["adapter_version"], **(adapter or {})},
        "input_lookup": {"status": "ok", "input_digest": INPUT_DIGEST, "hash_matches": True, "definition_matches": True},
        "terminal": terminal if terminal is not None else {
            "proven_local_error": False, "response_assembly_failure": False, "file_save_failure": False,
        },
        "now": now,
    }


def _inspect(snapshot, owner=OWNER, fence=FENCE):
    reader = RecordingReader(snapshot)
    evidence = CourseRecovery(reader=reader).inspect(JOB, owner, fence)
    assert reader.writes == []
    assert reader.calls
    return evidence, reader


class TestV12RecoveryBranches:
    def test_valid_candidate_resumes_without_recalculate(self):
        snap = _snap(
            _job(call_phase="candidate_saved", candidate_ref={"bucket": "b", "key": "k"}),
            lookup={
                "status": "ok", "committed_present": True, "uncommitted_present": False,
                "hash_matches": True, "bytes_readable": True, "response_valid": True,
            },
        )
        evidence, _ = _inspect(snap)
        assert type(evidence) is RecoveryEvidence
        assert evidence.candidate_state == "valid"
        assert evidence.action == "resume_candidate"
        assert evidence.code is None
        assert evidence.epoch == EPOCH

    def test_retry_local_call_predicates(self):
        evidence, _ = _inspect(_snap())
        assert evidence.candidate_state == "absent_uncommitted"
        assert evidence.action == "retry_local_call"
        leftover = _snap(lookup={
            "status": "ok", "committed_present": False, "uncommitted_present": True,
            "hash_matches": None, "bytes_readable": False,
        })
        blocked, _ = _inspect(leftover)
        assert blocked.action != "retry_local_call"
        charted = _snap(_job(chart_snapshot={"kind": "snapshot", "revision": 1}))
        assert _inspect(charted)[0].action != "retry_local_call"

    def test_io_error_is_unreadable_not_absent(self):
        evidence, _ = _inspect(_snap(lookup={"status": "io_error"}))
        assert evidence.candidate_state == "unreadable"
        assert evidence.action == "wait_integrity"
        lost = _snap(
            _job(call_phase="candidate_saved", candidate_ref={"bucket": "b", "key": "k"}),
            lookup={
                "status": "ok", "committed_present": False, "uncommitted_present": False,
                "hash_matches": None, "bytes_readable": False,
            },
        )
        missing, _ = _inspect(lost)
        assert missing.candidate_state == "missing_committed"
        assert missing.action == "wait_integrity"
        mismatched = _snap(
            _job(call_phase="candidate_saved", candidate_ref={"bucket": "b", "key": "k"}),
            lookup={
                "status": "ok", "committed_present": True, "uncommitted_present": False,
                "hash_matches": False, "bytes_readable": True,
            },
        )
        invalid, _ = _inspect(mismatched)
        assert invalid.candidate_state == "invalid"
        assert invalid.action == "wait_integrity"

    def test_wait_configuration_when_adapter_cannot_calculate(self):
        evidence, _ = _inspect(_snap(adapter={"present": True, "can_calculate": False}))
        assert evidence.action == "wait_configuration"
        assert evidence.code == "TEMPORARILY_UNAVAILABLE"
        absent, _ = _inspect(_snap(adapter={"present": False, "can_calculate": False}))
        assert absent.action == "wait_configuration"

    def test_close_terminal_is_strict(self):
        terminal = {
            "proven_local_error": True, "response_assembly_failure": False, "file_save_failure": False,
        }
        allowed, _ = _inspect(_snap(
            _job(state="failed", call_phase="started", error_code="CALCULATION_FAILED", lease_until=100),
            _attempt(active_counted=False),
            terminal=terminal, now=50,
        ))
        assert allowed.action == "close_terminal"
        assert allowed.code == "CALCULATION_FAILED"
        by_code_only, _ = _inspect(_snap(
            _job(state="failed", error_code="CALCULATION_FAILED"),
            terminal={"proven_local_error": False},
        ))
        assert by_code_only.action != "close_terminal"
        expired, _ = _inspect(_snap(
            _job(state="failed", error_code="CALCULATION_FAILED", lease_until=10, owner=OWNER, fence=FENCE),
            terminal=terminal, now=50,
        ))
        assert expired.action != "close_terminal"
        counted, _ = _inspect(_snap(
            _job(state="failed", error_code="CALCULATION_FAILED"),
            _attempt(active_counted=False),
            terminal={"proven_local_error": False},
            now=50,
        ))
        assert counted.action != "close_terminal"
        unknown, _ = _inspect(_snap(
            _job(state="outcome_unknown", error_code="CALCULATION_OUTCOME_UNKNOWN"),
            terminal=terminal,
        ))
        assert unknown.action == "wait_integrity"
        assert unknown.action != "close_terminal"
        assembly, _ = _inspect(_snap(
            _job(state="failed", error_code="CALCULATION_FAILED"),
            terminal={**terminal, "response_assembly_failure": True},
        ))
        assert assembly.action != "close_terminal"
        with pytest.raises(CourseError) as error:
            _inspect(_snap(
                _job(state="failed", error_code="CALCULATION_FAILED", owner="other", fence=1),
                terminal=terminal,
            ), owner=OWNER, fence=FENCE)
        assert error.value.code == "INVALID_STATE"


    def test_lease_active_count_and_thirty_seconds_do_not_free_final(self):
        snap = _snap(
            _job(state="running", lease_until=0),
            _attempt(active_counted=False),
            now=30,
        )
        snap["app_wait_seconds"] = 30
        evidence, reader = _inspect(snap)
        assert evidence.action != "close_terminal"
        assert reader.writes == []
        assert snap["final"]["phase"] == "recovery_required"

    def test_previous_epoch_inspect_does_not_write_current_course(self):
        snap = _snap(
            _job(epoch=EPOCH),
            _attempt(epoch=EPOCH),
            user_epoch=LATER_EPOCH,
        )
        evidence, reader = _inspect(snap)
        assert evidence.epoch == EPOCH
        assert evidence.epoch != LATER_EPOCH
        assert reader.writes == []
        assert reader.calls[0][3] == EPOCH
        template = _template(1005)
        job, attempt, user, rows = _rows(template, user_epoch=LATER_EPOCH)
        plan = CourseCompletionPlan().build(job, attempt, user, rows, _evaluation(
            kind="ventilations", required=8, observed=8, met=True, completed=True,
        ))
        kinds = [item["kind"] for item in parse_json(plan.actions_json)["writes"]]
        assert "COURSE_HEAD" not in kinds
        assert "COURSE_ITEM" not in kinds
        assert "COURSE_FINAL" not in kinds
        assert CONTRACT_VERSION == "vcc-internal-v1"

    def test_io_exception_is_not_absent(self):
        class Broken:
            def load_consistent(self, job_id, owner, fence):
                raise OSError("disk")

        try:
            CourseRecovery(reader=Broken()).inspect(JOB, OWNER, FENCE)
        except CourseError as error:
            assert error.code == "TEMPORARILY_UNAVAILABLE"
        else:
            raise AssertionError("expected CourseError")

    def test_hook_requests_cover_seal_paths(self):
        assert "close_course_terminal" in HOOK_REQUESTS
        assert "reopen_course_recovery" in HOOK_REQUESTS
        assert "terminal_seal" in HOOK_REQUESTS
        assert "claim" in HOOK_REQUESTS
        assert "begin_calculation" in HOOK_REQUESTS
        assert "finalize" in HOOK_REQUESTS
        assert "V12" in HOOK_REQUESTS
        assert "V19" in HOOK_REQUESTS


class TestRecoverySnapshotIntegrity:
    @staticmethod
    def valid_candidate():
        return _snap(_job(call_phase="candidate_saved", candidate_ref={"bucket": "b", "key": "k"}), lookup={
            "status": "ok", "committed_present": True, "uncommitted_present": False,
            "hash_matches": True, "bytes_readable": True, "response_valid": True,
        })

    def test_valid_candidate_requires_exact_reader_and_verified_input(self):
        for changed, expected in (
            ({"adapter": {"present": False}}, "wait_configuration"),
            ({"adapter": {"present": True, "can_verify": False}}, "wait_configuration"),
            ({"input_lookup": {"status": "io_error"}}, "wait_integrity"),
        ):
            snap = self.valid_candidate()
            snap.update(changed)
            assert _inspect(snap)[0].action == expected
        reader_only = self.valid_candidate()
        reader_only["adapter"]["can_calculate"] = False
        assert _inspect(reader_only)[0].action == "resume_candidate"

    def test_uncommitted_but_verified_candidate_resumes_without_recalculation(self):
        snap = self.valid_candidate()
        snap["job"]["call_phase"] = "started"
        snap["job"]["candidate_ref"] = None
        snap["candidate_lookup"].update(committed_present=False, uncommitted_present=True)
        evidence, _ = _inspect(snap)
        assert evidence.candidate_state == "valid"
        assert evidence.action == "resume_candidate"

    @pytest.mark.parametrize("mutation", [
        lambda s: s["job"].update(terminal_seal={"job_id": JOB}),
        lambda s: s["final"].update(phase="passed", active_attempt_id=None),
        lambda s: s["final"].update(active_attempt_id="51000000-0000-4000-8000-000000000001"),
        lambda s: s["attempt"].update(job_id="other-job"),
        lambda s: s["attempt"].update(input_digest="b" * 64),
        lambda s: s["job"].update(principal="other-student"),
        lambda s: s["job"].update(definition_sha256="b" * 64),
        lambda s: s["head"].update(epoch=LATER_EPOCH),
    ])
    def test_incoherent_or_sealed_snapshot_cannot_produce_recovery_authority(self, mutation):
        snap = self.valid_candidate()
        mutation(snap)
        with pytest.raises(CourseError):
            _inspect(snap)

    def test_snapshot_context_is_owned_and_includes_all_transaction_dependencies(self):
        snap = self.valid_candidate()
        evidence, _ = _inspect(snap)
        context = parse_json(evidence.snapshot_json)
        assert context["user_revision"] == 9
        assert context["user_epoch"] == EPOCH
        assert context["head_revision"] == 1
        assert context["head_definition_hash"] == BINDING.definition_hash
        assert context["final_revision"] == 1
        assert context["final_active_attempt_id"] == ATTEMPT
        assert context["definition_sha256"] == hashlib.sha256(DEFINITION_BODY).hexdigest()
        assert context["candidate_ref"] == {"bucket": "b", "key": "k"}
        snap["job"]["candidate_ref"]["key"] = "changed-after-inspect"
        assert parse_json(evidence.snapshot_json)["candidate_ref"]["key"] == "k"

    def test_failed_job_candidate_inspection_accepts_no_owner_without_forging_lease(self):
        snap = self.valid_candidate()
        snap["job"].update(state="failed", owner=None, lease_until=0)
        evidence, _ = _inspect(snap, owner=None)
        assert evidence.action == "resume_candidate"
        snap["candidate_lookup"].update(committed_present=False, bytes_readable=False, hash_matches=None)
        snap["job"].update(call_phase="started", candidate_ref=None)
        assert _inspect(snap, owner=None)[0].action != "retry_local_call"

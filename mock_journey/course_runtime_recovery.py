"""Read durable course recovery evidence through the existing job/artifact adapters."""

import hashlib
import json

from mock_journey.contracts import VerifiedCalculation
from mock_journey.errors import JourneyError
from mock_journey.typed import canonical_bytes


class CourseRecoveryReader:
    def __init__(self, jobs, storage, adapters):
        self.jobs, self.storage, self.adapters = jobs, storage, adapters

    def load_consistent(self, job_id, owner, fence):
        snap = self.jobs.recovery_snapshot(job_id)
        job, attempt = snap["job"], snap["attempt"]
        binding = {key: job[key] for key in (
            "attempt_id", "epoch", "input_digest", "adapter_version", "projection_version",
        )}
        snap["input_lookup"] = {"status": "unreadable"}
        snap["candidate_lookup"] = {"status": "unreadable"}
        snap["adapter"] = {"present": False, "can_calculate": False, "can_verify": False}
        snap["terminal"] = {"proven_local_error": False, "response_assembly_failure": False,
                            "file_save_failure": False}
        adapter, loaded = None, None
        try:
            adapter = self.adapters.resolve(job["adapter_version"], job["projection_version"])
            snap["adapter"] = {
                "present": True, "can_calculate": getattr(adapter, "can_calculate", True) is True,
                "can_verify": callable(getattr(adapter, "validate_response", None)),
                "adapter_version": job["adapter_version"], "projection_version": job["projection_version"],
            }
        except JourneyError:
            pass  # Missing exact adapter is configuration waiting, not a new version choice.
        try:
            loaded = self.storage.load_input(job["input_manifest_ref"], binding)
            definition = json.loads(attempt["definition_json"])
            expected = {key: definition[key] for key in loaded.projected.payload["definition"]}
            snap["input_lookup"] = {
                "status": "ok", "input_digest": job["input_digest"], "hash_matches": True,
                "definition_matches": canonical_bytes(expected) == canonical_bytes(loaded.projected.payload["definition"]),
            }
        except (JourneyError, ValueError, KeyError, TypeError):
            pass  # An unreadable committed input is never absence or terminal evidence.
        if job.get("call_id") is not None and job.get("planned_candidate_ref") is not None:
            call_binding = {**binding, "job_id": job_id, "call_id": job["call_id"]}
            try:
                raw = self.storage.load_calculation(job["planned_candidate_ref"], call_binding)
                committed = job.get("candidate_ref")
                lookup = {"status": "ok", "committed_present": False, "uncommitted_present": False,
                          "bytes_readable": raw is not None, "hash_matches": False, "response_valid": False}
                if raw is not None:
                    lookup["committed_present" if committed is not None else "uncommitted_present"] = True
                    lookup["hash_matches"] = committed is None or (
                        committed.get("sha256") == hashlib.sha256(raw).hexdigest()
                        and committed.get("size") == len(raw)
                        and all(committed.get(k) == v for k, v in job["planned_candidate_ref"].items()))
                    if adapter is not None and loaded is not None and lookup["hash_matches"]:
                        verified = adapter.validate_response(raw, loaded.projected, call_binding)
                        lookup["response_valid"] = isinstance(verified, VerifiedCalculation)
                snap["candidate_lookup"] = lookup
            except (JourneyError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
                snap["candidate_lookup"] = {"status": "unreadable"}
        proof = job.get("local_error_proof")
        if type(proof) is dict:
            snap["terminal"]["proven_local_error"] = proof == {
                "call_id": job.get("call_id"), "fence": job["fence"], "input_digest": job["input_digest"],
                "code": job.get("error_code"), "stage": "calculate",
            }
        return snap

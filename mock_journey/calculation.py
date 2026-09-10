"""Authenticated durable input acceptance and stored-result retrieval."""

import json
import time
import uuid
from types import MappingProxyType

from mock_journey.errors import JourneyError
from services.operational_logs import record_event, bind_identifiers, write_diagnostic
from mock_journey.worker import call_binding


class CalculationService:
    def __init__(self, state, jobs, storage, schemas, *, payload_limit, clock=time.time):
        if type(payload_limit) is not int or payload_limit <= 0:
            raise ValueError("A verified ingress payload limit is required.")
        if type(schemas) is not dict or any(key != value.version for key, value in schemas.items()):
            raise ValueError("A versioned projection schema registry is required.")
        self.state, self.jobs, self.storage = state, jobs, storage
        self.schemas = MappingProxyType(dict(schemas))
        self.payload_limit, self.clock = payload_limit, clock

    def submit(self, auth, attempt_id, event):
        # Called after the outer handler's token validation. Recheck binding
        # before the potentially expensive parser and again in the accept CAS.
        attempt = self.state.get_attempt(auth, attempt_id)
        bind_identifiers(attempt_id=attempt_id, progress_epoch=attempt["epoch"])
        from mock_journey.legacy_bridge import MeasurementInputError, parse_measurement

        body = event.get("body")
        if type(body) is not str:
            raise MeasurementInputError()
        try:
            if len(body.encode("utf-8")) > self.payload_limit:
                raise JourneyError("PAYLOAD_TOO_LARGE")
        except UnicodeError:
            raise MeasurementInputError() from None
        from mock_journey.projection import project_input, typed_identity

        definition = json.loads(attempt["definition_json"])
        schema = self.schemas.get(definition["projection_version"])
        if schema is None:
            raise JourneyError("TEMPORARILY_UNAVAILABLE")
        parse_started = time.monotonic()
        write_diagnostic("info", "request_start", {"path": "/cpr-analysis"})
        projected = project_input(parse_measurement(event), definition, schema)
        write_diagnostic("info", "parse_complete", {
            "cpr_bytes": len(projected.cpr_bytes), "aed_bytes": len(projected.aed_bytes),
            "parse_ms": max(0, int((time.monotonic() - parse_started) * 1000)),
        })
        digest = typed_identity(projected)
        if attempt.get("input_digest") is not None:
            if attempt["input_digest"] != digest:
                raise JourneyError("ATTEMPT_INPUT_CONFLICT")
            record_event("calculation_replayed", job_id=attempt.get("job_id"), replayed=True)
            return self.result(auth, attempt_id)
        if attempt["state"] != "created":
            raise JourneyError("INVALID_STATE")
        definition = projected.payload["definition"]
        binding = {
            "attempt_id": attempt_id, "epoch": attempt["epoch"], "input_digest": digest,
            "adapter_version": definition["adapter_version"], "projection_version": definition["projection_version"],
        }
        saved = self.storage.save_input(projected, binding)
        if saved["input_digest"] != digest:
            raise JourneyError("STORED_INPUT_INVALID")
        proposed_job_id = str(uuid.uuid4())
        accepted = self.jobs.accept_input(
            auth, attempt_id, digest, saved["manifest_ref"], job_id=proposed_job_id,
            adapter_version=definition["adapter_version"], next_due_at=int(self.clock()),
        )
        try:
            record_event("calculation_accepted" if accepted["job_id"] == proposed_job_id else "calculation_replayed",
                         job_id=accepted["job_id"], replayed=accepted["job_id"] != proposed_job_id)
        except Exception:
            pass  # An accepted job remains accepted if logging metadata fails.
        # Outbox, not an in-process background thread, owns execution. A result
        # that already committed can be returned now; otherwise acknowledge
        # early, within the configured gateway budget (wait_expired=false).
        return self.result(auth, attempt_id)

    def result(self, auth, attempt_id):
        attempt = self.state.get_attempt(auth, attempt_id)
        state = attempt["state"]
        if state in ("created", "cancelled"):
            raise JourneyError("INVALID_STATE")
        if state == "outcome_unknown":
            raise JourneyError("CALCULATION_OUTCOME_UNKNOWN")
        if state == "failed":
            code = attempt.get("error_code")
            raise JourneyError(code if code in (
                "STORED_INPUT_INVALID", "CALCULATOR_CONTRACT_MISMATCH", "CALCULATION_FAILED",
            ) else "CALCULATION_FAILED")
        if state == "evaluated":
            job = self.jobs.get_job(attempt["job_id"])
            if not job or job["state"] != "done" or job["attempt_id"] != attempt_id:
                raise JourneyError("STORED_INPUT_INVALID")
            return 200, self.storage.read_final(job["final_ref"], call_binding(job), job["chart_publication"])
        if state not in ("queued", "processing"):
            raise JourneyError("STORED_INPUT_INVALID")
        return 202, {
            "attempt_id": attempt_id, "state": state, "wait_expired": False,
            "status_path": f"/mock/v1/attempts/{attempt_id}",
        }

    def chart_link(self, auth, attempt_id):
        attempt = self.state.get_attempt(auth, attempt_id)
        if attempt["state"] != "evaluated":
            raise JourneyError("INVALID_STATE")
        job = self.jobs.get_job(attempt["job_id"])
        if not job or job["state"] != "done" or job["attempt_id"] != attempt_id:
            raise JourneyError("STORED_INPUT_INVALID")
        publication = job["chart_publication"]
        url = self.storage.sign_chart(publication)
        from datetime import datetime, timezone
        expires_at = (datetime.fromtimestamp(int(self.clock()) + 300, timezone.utc)
                      .isoformat().replace("+00:00", "Z")) if url is not None else None
        return {"attempt_id": attempt_id, "chart_dataset_url": url, "expires_at": expires_at}

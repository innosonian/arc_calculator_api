"""Outbox delivery and independent due-job reconciliation, with references only."""

from mock_journey.bootstrap import configure_imports

configure_imports()

import json
import time
import uuid

from mock_journey.errors import JourneyError
from services.operational_logs import record_event


def _has_processing_time(relay, context):
    reserve = getattr(relay, "processing_reserve_ms", None)
    if reserve is None:
        return True
    from mock_journey.aws_logs import remaining_ms
    return remaining_ms(context) > reserve


class QueueSender:
    def __init__(self, client, queue_url):
        if type(queue_url) is not str or not queue_url.startswith("https://"):
            raise ValueError("A verified queue destination is required.")
        self.client, self.queue_url = client, queue_url

    def send(self, job_id):
        # Destination is an operator binding, never an app request field.
        result = self.client.send_message(QueueUrl=self.queue_url, MessageBody=json.dumps({"job_id": job_id}))
        if type(result.get("MessageId")) is not str or not result["MessageId"]:
            raise JourneyError("TEMPORARILY_UNAVAILABLE")


class OutboxRelay:
    def __init__(self, jobs, sender, *, lease_seconds, retry_seconds, page_size, max_pages,
                 clock=time.time, progress=None):
        if any(type(value) is not int or value <= 0 for value in (lease_seconds, retry_seconds, page_size, max_pages)):
            raise ValueError("Verified relay operating limits are required.")
        self.jobs, self.sender, self.clock = jobs, sender, clock
        self.lease_seconds, self.retry_seconds = lease_seconds, retry_seconds
        self.page_size, self.max_pages = page_size, max_pages
        # Explicit legacy/local construction remains available. AWS assembly
        # supplies the required, initialized persistent progress repository.
        self.progress = progress

    def dispatch(self, job_id):
        from mock_journey.jobs import JobLeaseLost
        from mock_journey.relay_progress import RelayProgressLost, RelayTimeStopped
        owner = str(uuid.uuid4())
        action, outbox = self.jobs.claim_outbox(job_id, owner, self.lease_seconds)
        if action == "done":
            return True
        if action == "busy":
            return False
        try:
            self.sender.send(job_id)
            # A crash here can duplicate the wake. The worker's current lease,
            # execution fence and saved candidate govern recovery and commit.
            self.jobs.mark_outbox_sent(job_id, owner, outbox["fence"])
            return True
        except (RelayTimeStopped, RelayProgressLost):
            raise
        except Exception:
            try:
                self.jobs.release_outbox(job_id, owner, outbox["fence"],
                                         next_due_at=int(self.clock()) + self.retry_seconds,
                                         error_code="TEMPORARILY_UNAVAILABLE")
            except (JourneyError, JobLeaseLost):
                pass
            return False

    def reconcile(self, *, context=None):
        if self.progress is not None:
            return self._reconcile_checkpoint(context)
        counts = {"outbox_wakes": 0, "job_wakes": 0, "failures": 0}
        scan_exhausted = False
        # A sent outbox or a queue DLQ must not hide a non-terminal Job.
        try:
            for getter, key in ((self.jobs.due_outbox, "outbox_wakes"), (self.jobs.due_jobs, "job_wakes")):
                cursor = None
                for _ in range(self.max_pages):
                    if not _has_processing_time(self, context):
                        scan_exhausted = True
                        return counts
                    rows, cursor = getter(limit=self.page_size, cursor=cursor)
                    for row in rows:
                        if not _has_processing_time(self, context):
                            scan_exhausted = True
                            return counts
                        try:
                            if key == "outbox_wakes":
                                success = self.dispatch(row["job_id"])
                            else:
                                self.sender.send(row["job_id"])
                                success = True
                            counts[key if success else "failures"] += 1
                        except Exception:
                            counts["failures"] += 1
                    if cursor is None:
                        break
                if cursor is not None:
                    # Observe incomplete scans; do not claim fairness or change
                    # a Job's runnable time merely because its wake was sent.
                    scan_exhausted = True
            return counts
        except Exception:
            counts["failures"] += 1
            raise
        finally:
            record_event("relay_reconciled", **counts, scan_exhausted=scan_exhausted)

    def _reconcile_checkpoint(self, context):
        from mock_journey.aws_logs import remaining_ms
        from mock_journey.relay_progress import (
            RelayProgressLost, RelayTimeStopped, relay_call_guard, validate_cursor,
        )

        counts = {"outbox_wakes": 0, "job_wakes": 0, "failures": 0}
        steps = {"OUTBOX": 0, "JOB": 0}
        completed = set()
        progress_busy, scan_exhausted = False, True
        snapshot = None
        stopped = None
        budget = getattr(self, "relay_budget", None)

        def has_time(cost_ms=0):
            if budget is None:
                return _has_processing_time(self, context)
            return remaining_ms(context) > budget.reserve_ms + cost_ms

        def check_call():
            nonlocal stopped
            if stopped is not None:
                raise stopped
            if not has_time(budget.call_ms if budget else 0):
                stopped = RelayTimeStopped()
                raise stopped
            if snapshot is not None and snapshot["lease_until"] <= self.progress.now():
                stopped = RelayProgressLost()
                raise stopped

        try:
            # Acquisition is bounded by the existing explicit conflict budget.
            if not has_time(budget.acquire_ms + budget.step_ms if budget else 0):
                return counts
            with relay_call_guard(check_call):
                snapshot = self.progress.acquire(lease_seconds=self.lease_seconds)
                if snapshot is None:
                    progress_busy = True
                    return counts
                cap = self.page_size * self.max_pages
                while True:
                    kind = snapshot["next_kind"]
                    # A completed kind is not repeatedly restarted in this
                    # invocation. The persisted alternate kind resumes later.
                    if kind in completed or steps[kind] >= cap:
                        break
                    if not has_time(budget.step_ms if budget else 0):
                        break
                    check_call()
                    if snapshot["scans"][kind]["cutoff"] is None:
                        snapshot = self.progress.begin_pass(snapshot, kind, lease_seconds=self.lease_seconds)
                    elif budget and ((snapshot["lease_until"] - self.progress.now()) * 1000
                                     <= budget.step_ms + budget.reserve_ms + 1000):
                        snapshot = self.progress.renew(snapshot, lease_seconds=self.lease_seconds)
                    scan = snapshot["scans"][kind]
                    check_call()
                    raw, cursor = self.jobs.due_step(kind, cutoff=scan["cutoff"], cursor=scan["cursor"])
                    raw = validate_cursor(raw, kind, scan["cutoff"])
                    cursor = validate_cursor(cursor, kind, scan["cutoff"])
                    steps[kind] += 1
                    # Query returns a raw reference only. Time refusal here is
                    # not an item failure: its actual LEK must remain unsaved.
                    check_call()
                    if raw is not None:
                        try:
                            current = self.jobs.due_current(kind, raw, cutoff=scan["cutoff"])
                            check_call()
                            if current is not None:
                                if kind == "OUTBOX":
                                    success = self.dispatch(current["job_id"])
                                else:
                                    self.sender.send(current["job_id"])
                                    success = True
                                key = "outbox_wakes" if kind == "OUTBOX" else "job_wakes"
                                counts[key if success else "failures"] += 1
                        except (RelayTimeStopped, RelayProgressLost):
                            raise
                        except Exception:
                            # The raw key was validated. A returned read/send
                            # failure is considered this pass, and retried on a
                            # later pass without rewriting the Job's due time.
                            counts["failures"] += 1
                    check_call()
                    snapshot = self.progress.advance(snapshot, kind, cursor, lease_seconds=self.lease_seconds)
                    if cursor is None:
                        completed.add(kind)
                    if len(completed) == 2:
                        scan_exhausted = False
                        break
                # Time-reserved returns release only if another SDK call fits.
                # Failed/unknown CAS and expired leases skip this path entirely.
                if has_time(budget.call_ms if budget else 0):
                    check_call()
                    snapshot = self.progress.release(snapshot)
            return counts
        except RelayTimeStopped:
            # The durable pre-query position remains valid; an already returned
            # send may be repeated. No further SDK call follows a sticky stop.
            return counts
        except Exception:
            counts["failures"] += 1
            raise
        finally:
            continuation = snapshot is not None and any(
                scan["cutoff"] is not None for scan in snapshot["scans"].values())
            record_event("relay_reconciled", **counts, scan_exhausted=scan_exhausted,
                         progress_busy=progress_busy, continuation=continuation,
                         passes_completed=len(completed), query_steps=sum(steps.values()))


def handle_stream(event, context, relay):
    records = event.get("Records") if type(event) is dict else None
    if type(records) is not list:
        raise ValueError("Invalid stream envelope.")
    failures = []
    for record in records:
        # Only keys/sequence metadata are read. Full NewImage may contain
        # unrelated session/user data and must never be logged or enqueued.
        if type(record) is not dict or type(record.get("dynamodb")) is not dict:
            raise ValueError("Invalid stream envelope.")
        sequence = record["dynamodb"].get("SequenceNumber")
        if type(sequence) is not str or not sequence:
            raise ValueError("Invalid stream envelope.")
        try:
            keys = record["dynamodb"]["Keys"]
            pk, sk = keys["PK"]["S"], keys["SK"]["S"]
            if (record.get("eventName") == "INSERT"
                    and type(pk) is str and pk.startswith("OUTBOX#") and sk == "DISPATCH"):
                job_id = pk.removeprefix("OUTBOX#")
                if (not _has_processing_time(relay, context)
                        or str(uuid.UUID(job_id)) != job_id or not relay.dispatch(job_id)):
                    failures.append({"itemIdentifier": sequence})
        except Exception:
            failures.append({"itemIdentifier": sequence})
    return {"batchItemFailures": failures}


def run(event, context):
    from mock_journey.worker_runtime import get_relay
    from mock_journey.aws_runtime import invocation
    try:
        relay = get_relay()
        with invocation(relay, context):
            if type(event) is dict and event.get("source") == "aws.events":
                return relay.reconcile(context=context)
            return handle_stream(event, context, relay)
    except Exception:
        # Retry the invocation, without exposing SDK/configuration/event text.
        raise JourneyError("TEMPORARILY_UNAVAILABLE") from None

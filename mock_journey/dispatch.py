"""Outbox delivery and independent due-job reconciliation, with references only."""

from mock_journey.bootstrap import configure_imports

configure_imports()

import json
import time
import uuid

from mock_journey.errors import JourneyError


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
    def __init__(self, jobs, sender, *, lease_seconds, retry_seconds, page_size, max_pages, clock=time.time):
        if any(type(value) is not int or value <= 0 for value in (lease_seconds, retry_seconds, page_size, max_pages)):
            raise ValueError("Verified relay operating limits are required.")
        self.jobs, self.sender, self.clock = jobs, sender, clock
        self.lease_seconds, self.retry_seconds = lease_seconds, retry_seconds
        self.page_size, self.max_pages = page_size, max_pages

    def dispatch(self, job_id):
        from mock_journey.jobs import JobLeaseLost
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
        except Exception:
            try:
                self.jobs.release_outbox(job_id, owner, outbox["fence"],
                                         next_due_at=int(self.clock()) + self.retry_seconds,
                                         error_code="TEMPORARILY_UNAVAILABLE")
            except (JourneyError, JobLeaseLost):
                pass
            return False

    def reconcile(self):
        counts = {"outbox_wakes": 0, "job_wakes": 0, "failures": 0}
        # A sent outbox or a queue DLQ must not hide a non-terminal Job.
        for getter, key in ((self.jobs.due_outbox, "outbox_wakes"), (self.jobs.due_jobs, "job_wakes")):
            cursor = None
            for _ in range(self.max_pages):
                rows, cursor = getter(limit=self.page_size, cursor=cursor)
                for row in rows:
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
        return counts


def handle_stream(event, context, relay):
    records = event.get("Records") if type(event) is dict else None
    if type(records) is not list:
        raise ValueError("Invalid stream envelope.")
    failures = []
    for record in records:
        # Only keys/sequence metadata are read. Full NewImage may contain
        # unrelated session/user data and must never be logged or enqueued.
        sequence = record.get("dynamodb", {}).get("SequenceNumber")
        if type(sequence) is not str or not sequence:
            raise ValueError("Invalid stream envelope.")
        try:
            keys = record["dynamodb"]["Keys"]
            pk, sk = keys["PK"]["S"], keys["SK"]["S"]
            if (record.get("eventName") == "INSERT"
                    and type(pk) is str and pk.startswith("OUTBOX#") and sk == "DISPATCH"):
                job_id = pk.removeprefix("OUTBOX#")
                if str(uuid.UUID(job_id)) != job_id or not relay.dispatch(job_id):
                    failures.append({"itemIdentifier": sequence})
        except Exception:
            failures.append({"itemIdentifier": sequence})
    return {"batchItemFailures": failures}


def run(event, context):
    from mock_journey.worker_runtime import get_relay
    relay = get_relay()
    if type(event) is dict and event.get("source") == "aws.events":
        return relay.reconcile()
    return handle_stream(event, context, relay)

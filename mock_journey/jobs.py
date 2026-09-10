"""Durable job/outbox transactions, composed with the existing control store."""

from copy import deepcopy
import hashlib
import json
import re

from botocore.exceptions import BotoCoreError, ClientError

from mock_journey.contracts import PENDING_GOAL_ADAPTER_VERSION, PENDING_GOAL_PROFILE_VERSION
from mock_journey.errors import JourneyError
from mock_journey.state import _ReadConflict, _decode, _encode, _key, _unavailable


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_BINDING = ("attempt_id", "epoch", "input_digest", "adapter_version", "projection_version", "job_id", "call_id")
_FAILURES = {"STORED_INPUT_INVALID", "CALCULATOR_CONTRACT_MISMATCH", "CALCULATION_FAILED"}


class JobLeaseLost(Exception):
    def __init__(self):
        super().__init__("The job lease is no longer owned by this worker.")


def _integer(value, *, positive=False):
    if type(value) is not int or value < (1 if positive else 0):
        raise _unavailable()
    return value


def _text(value):
    if type(value) is not str or not value:
        raise _unavailable()
    return value


def _digest(value):
    if type(value) is not str or not _SHA256.fullmatch(value):
        raise JourneyError("STORED_INPUT_INVALID")
    return value


def _reference(value, *, planned=False):
    fields = {"bucket", "key"} if planned else {"bucket", "key", "sha256", "size"}
    if type(value) is not dict or set(value) != fields:
        raise JourneyError("STORED_INPUT_INVALID")
    if any(type(value[k]) is not str or not value[k] for k in ("bucket", "key")):
        raise JourneyError("STORED_INPUT_INVALID")
    if not planned:
        _digest(value["sha256"])
        if type(value["size"]) is not int or value["size"] < 0:
            raise JourneyError("STORED_INPUT_INVALID")
    return deepcopy(value)


class DynamoJobRepository:
    def __init__(self, state):
        self.state = state

    def _command(self, build):
        for _ in range(self.state.max_conflict_retries):
            try:
                result, actions = build()
            except _ReadConflict:
                continue
            if not actions or self.state._write(actions):
                return result
        raise _unavailable()

    def get_job(self, job_id):
        return self.state._get(_key("JOB", job_id, "STATE"))

    def _job(self, job_id):
        job = self.get_job(job_id)
        if not job:
            raise JourneyError("NOT_FOUND")
        return job

    def _job_attempt(self, job_id, *, with_user=False):
        initial = self._job(job_id)
        keys = [_key("JOB", job_id, "STATE"), _key("ATTEMPT", initial["attempt_id"], "META")]
        if with_user:
            keys.append(_key("USER", initial["principal"], "STATE"))
        rows = self.state._read(keys)
        job, attempt = rows[:2]
        if (not job or not attempt or job.get("attempt_id") != attempt.get("attempt_id")
                or job.get("principal") != attempt.get("principal")
                or job.get("epoch") != attempt.get("epoch")
                or attempt.get("job_id") != job_id or attempt.get("input_digest") != job.get("input_digest")
                or type(attempt.get("definition_json")) is not str
                or hashlib.sha256(attempt["definition_json"].encode()).hexdigest() != job.get("definition_sha256")):
            raise JourneyError("STORED_INPUT_INVALID")
        if with_user:
            self.state._require_user(rows[2], job["principal"])
        return rows

    @staticmethod
    def _lease(job, owner, fence, now):
        if (job.get("owner") != owner or job.get("fence") != fence
                or job.get("lease_until", 0) <= now or job.get("state") in {"done", "failed"}):
            raise JobLeaseLost()

    def _job_action(self, old, new, *, owner=None, fence=None, now=None):
        expression = "#r = :revision AND #a = :attempt AND #i = :input AND #phase = :phase AND #call = :call"
        names = {"#r": "revision", "#a": "attempt_id", "#i": "input_digest", "#phase": "call_phase", "#call": "call_id"}
        values = {":revision": old["revision"], ":attempt": old["attempt_id"], ":input": old["input_digest"],
                  ":phase": old["call_phase"], ":call": old["call_id"]}
        if owner is not None:
            expression += " AND #o = :owner AND #f = :fence AND #l > :now"
            names.update({"#o": "owner", "#f": "fence", "#l": "lease_until"})
            values.update({":owner": owner, ":fence": fence, ":now": now})
        return self.state._replace(new, expression, names, values)

    @staticmethod
    def _due(item, when, kind):
        _integer(when)
        item.update(next_due_at=when, GSI1PK=f"DUE#{kind}", GSI1SK=when)

    @staticmethod
    def _remove_due(item):
        for key in ("next_due_at", "GSI1PK", "GSI1SK"):
            item.pop(key, None)

    def accept_input(self, auth, attempt_id, input_digest, manifest_ref, *, job_id, adapter_version, next_due_at):
        _digest(input_digest)
        reference = _reference(manifest_ref)
        _text(job_id)
        _integer(next_due_at)

        def build():
            session, attempt = self.state._read([
                _key("SESSION", auth.session_id, "AUTH"), _key("ATTEMPT", attempt_id, "META"),
            ])
            now = self.state._now()
            self.state._check_session(session, auth, now)
            self.state._check_attempt(attempt, auth)
            if attempt.get("input_digest") is not None:
                if attempt["input_digest"] != input_digest:
                    raise JourneyError("ATTEMPT_INPUT_CONFLICT")
                return attempt, None
            if attempt["state"] != "created":
                raise JourneyError("INVALID_STATE")
            try:
                definition = json.loads(attempt["definition_json"])
                projection_version = _text(definition["projection_version"])
                if definition["adapter_version"] != adapter_version:
                    raise JourneyError("PROFILE_MISMATCH")
            except (KeyError, TypeError, ValueError):
                raise JourneyError("STORED_INPUT_INVALID") from None
            updated = {**attempt, "state": "queued", "revision": attempt["revision"] + 1,
                       "input_digest": input_digest, "input_manifest_ref": reference, "job_id": job_id}
            job = {
                **_key("JOB", job_id, "STATE"), "job_id": job_id, "attempt_id": attempt_id,
                "principal": attempt["principal"], "epoch": attempt["epoch"], "input_digest": input_digest,
                "input_manifest_ref": reference, "adapter_version": adapter_version,
                "projection_version": projection_version,
                "definition_sha256": hashlib.sha256(attempt["definition_json"].encode()).hexdigest(),
                "state": "queued", "call_phase": "not_started", "revision": 0, "owner": None,
                "lease_until": 0, "fence": 0, "execution_fence": 0,
                "call_id": None, "planned_candidate_ref": None,
                "candidate_ref": None, "chart_snapshot": {"kind": "unset", "revision": 0},
                "final_ref": None, "chart_publication": None, "error_code": None, "created_at": now,
            }
            self._due(job, next_due_at, "JOB")
            outbox = {
                **_key("OUTBOX", job_id, "DISPATCH"), "job_id": job_id, "state": "pending",
                "owner": None, "lease_until": 0, "fence": 0, "revision": 0,
                "delivery_attempts": 0, "error_code": None,
            }
            self._due(outbox, next_due_at, "OUTBOX")
            attempt_action = self.state._attempt_action(attempt, updated)
            attempt_action["Put"]["ConditionExpression"] += " AND attribute_not_exists(#input)"
            attempt_action["Put"]["ExpressionAttributeNames"]["#input"] = "input_digest"
            return updated, [self.state._session_condition(auth, now), attempt_action,
                             self.state._put(job), self.state._put(outbox)]

        return self._command(build)

    def claim(self, job_id, owner, lease_seconds):
        _text(owner)
        _integer(lease_seconds, positive=True)

        def build():
            job, attempt = self._job_attempt(job_id)
            now = self.state._now()
            if job["state"] in {"done", "failed"}:
                return ("done", job), None
            if job.get("call_phase") not in {"not_started", "started", "candidate_saved"}:
                # An old or unknown execution protocol is not permission to
                # reinterpret accepted work as a new internal calculation.
                raise _unavailable()
            if job["lease_until"] > now or job.get("next_due_at", now) > now:
                return ("busy", job), None
            action = "execute" if job["call_phase"] == "not_started" else "recover"
            updated = {**job, "state": "running", "owner": owner, "lease_until": now + lease_seconds,
                       "fence": job["fence"] + 1, "revision": job["revision"] + 1}
            self._due(updated, updated["lease_until"], "JOB")
            actions = [self._job_action(job, updated)]
            if attempt["state"] == "queued":
                newer = {**attempt, "state": "processing", "revision": attempt["revision"] + 1}
                actions.append(self.state._attempt_action(attempt, newer))
            return (action, updated), actions

        return self._command(build)

    def renew_lease(self, job_id, owner, fence, lease_seconds):
        _integer(lease_seconds, positive=True)

        def build():
            job = self._job(job_id)
            now = self.state._now()
            self._lease(job, owner, fence, now)
            updated = {**job, "lease_until": now + lease_seconds, "revision": job["revision"] + 1}
            self._due(updated, updated["lease_until"], "JOB")
            return updated, [self._job_action(job, updated, owner=owner, fence=fence, now=now)]

        return self._command(build)

    def begin_calculation(self, job_id, owner, fence, call_id, planned_candidate_ref, *, previous_call_id=None):
        """Choose one candidate path per execution; recovery rotates ownership.

        A worker recovering a missing candidate must hold a newer lease/fence
        and name the execution it inspected. The prior worker can only write
        its old path, never the newly selected candidate.
        """
        _text(call_id)
        reference = _reference(planned_candidate_ref, planned=True)

        def build():
            job = self._job(job_id)
            now = self.state._now()
            self._lease(job, owner, fence, now)
            if job["call_phase"] == "not_started":
                allowed = previous_call_id is None and job["call_id"] is None
            elif job["call_phase"] == "started":
                allowed = (previous_call_id is not None and previous_call_id == job["call_id"]
                           and call_id != previous_call_id
                           and type(job.get("execution_fence")) is int
                           and job["execution_fence"] < fence
                           and job.get("candidate_ref") is None
                           and job["chart_snapshot"]["kind"] == "unset"
                           and reference != job["planned_candidate_ref"])
            else:
                allowed = False
            if not allowed:
                return (False, job), None
            updated = {**job, "call_phase": "started", "call_id": call_id,
                       "execution_fence": fence, "planned_candidate_ref": reference,
                       "revision": job["revision"] + 1}
            return (True, updated), [self._job_action(job, updated, owner=owner, fence=fence, now=now)]

        return self._command(build)

    def mark_calculation_saved(self, job_id, owner, fence, candidate_ref):
        reference = _reference(candidate_ref)

        def build():
            job = self._job(job_id)
            now = self.state._now()
            self._lease(job, owner, fence, now)
            planned = job["planned_candidate_ref"]
            if (job["call_phase"] not in {"started", "candidate_saved"} or not planned
                    or {key: reference[key] for key in ("bucket", "key")} != planned):
                raise JourneyError("STORED_INPUT_INVALID")
            if job["candidate_ref"] is not None:
                if job["candidate_ref"] != reference:
                    raise JourneyError("STORED_INPUT_INVALID")
                return job, None
            updated = {**job, "call_phase": "candidate_saved", "candidate_ref": reference,
                       "revision": job["revision"] + 1}
            return updated, [self._job_action(job, updated, owner=owner, fence=fence, now=now)]

        return self._command(build)

    @staticmethod
    def _binding(value, job):
        if type(value) is not dict or any(value.get(key) != job.get(key) for key in _BINDING):
            raise JourneyError("STORED_INPUT_INVALID")

    def pin_chart(self, job_id, owner, fence, selection):
        def build():
            job = self._job(job_id)
            now = self.state._now()
            self._lease(job, owner, fence, now)
            self._binding(selection, job)
            if job["call_phase"] != "candidate_saved":
                raise JourneyError("INVALID_STATE")
            chosen = job["chart_snapshot"]
            if chosen["kind"] != "unset":
                return chosen, None
            kind = selection.get("kind")
            expected = set(_BINDING) | {"kind"}
            if kind == "snapshot":
                expected |= {"snapshot_ref", "source_sha256", "published_body_sha256"}
                _reference(selection.get("snapshot_ref"))
                _digest(selection.get("source_sha256"))
                _digest(selection.get("published_body_sha256"))
            elif kind != "no_chart":
                raise JourneyError("STORED_INPUT_INVALID")
            if set(selection) != expected:
                raise JourneyError("STORED_INPUT_INVALID")
            chosen = {**deepcopy(selection), "revision": 1}
            updated = {**job, "chart_snapshot": chosen, "revision": job["revision"] + 1}
            return chosen, [self._job_action(job, updated, owner=owner, fence=fence, now=now)]

        return self._command(build)

    @staticmethod
    def _evaluation(value, attempt):
        if type(value) is not dict or set(value) != {"goal", "score", "program_completed", "reason_codes"}:
            raise JourneyError("CALCULATOR_CONTRACT_MISMATCH")
        definition = json.loads(attempt["definition_json"])
        goal = value["goal"]
        score = value["score"]
        versioned_goal = definition.get("adapter_version") == PENDING_GOAL_ADAPTER_VERSION
        goal_fields = {"kind", "required", "observed", "met"}
        if versioned_goal:
            goal_fields.add("status")
        if (type(goal) is not dict or set(goal) != goal_fields
                or goal.get("kind") != definition["goal"]["kind"]
                or type(goal.get("required")) is not int or goal["required"] != definition["goal"]["required"]
                or (versioned_goal and definition.get("profile_version") != PENDING_GOAL_PROFILE_VERSION)
                or type(score) is not dict or set(score) != {"decision"}
                or type(score.get("decision")) is not str or score["decision"] not in {"pass", "fail"}):
            raise JourneyError("CALCULATOR_CONTRACT_MISMATCH")
        expected_status = "pending_policy" if goal["kind"] == "cycles" else "evaluated"
        if versioned_goal and goal["status"] != expected_status:
            raise JourneyError("CALCULATOR_CONTRACT_MISMATCH")
        pending = versioned_goal and expected_status == "pending_policy"
        if pending:
            if goal["observed"] is not None or goal["met"] is not None:
                raise JourneyError("CALCULATOR_CONTRACT_MISMATCH")
            completed, reasons = False, ["GOAL_POLICY_UNRESOLVED"]
        else:
            if (type(goal["observed"]) is not int or goal["observed"] < 0
                    or type(goal["met"]) is not bool or goal["met"] != (goal["observed"] >= goal["required"])):
                raise JourneyError("CALCULATOR_CONTRACT_MISMATCH")
            completed = goal["met"] and score["decision"] == "pass"
            reasons = [] if goal["met"] else ["GOAL_NOT_MET"]
        reasons += [] if score["decision"] == "pass" else ["SCORE_NOT_PASS"]
        if type(value["program_completed"]) is not bool or value["program_completed"] != completed or value["reason_codes"] != reasons:
            raise JourneyError("CALCULATOR_CONTRACT_MISMATCH")
        return deepcopy(value)

    def _close_count(self, attempt, user, now):
        if attempt["epoch"] != user["epoch"] or not attempt["active_counted"]:
            return None
        slot_key = f'{attempt["program_id"]}:{attempt["target"]}'
        if self.state._slot(user, slot_key)["open_attempts"] <= 0:
            raise _unavailable()
        changed = deepcopy(user)
        changed["slots"][slot_key]["open_attempts"] -= 1
        changed.update(revision=user["revision"] + 1, updated_at=now)
        return changed

    @staticmethod
    def _publication(value, job):
        DynamoJobRepository._binding(value, job)
        selected = job["chart_snapshot"]
        if (selected["kind"] == "unset" or value.get("kind") != selected["kind"]
                or type(value.get("selection_revision")) is not int
                or value["selection_revision"] < 1
                or value["selection_revision"] != selected["revision"]):
            raise JourneyError("STORED_INPUT_INVALID")
        fields = set(_BINDING) | {"kind", "selection_revision"}
        if selected["kind"] == "snapshot":
            fields |= {"key", "published_body_sha256"}
            if (value.get("published_body_sha256") != selected["published_body_sha256"]
                    or type(value.get("key")) is not str or not value["key"]):
                raise JourneyError("STORED_INPUT_INVALID")
        elif selected["kind"] != "no_chart":
            raise JourneyError("STORED_INPUT_INVALID")
        if set(value) != fields:
            raise JourneyError("STORED_INPUT_INVALID")

    def finalize(self, job_id, owner, fence, final_ref, evaluation, chart_publication):
        reference = _reference(final_ref)

        def build():
            job, attempt, user = self._job_attempt(job_id, with_user=True)
            if job["state"] == "done":
                return attempt, None
            now = self.state._now()
            self._lease(job, owner, fence, now)
            if job["call_phase"] != "candidate_saved":
                raise JourneyError("INVALID_STATE")
            selected = job["chart_snapshot"]
            self._publication(chart_publication, job)
            checked = self._evaluation(evaluation, attempt)
            changed_user = self._close_count(attempt, user, now)
            applied = False
            if attempt["epoch"] != user["epoch"]:
                reason = "PROGRESS_RESET"
            elif checked["goal"].get("status") == "pending_policy":
                reason = "GOAL_POLICY_UNRESOLVED"
            elif not checked["program_completed"]:
                reason = "REQUIREMENTS_NOT_MET"
            else:
                slot_key = f'{attempt["program_id"]}:{attempt["target"]}'
                if self.state._slot(user, slot_key)["completed"]:
                    reason = "ALREADY_COMPLETED"
                else:
                    applied, reason = True, "APPLIED"
                    if changed_user is None:
                        changed_user = deepcopy(user)
                        changed_user.update(revision=user["revision"] + 1, updated_at=now)
                    changed_user["slots"][slot_key].update(
                        completed=True, completed_by_attempt=attempt["attempt_id"], completed_at=now)
            progress = {"applied": applied, "applied_epoch": attempt["epoch"] if applied else None, "reason": reason}
            updated_attempt = {**attempt, "state": "evaluated", "revision": attempt["revision"] + 1,
                               "active_counted": False, "result_ref": reference, "evaluation": checked,
                               "progress_application": progress, "chart_publication": deepcopy(chart_publication)}
            updated_attempt.pop("error_code", None)
            updated_job = {**job, "state": "done", "revision": job["revision"] + 1,
                           "owner": None, "lease_until": 0, "final_ref": reference, "error_code": None,
                           "chart_publication": deepcopy(chart_publication)}
            self._remove_due(updated_job)
            job_action = self._job_action(job, updated_job, owner=owner, fence=fence, now=now)
            job_action["Put"]["ConditionExpression"] += " AND #chart = :chart"
            job_action["Put"]["ExpressionAttributeNames"]["#chart"] = "chart_snapshot"
            job_action["Put"]["ExpressionAttributeValues"].update(_encode({":chart": selected}))
            return updated_attempt, [job_action, self.state._attempt_action(attempt, updated_attempt),
                                     self.state._user_action(user, changed_user)]

        return self._command(build)

    def _finish_error(self, job_id, owner, fence, error_code, next_due_at):
        unknown = next_due_at is not None
        if unknown:
            _integer(next_due_at)
            if error_code != "CALCULATION_OUTCOME_UNKNOWN":
                raise _unavailable()
        elif error_code not in _FAILURES:
            raise _unavailable()

        def build():
            job, attempt, user = self._job_attempt(job_id, with_user=True)
            now = self.state._now()
            self._lease(job, owner, fence, now)
            if unknown and job["call_phase"] != "started":
                raise JourneyError("INVALID_STATE")
            state = "outcome_unknown" if unknown else "failed"
            updated_job = {**job, "state": state, "owner": None, "lease_until": 0,
                           "revision": job["revision"] + 1, "error_code": error_code}
            if unknown:
                self._due(updated_job, next_due_at, "JOB")
            else:
                self._remove_due(updated_job)
            updated_attempt = {**attempt, "state": state, "active_counted": False,
                               "revision": attempt["revision"] + 1, "evaluation": None,
                               "progress_application": None, "error_code": error_code}
            changed_user = self._close_count(attempt, user, now)
            return updated_job, [self._job_action(job, updated_job, owner=owner, fence=fence, now=now),
                                 self.state._attempt_action(attempt, updated_attempt),
                                 self.state._user_action(user, changed_user)]

        return self._command(build)

    def mark_unknown(self, job_id, owner, fence, error_code, *, next_due_at):
        return self._finish_error(job_id, owner, fence, error_code, next_due_at)

    def mark_failed(self, job_id, owner, fence, error_code):
        return self._finish_error(job_id, owner, fence, error_code, None)

    def _outbox(self, job_id):
        record = self.state._get(_key("OUTBOX", job_id, "DISPATCH"))
        if not record:
            raise JourneyError("NOT_FOUND")
        return record

    def _outbox_action(self, old, new, *, owner=None, fence=None, now=None):
        expression, names, values = "#r = :revision", {"#r": "revision"}, {":revision": old["revision"]}
        if owner is not None:
            expression += " AND #o = :owner AND #f = :fence AND #l > :now"
            names.update({"#o": "owner", "#f": "fence", "#l": "lease_until"})
            values.update({":owner": owner, ":fence": fence, ":now": now})
        return self.state._replace(new, expression, names, values)

    def claim_outbox(self, job_id, owner, lease_seconds):
        _text(owner)
        _integer(lease_seconds, positive=True)

        def build():
            old = self._outbox(job_id)
            now = self.state._now()
            if old["state"] == "sent":
                return ("done", old), None
            if old["lease_until"] > now or old.get("next_due_at", now) > now:
                return ("busy", old), None
            new = {**old, "owner": owner, "lease_until": now + lease_seconds,
                   "fence": old["fence"] + 1, "revision": old["revision"] + 1,
                   "delivery_attempts": old["delivery_attempts"] + 1}
            self._due(new, new["lease_until"], "OUTBOX")
            return ("send", new), [self._outbox_action(old, new)]

        return self._command(build)

    def mark_outbox_sent(self, job_id, owner, fence):
        def build():
            old = self._outbox(job_id)
            if old["state"] == "sent":
                return old, None
            now = self.state._now()
            self._lease(old, owner, fence, now)
            new = {**old, "state": "sent", "owner": None, "lease_until": 0,
                   "revision": old["revision"] + 1, "error_code": None}
            self._remove_due(new)
            return new, [self._outbox_action(old, new, owner=owner, fence=fence, now=now)]

        return self._command(build)

    def release_outbox(self, job_id, owner, fence, *, next_due_at, error_code):
        _integer(next_due_at)
        if error_code != "TEMPORARILY_UNAVAILABLE":
            raise _unavailable()

        def build():
            old = self._outbox(job_id)
            now = self.state._now()
            self._lease(old, owner, fence, now)
            new = {**old, "owner": None, "lease_until": 0, "revision": old["revision"] + 1,
                   "error_code": error_code}
            self._due(new, next_due_at, "OUTBOX")
            return new, [self._outbox_action(old, new, owner=owner, fence=fence, now=now)]

        return self._command(build)

    def _due_page(self, kind, *, limit, cursor):
        _integer(limit, positive=True)
        now = self.state._now()
        request = {
            "TableName": self.state.table_name, "IndexName": "GSI1", "Limit": limit,
            "KeyConditionExpression": "#pk = :kind AND #sk <= :now",
            "ExpressionAttributeNames": {"#pk": "GSI1PK", "#sk": "GSI1SK"},
            "ExpressionAttributeValues": _encode({":kind": f"DUE#{kind}", ":now": now}),
        }
        if cursor is not None:
            request["ExclusiveStartKey"] = _encode(cursor)
        try:
            response = self.state.client.query(**request)
            records = []
            for value in response.get("Items", []):
                row = _decode(value)
                current = self.state._get({"PK": row["PK"], "SK": row["SK"]})
                if (current and current.get("GSI1PK") == f"DUE#{kind}"
                        and current.get("next_due_at", now + 1) <= self.state._now()
                        and current.get("lease_until", 0) <= self.state._now()):
                    records.append(current)
            continuation = response.get("LastEvaluatedKey")
            return records, _decode(continuation) if continuation else None
        except (ClientError, BotoCoreError, KeyError, TypeError):
            raise _unavailable() from None

    def due_outbox(self, *, limit, cursor=None):
        return self._due_page("OUTBOX", limit=limit, cursor=cursor)

    def due_jobs(self, *, limit, cursor=None):
        return self._due_page("JOB", limit=limit, cursor=cursor)

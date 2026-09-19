"""Durable job/outbox transactions, composed with the existing control store."""

from copy import deepcopy
import hashlib
import json
import re

from botocore.exceptions import BotoCoreError, ClientError

from mock_journey.contracts import PENDING_GOAL_ADAPTER_VERSIONS, PENDING_GOAL_PROFILE_VERSION
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
    def __init__(self, state, *, course_blobs=None):
        self.state = state
        self.course_blobs = course_blobs
        self.course_recovery = None

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

    @staticmethod
    def _require_unsealed(job):
        if job.get("terminal_seal"):
            raise JourneyError("INVALID_STATE")

    def _binding_map(self, attempt):
        from mock_journey.course_contracts import CourseBinding
        raw = attempt.get("course_binding")
        if raw is None:
            return None
        try:
            bound = CourseBinding(**raw) if type(raw) is dict else raw
            if type(bound) is not CourseBinding:
                raise ValueError()
            return dict(bound.__dict__)
        except (ValueError, TypeError):
            raise JourneyError("STORED_INPUT_INVALID") from None

    def _course_rows(self, attempt, user):
        binding = self._binding_map(attempt)
        if binding is None:
            return {"is_dummy": attempt.get("principal") == "dummy-tester"}
        epoch = attempt["epoch"]
        if epoch != user["epoch"]:
            return {"is_dummy": attempt.get("principal") == "dummy-tester"}
        pk = f"COURSE#{binding['scope_key']}"
        item_key = {"PK": pk, "SK": f"EPOCH#{epoch}#ITEM#{binding['placement_key']}"}
        head, item, final = self.state._read([
            {"PK": pk, "SK": f"EPOCH#{epoch}#HEAD"}, item_key,
            {"PK": pk, "SK": f"EPOCH#{epoch}#FINAL"},
        ])
        if head is None or final is None or self.course_blobs is None:
            raise _unavailable()
        from mock_journey.course_state import bundle_from_record
        from mock_journey.course_policy import scope_key
        from mock_journey.typed import parse_json
        body = self.course_blobs.get_bytes(head.get("bundle_ref"))
        if body is None:
            raise _unavailable()
        bundle = bundle_from_record(parse_json(body))
        if (bundle.definition_hash != head.get("definition_hash")
                or scope_key(bundle.scope) != binding["scope_key"]
                or bundle.scope.learner.principal != attempt["principal"]):
            raise JourneyError("STORED_INPUT_INVALID")
        return {
            "head": head, "item": item, "final": final, "bundle": bundle,
            "is_dummy": attempt.get("principal") == "dummy-tester",
        }

    def _course_put_from_row(self, current, updates, match):
        if current is None:
            raise _unavailable()
        updated = deepcopy(current)
        bump = updates.pop("revision_bump", None)
        updated.update(updates)
        if bump is True or "revision" in current:
            revision = current.get("revision")
            if type(revision) is not int:
                raise _unavailable()
            updated["revision"] = revision + 1
            match = dict(match)
            match.setdefault("revision", revision)
        expression, names, values = DynamoJobRepository._match_expression(match)
        return self.state._replace(updated, expression, names, values)

    @staticmethod
    def _match_expression(match):
        names, values, parts = {}, {}, []
        for index, (field, value) in enumerate(match.items()):
            name, token = f"#cm{index}", f":cm{index}"
            names[name] = field
            values[token] = value
            parts.append(f"{name} = {token}")
        return " AND ".join(parts), names, values

    def _plan_extra_actions(self, payload, course_rows, attempt_epoch, user_epoch):
        extra = []
        conditions = payload.get("conditions") or {}
        for write in payload.get("writes") or []:
            kind = write.get("kind")
            key = write.get("key")
            fields = write.get("set")
            if type(key) is not list or len(key) != 2 or type(fields) is not dict:
                raise JourneyError("CALCULATOR_CONTRACT_MISMATCH")
            if kind in ("JOB", "ATTEMPT", "USER"):
                continue
            if kind == "SUBMISSION":
                item = {"PK": key[0], "SK": key[1], **deepcopy(fields)}
                extra.append(self.state._put(item))
                continue
            if attempt_epoch != user_epoch:
                continue
            current = None
            if kind == "COURSE_ITEM":
                current = course_rows.get("item")
            elif kind == "COURSE_HEAD":
                current = course_rows.get("head")
            elif kind == "COURSE_FINAL":
                current = course_rows.get("final")
            else:
                raise JourneyError("CALCULATOR_CONTRACT_MISMATCH")
            if current is not None and key != [current["PK"], current["SK"]]:
                raise JourneyError("CALCULATOR_CONTRACT_MISMATCH")
            match = {"epoch": attempt_epoch}
            if current is not None and type(current.get("revision")) is int:
                match["revision"] = current["revision"]
            if kind == "COURSE_HEAD":
                match["definition_hash"] = conditions["head_definition_hash"]
            elif kind == "COURSE_FINAL":
                match["active_attempt_id"] = conditions["final_active_attempt_id"]
                match["phase"] = conditions["final_phase"]
            extra.append(self._course_put_from_row(current, deepcopy(fields), match))
        return extra

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
            bound = self._binding_map(attempt)
            if bound is not None:
                job["course_binding"] = bound
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
            self._require_unsealed(job)
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
            if attempt["state"] == "queued" or (self._binding_map(attempt) is not None
                                                  and attempt["state"] in {"failed", "outcome_unknown"}):
                newer = {**attempt, "state": "processing", "revision": attempt["revision"] + 1}
                actions.append(self.state._attempt_action(attempt, newer))
            return (action, updated), actions

        return self._command(build)

    def renew_lease(self, job_id, owner, fence, lease_seconds):
        _integer(lease_seconds, positive=True)

        def build():
            job = self._job(job_id)
            now = self.state._now()
            self._require_unsealed(job)
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
            self._require_unsealed(job)
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
            self._require_unsealed(job)
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
        versioned_goal = definition.get("adapter_version") in PENDING_GOAL_ADAPTER_VERSIONS
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

    def finalize(self, job_id, owner, fence, final_ref, evaluation, chart_publication, *,
                 completion_plan=None):
        reference = _reference(final_ref)

        def build():
            job, attempt, user = self._job_attempt(job_id, with_user=True)
            if job["state"] == "done":
                return attempt, None
            self._require_unsealed(job)
            now = self.state._now()
            self._lease(job, owner, fence, now)
            if job["call_phase"] != "candidate_saved":
                raise JourneyError("INVALID_STATE")
            selected = job["chart_snapshot"]
            self._publication(chart_publication, job)
            checked = self._evaluation(evaluation, attempt)
            bound = self._binding_map(attempt)
            if bound is not None and completion_plan is None:
                raise _unavailable()
            plan_payload = None
            plan_receipt = None
            course_rows = None
            if completion_plan is not None and bound is not None:
                from mock_journey.typed import parse_json
                course_rows = self._course_rows(attempt, user)
                plan = completion_plan.build(job, attempt, user, course_rows, checked)
                plan_payload = parse_json(plan.actions_json)
                plan_receipt = parse_json(plan.result_receipt_json)
            changed_user = self._close_count(attempt, user, now)
            if plan_receipt is not None:
                progress = plan_receipt["progress_application"]
            else:
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
                progress = {"applied": applied, "applied_epoch": attempt["epoch"] if applied else None,
                            "reason": reason}
            updated_attempt = {**attempt, "state": "evaluated", "revision": attempt["revision"] + 1,
                               "active_counted": False, "result_ref": reference, "evaluation": checked,
                               "progress_application": progress, "chart_publication": deepcopy(chart_publication)}
            if plan_receipt is not None:
                updated_attempt["submit_arc"] = plan_receipt["submit_arc"]
            updated_attempt.pop("error_code", None)
            updated_job = {**job, "state": "done", "revision": job["revision"] + 1,
                           "owner": None, "lease_until": 0, "final_ref": reference, "error_code": None,
                           "chart_publication": deepcopy(chart_publication)}
            self._remove_due(updated_job)
            job_action = self._job_action(job, updated_job, owner=owner, fence=fence, now=now)
            job_action["Put"]["ConditionExpression"] += " AND #chart = :chart"
            job_action["Put"]["ExpressionAttributeNames"]["#chart"] = "chart_snapshot"
            job_action["Put"]["ExpressionAttributeValues"].update(_encode({":chart": selected}))
            actions = [job_action, self.state._attempt_action(attempt, updated_attempt),
                       self.state._user_action(user, changed_user)]
            if plan_payload is not None:
                actions.extend(self._plan_extra_actions(
                    plan_payload, course_rows, attempt["epoch"], user["epoch"],
                ))
            if len(actions) > 20:
                raise _unavailable()
            return updated_attempt, actions

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
            self._require_unsealed(job)
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

    def _evidence_digest(self, evidence):
        from mock_journey.typed import digest
        return digest([
            evidence.job_id, evidence.attempt_id, evidence.job_revision, evidence.attempt_revision,
            evidence.epoch, evidence.call_id, evidence.fence, evidence.input_digest,
            evidence.stage, evidence.candidate_state, evidence.code, evidence.action,
            None if evidence.snapshot_json is None else evidence.snapshot_json.decode("utf-8"),
        ])

    def is_course_job(self, job_id):
        return self._binding_map(self._job_attempt(job_id)[1]) is not None

    def recovery_snapshot(self, job_id):
        """One transaction snapshot of the original course scope, never current-course substitution."""
        for _ in range(self.state.max_conflict_retries):
            try:
                job, attempt, user = self._job_attempt(job_id, with_user=True)
                binding = self._binding_map(attempt)
                if binding is None:
                    raise JourneyError("INVALID_STATE")
                pk = f"COURSE#{binding['scope_key']}"
                keys = [_key("JOB", job_id, "STATE"), _key("ATTEMPT", attempt["attempt_id"], "META"),
                        _key("USER", attempt["principal"], "STATE"),
                        {"PK": pk, "SK": f"EPOCH#{attempt['epoch']}#HEAD"},
                        {"PK": pk, "SK": f"EPOCH#{attempt['epoch']}#FINAL"}]
                current = self.state._read(keys)
                if not all(current):
                    raise JourneyError("STORED_INPUT_INVALID")
                if current[0]["revision"] != job["revision"] or current[1]["revision"] != attempt["revision"]:
                    continue
                return dict(zip(("job", "attempt", "user", "head", "final"), current), now=self.state._now())
            except _ReadConflict:
                continue
        raise _unavailable()

    def _verify_course_evidence(self, job_id, evidence, action, owner, fence):
        from mock_journey.course_contracts import RecoveryEvidence
        from mock_journey.course_recovery import recovery_snapshot_context
        from mock_journey.typed import parse_json
        if (type(evidence) is not RecoveryEvidence or evidence.snapshot_json is None
                or evidence.action != action or evidence.job_id != job_id or self.course_recovery is None):
            raise JourneyError("INVALID_STATE")
        fresh = self.course_recovery.inspect(job_id, owner, fence)
        if fresh != evidence:
            raise JourneyError("INVALID_STATE")
        snap = self.recovery_snapshot(job_id)
        job, attempt = snap["job"], snap["attempt"]
        if (recovery_snapshot_context(snap) != parse_json(evidence.snapshot_json)
                or job["revision"] != evidence.job_revision or attempt["revision"] != evidence.attempt_revision
                or job["fence"] != evidence.fence or job.get("call_id") != evidence.call_id
                or job["input_digest"] != evidence.input_digest or attempt["epoch"] != evidence.epoch):
            raise JourneyError("INVALID_STATE")
        self._require_unsealed(job)
        return snap

    def _recovery_course_actions(self, snap, *, release=False):
        job, attempt, user, head, final = (snap[k] for k in ("job", "attempt", "user", "head", "final"))
        binding = self._binding_map(attempt)
        actions = []
        for row in (head, final):
            match = {"revision": row["revision"], "epoch": attempt["epoch"]}
            if row is head:
                match["definition_hash"] = head["definition_hash"]
            else:
                match.update(active_attempt_id=final["active_attempt_id"], phase=final["phase"])
            updates = {}
            if attempt["epoch"] == user["epoch"] and binding["start_role"] == "final_assessment":
                if final["active_attempt_id"] != attempt["attempt_id"] or final["phase"] == "passed":
                    raise JourneyError("INVALID_STATE")
                if row is final:
                    updates = {"phase": "free" if release else "recovery_required",
                               "active_attempt_id": None if release else attempt["attempt_id"]}
            if updates or (row is head and attempt["epoch"] == user["epoch"]):
                actions.append(self._course_put_from_row(row, updates, match))
            else:
                expression, names, values = self._match_expression(match)
                actions.append(self.state._condition({"PK": row["PK"], "SK": row["SK"]},
                                                     expression, names, values))
        return actions

    def defer_course_recovery(self, job_id, owner, fence, error_code, *, next_due_at,
                              proven_local_error=False):
        """Keep repairable evidence and schedule inspection; never infer an educational failure."""
        _integer(next_due_at)
        if error_code not in _FAILURES | {"TEMPORARILY_UNAVAILABLE", "CALCULATION_OUTCOME_UNKNOWN"}:
            raise JourneyError("INVALID_STATE")
        def build():
            snap = self.recovery_snapshot(job_id)
            job, attempt, user = snap["job"], snap["attempt"], snap["user"]
            now = self.state._now()
            self._require_unsealed(job)
            self._lease(job, owner, fence, now)
            recovery_error = ("CALCULATION_OUTCOME_UNKNOWN"
                              if job.get("error_code") == "CALCULATION_OUTCOME_UNKNOWN"
                              or job.get("state") == "outcome_unknown" else error_code)
            changed_job = {**job, "revision": job["revision"] + 1, "error_code": recovery_error}
            if proven_local_error and recovery_error != "CALCULATION_OUTCOME_UNKNOWN":
                if job["call_phase"] != "started" or job.get("candidate_ref") is not None:
                    raise JourneyError("INVALID_STATE")
                changed_job["local_error_proof"] = {"call_id": job["call_id"], "fence": fence,
                    "input_digest": job["input_digest"], "code": error_code, "stage": "calculate"}
            else:
                changed_job.update(owner=None, lease_until=0)
            self._due(changed_job, next_due_at, "JOB")
            changed_attempt = {**attempt, "state": "outcome_unknown", "error_code": recovery_error,
                "revision": attempt["revision"] + 1, "evaluation": None, "progress_application": None}
            actions = [self._job_action(job, changed_job, owner=owner, fence=fence, now=now),
                       self.state._attempt_action(attempt, changed_attempt), self.state._user_action(user)]
            actions.extend(self._recovery_course_actions(snap))
            return changed_job, actions
        return self._command(build)

    def close_course_terminal(self, job_id, owner, fence, evidence):
        from mock_journey.course_contracts import RecoveryEvidence
        if type(evidence) is not RecoveryEvidence or evidence.snapshot_json is None:
            raise JourneyError("INVALID_STATE")
        seal_digest = self._evidence_digest(evidence)
        def build():
            existing = self._job(job_id).get("terminal_seal")
            if type(existing) is dict and existing.get("evidence_digest") == seal_digest:
                return self._job_attempt(job_id)[1], None
            snap = self._verify_course_evidence(job_id, evidence, "close_terminal", owner, fence)
            job, attempt, user = snap["job"], snap["attempt"], snap["user"]
            now = self.state._now()
            self._lease(job, owner, fence, now)
            if (job.get("final_ref") is not None or attempt.get("result_ref") is not None
                    or job.get("candidate_ref") is not None or evidence.candidate_state != "absent_uncommitted"):
                raise JourneyError("INVALID_STATE")
            seal = {"job_id": job_id, "attempt_id": attempt["attempt_id"], "epoch": attempt["epoch"],
                    "call_id": job.get("call_id"), "fence": fence, "evidence_digest": seal_digest}
            updated_job = {**job, "state": "failed", "owner": None, "lease_until": 0,
                           "revision": job["revision"] + 1, "terminal_seal": seal}
            self._remove_due(updated_job)
            updated_attempt = {**attempt, "state": "failed", "evaluation": None,
                               "revision": attempt["revision"] + 1, "active_counted": False}
            actions = [self._job_action(job, updated_job, owner=owner, fence=fence, now=now),
                       self.state._attempt_action(attempt, updated_attempt),
                       self.state._user_action(user, self._close_count(attempt, user, now))]
            actions.extend(self._recovery_course_actions(snap, release=True))
            return updated_attempt, actions
        return self._command(build)

    def reopen_course_recovery(self, job_id, evidence):
        from mock_journey.course_contracts import RecoveryEvidence
        if type(evidence) is not RecoveryEvidence or evidence.snapshot_json is None:
            raise JourneyError("INVALID_STATE")
        def build():
            snap = self._verify_course_evidence(job_id, evidence, "resume_candidate", None, evidence.fence)
            job, attempt, user = snap["job"], snap["attempt"], snap["user"]
            if (job["state"] != "failed" or attempt["state"] != "failed"
                    or job.get("owner") is not None or job.get("lease_until", 0) > self.state._now()
                    or job.get("final_ref") is not None or attempt.get("result_ref") is not None):
                raise JourneyError("INVALID_STATE")
            now = self.state._now()
            changed_job = {**job, "state": "queued", "revision": job["revision"] + 1,
                           "owner": None, "lease_until": 0,
                           "recovery_from": {"state": job["state"], "error_code": job.get("error_code"),
                                             "revision": job["revision"]}}
            self._due(changed_job, now, "JOB")
            changed_attempt = {**attempt, "state": "processing", "revision": attempt["revision"] + 1}
            actions = [self._job_action(job, changed_job), self.state._attempt_action(attempt, changed_attempt),
                       self.state._user_action(user)]
            actions.extend(self._recovery_course_actions(snap))
            return changed_attempt, actions
        return self._command(build)

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

    def due_step(self, kind, *, cutoff, cursor=None):
        """One physical query; preserve its real LEK separately from base state."""
        from mock_journey.relay_progress import check_relay_call, validate_cursor
        cursor = validate_cursor(cursor, kind, cutoff)
        request = {
            "TableName": self.state.table_name, "IndexName": "GSI1", "Limit": 1,
            "ScanIndexForward": True,
            "KeyConditionExpression": "#gpk = :kind AND #gsk <= :cutoff",
            "ProjectionExpression": "#pk, #sk, #gpk, #gsk",
            "ExpressionAttributeNames": {"#pk": "PK", "#sk": "SK", "#gpk": "GSI1PK", "#gsk": "GSI1SK"},
            "ExpressionAttributeValues": _encode({":kind": "DUE#" + kind, ":cutoff": cutoff}),
        }
        if cursor is not None:
            request["ExclusiveStartKey"] = _encode(cursor)
        try:
            check_relay_call()
            response = self.state.client.query(**request)
            items = response.get("Items", [])
            if type(items) is not list or len(items) > 1:
                raise _unavailable()
            raw = validate_cursor(_decode(items[0]), kind, cutoff) if items else None
            returned = response.get("LastEvaluatedKey")
            if returned is not None and type(returned) is not dict:
                raise _unavailable()
            continuation = validate_cursor(_decode(returned), kind, cutoff) if returned else None
            # No filter, and one evaluated item. A nonempty page's continuation
            # must be that actual item, not a key guessed from its refreshed row.
            if (continuation is not None and (continuation == cursor
                    or (raw is not None and continuation != raw))):
                raise _unavailable()
            return raw, continuation
        except (ClientError, BotoCoreError, KeyError, TypeError, AttributeError):
            raise _unavailable() from None

    def due_current(self, kind, raw, *, cutoff):
        """Strong eligibility reread after the caller's post-query time check."""
        from mock_journey.relay_progress import check_relay_call, validate_cursor
        raw = validate_cursor(raw, kind, cutoff)
        if raw is None:
            raise _unavailable()
        check_relay_call()
        current = self.state._get({"PK": raw["PK"], "SK": raw["SK"]})
        if current is None:
            return None
        job_id = raw["PK"].split("#", 1)[1]
        if (current.get("PK") != raw["PK"] or current.get("SK") != raw["SK"]
                or current.get("job_id") != job_id):
            raise _unavailable()
        now = self.state._now()
        if (current.get("GSI1PK") != "DUE#" + kind
                or current.get("state") in {"done", "failed", "sent"}):
            return None
        if (_integer(current.get("next_due_at")) > now
                or _integer(current.get("lease_until")) > now):
            return None
        return current

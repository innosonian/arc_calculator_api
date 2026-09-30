"""Course view and start use-case of DynamoCourseRepository.

Split out of course_state.py (S7-04/X3-04). Each start retry re-reads the
view through the same snapshot loader as load_view and commits against those
same rows.
"""

from copy import deepcopy

from mock_journey.course_contracts import (
    POLICY_VERSION, START_KINDS, EXECUTION_KEYS,
    AttemptTemplate, CourseBinding, CourseView, PublicIds, StartCommand, StartReceipt,
    require_member, require_uuid,
)
from mock_journey.course_errors import CourseError
from mock_journey.course_policy import learner_key, placement_key, scope_key
from mock_journey.course_records import (
    _json_field, _parse_field, _rfc3339, _unavailable, scope_identity_list, start_request_digest,
)
from mock_journey.course_repo_core import SCHEMA_VERSION, CourseRepositoryCore, _check, _merge_final, _put
from mock_journey.storage_keys import (
    attempt_key, course_final_key, course_head_key, course_item_key, course_start_key, create_receipt_key,
    learner_head_key, principal_locator_key, start_locator_key,
)
from mock_journey.models import AuthContext
from mock_journey.typed import digest, json_bytes, parse_json


class StartOps(CourseRepositoryCore):
    """load_view/load_start_view, idempotent find_created and start."""

    def load_view(self, auth: AuthContext, ids: PublicIds) -> CourseView:
        session, user, now = self._session_user(auth)
        view, _, _ = self._load_snapshot(auth, user, course_id=ids.course_id, enrollment_id=ids.enrollment_id)
        if view.public_ids != ids:
            raise CourseError("NOT_FOUND")
        return view

    def load_start_view(self, auth: AuthContext, command: StartCommand) -> CourseView:
        session, user, now = self._session_user(auth)
        view, _, _ = self._load_snapshot(
            auth, user, course_id=command.course_id, enrollment_id=command.enrollment_id,
        )
        return view

    def _load_snapshot(self, auth, user, *, course_id=None, enrollment_id=None, view=None):
        """Read PRINCIPAL, LEARNER, HEAD, FINAL and the bundle blob, in that order, into a CourseView.

        Without ``view`` (load_view/load_start_view) the course is located by
        its public ids in the inventory: no learner key or assignment is
        NOT_FOUND, a missing or foreign HEAD/FINAL is TEMPORARILY_UNAVAILABLE and
        an unreadable bundle is ARC_PROGRESS_UNAVAILABLE. With ``view`` (a start
        retry re-reading the rows it will commit against) the scope comes from
        the view and the former looser rules stay as they were (D119): the
        learner key falls back to the view's learner, missing HEAD/FINAL is
        ARC_PROGRESS_UNAVAILABLE and an unreadable bundle keeps the view's bundle.
        Returns (view, head_row, final_row).
        """
        epoch = user["epoch"]
        locator = self._get(principal_locator_key(auth.principal, epoch))
        if view is None:
            learner_key_value = locator.get("learner_key") if locator is not None else None
            if type(learner_key_value) is not str:
                learner_key_value = user.get("course_learner_key")
            if type(learner_key_value) is not str:
                raise CourseError("NOT_FOUND")
        else:
            learner_key_value = (locator or {}).get("learner_key") or user.get("course_learner_key")
            if type(learner_key_value) is not str:
                learner_key_value = learner_key(view.bundle.scope.learner)
        inventory_row = self._inventory_row(learner_key_value, epoch)
        inventory = self._inventory_view(inventory_row, learner_key_value, epoch)
        if view is None:
            binding = None
            for item in inventory.assignments:
                if item.public_ids.course_id == course_id and item.public_ids.enrollment_id == enrollment_id:
                    binding = item
                    break
            if binding is None:
                raise CourseError("NOT_FOUND")
            scope_key_value = scope_key(binding.scope)
            public_ids = binding.public_ids
        else:
            scope_key_value = view.scope_key
            public_ids = view.public_ids
        head = self._get(course_head_key(scope_key_value, epoch))
        final = self._get(course_final_key(scope_key_value, epoch))
        if head is None or final is None:
            if view is None:
                _unavailable()
            raise CourseError("ARC_PROGRESS_UNAVAILABLE")
        if view is None and head.get("scope_key") != scope_key_value:
            _unavailable()
        bundle = self._load_bundle(head)
        if bundle is None:
            if view is None:
                raise CourseError("ARC_PROGRESS_UNAVAILABLE")
            bundle = view.bundle
        progress = _merge_final(_parse_field(head["progress_json"]), final)
        gate = self._gate_from_head(scope_key_value, epoch, head)
        view_scope_key = scope_key(bundle.scope) if view is None else view.scope_key
        return CourseView(view_scope_key, public_ids, bundle, json_bytes(progress), gate, inventory), head, final

    def find_created(self, auth: AuthContext, command: StartCommand, *, kind: str) -> StartReceipt | None:
        require_member(kind, START_KINDS)
        session, user, now = self._session_user(auth)
        row = self._get(create_receipt_key(auth.session_id, command.request_id))
        if row is None:
            return None
        expected = start_request_digest(command)
        if row.get("request_digest") != expected or row.get("kind") != kind:
            raise CourseError("IDEMPOTENCY_CONFLICT")
        if row.get("principal") != auth.principal or row.get("bound_session_id") != auth.session_id:
            raise CourseError("NOT_FOUND")
        return _start_receipt_from_row(row, created=False)

    def start(self, auth: AuthContext, command: StartCommand, *, kind: str, view: CourseView, template: AttemptTemplate | None):
        require_member(kind, START_KINDS)
        if kind == "content":
            if template is not None:
                raise CourseError("INVALID_REQUEST")
        elif type(template) is not AttemptTemplate:
            raise CourseError("INVALID_REQUEST")
        existing = self.find_created(auth, command, kind=kind)
        if existing is not None:
            return existing
        placement = None
        for item in view.bundle.placements:
            if item.public_link_id == command.placement_id:
                placement = item
                break
        if placement is None:
            raise CourseError("NOT_FOUND")
        if kind == "content" and placement.kind not in {"video", "document"}:
            raise CourseError("INVALID_REQUEST")
        if kind == "attempt" and placement.kind not in {"training", "assessment"}:
            raise CourseError("INVALID_REQUEST")
        last = view.bundle.placements[-1] if view.bundle.placements else None
        role = "final_assessment" if last is not None and placement.public_link_id == last.public_link_id else "training"
        original_target = (type(placement.source_id), placement.source_id, placement.kind, role)
        for retry in range(self.settings.max_conflict_retries):
            # D133: the first iteration already checked the receipt above; only a
            # retry after a lost CREATE race re-reads it (the peer's start replays).
            if retry:
                replay = self.find_created(auth, command, kind=kind)
                if replay is not None:
                    return replay
            session, user, now = self._session_user(auth)
            fresh, head, final = self._load_snapshot(auth, user, view=view)
            self.policy.can_start(fresh, command, role)
            placement = next(item for item in fresh.bundle.placements if item.public_link_id == command.placement_id)
            current_role = "final_assessment" if placement == fresh.bundle.placements[-1] else "training"
            if (type(placement.source_id), placement.source_id, placement.kind, current_role) != original_target:
                raise CourseError("DEFINITION_CHANGED")
            receipt = self._commit_start(
                auth, user, now, command, kind, fresh, placement, role, template, head, final,
            )
            if receipt is not None:
                return receipt
        raise CourseError("TEMPORARILY_UNAVAILABLE")

    def _commit_start(self, auth, user, now, command, kind, view, placement, role, template, head, final):
        epoch = user["epoch"]
        scope_key_value = view.scope_key
        place_key = placement_key(scope_key_value, placement.source_id)
        # These are exactly the rows used by can_start; never adopt a newer
        # revision after the verdict. A collision causes a complete re-read.
        inventory = view.inventory
        learner_key_value = inventory.learner_key
        item_row = self._get(course_item_key(scope_key_value, epoch, place_key))
        request_digest = start_request_digest(command)
        start_id = self._uuid() if kind == "content" else None
        attempt_id = None
        condition = None
        if kind == "attempt":
            payload = parse_json(template.existing_template_json)
            expected_binding = CourseBinding(scope_key_value, place_key, role, view.bundle.definition_hash,
                                             placement.content_version, epoch, POLICY_VERSION)
            if template.binding != expected_binding:
                raise CourseError("DEFINITION_CHANGED")
            frozen = payload.get("definition_json", payload)
            if type(frozen) is str:
                frozen = _parse_field(frozen)
            if type(frozen) is not dict or not set(EXECUTION_KEYS) <= set(frozen):
                raise CourseError("DEFINITION_CHANGED")
            if digest({key: frozen[key] for key in EXECUTION_KEYS}) != digest(parse_json(placement.execution_json)):
                raise CourseError("DEFINITION_CHANGED")
            attempt_id = require_uuid(payload.get("attempt_id") or str(self.uuid_factory()))
            if placement.execution_json is not None:
                condition = parse_json(placement.execution_json)["condition"]
        actions = self._session_user_guards(auth, user) + [
            _check(learner_head_key(learner_key_value, epoch), {
                "state": "ready", "revision": inventory.revision, "inventory_generation": inventory.generation,
            }),
        ]
        new_head = deepcopy(head)
        new_head["revision"] = head["revision"] + 1
        new_head["updated_at"] = now
        actions.append(_put(new_head, if_match={
            "revision": head["revision"], "epoch": epoch, "gate_state": "ready",
            "definition_hash": command.definition_hash,
        }))
        if kind == "content":
            start_item = {
                **course_start_key(scope_key_value, epoch, start_id),
                "schema_version": SCHEMA_VERSION,
                "start_id": start_id,
                "kind": "content",
                "content_kind": placement.kind,
                "bound_session_id": auth.session_id,
                "principal": auth.principal,
                "scope_key": scope_key_value,
                "placement_key": place_key,
                "epoch": epoch,
                "public_link_id": placement.public_link_id,
                "course_id": command.course_id,
                "enrollment_id": command.enrollment_id,
                "source_id": placement.source_id,
                "content_version": placement.content_version,
                "definition_hash": command.definition_hash,
                "content_identity_hash": digest(parse_json(placement.content_identity_json)),
                "duration_ms": placement.duration_ms,
                "merged_intervals_ms": [],
                "displayed": [],
                "pending_confirmations": [],
                "report_count": 0,
                "completed": False,
                "start_gate_revision": head["revision"],
                "revision": 0,
                "scope_identity": scope_identity_list(view.bundle.scope),
            }
            locator = {
                **start_locator_key(start_id),
                "start_id": start_id,
                "scope_key": scope_key_value,
                "epoch": epoch,
                "bound_session_id": auth.session_id,
                "principal": auth.principal,
                "placement_key": place_key,
            }
            actions.extend([_put(start_item, if_not_exists=True), _put(locator, if_not_exists=True)])
            response = {
                "startId": start_id,
                "courseId": command.course_id,
                "enrollmentId": command.enrollment_id,
                "courseItemLinkId": command.placement_id,
                "contentVersion": placement.content_version,
                "definitionHash": command.definition_hash,
            }
        else:
            attempt_item = {
                **attempt_key(attempt_id),
                "schema_version": SCHEMA_VERSION,
                "attempt_id": attempt_id,
                "principal": auth.principal,
                "bound_session_id": auth.session_id,
                "creator_session_id": auth.session_id,
                "epoch": epoch,
                "state": "created",
                "revision": 0,
                "created_at": now,
                "definition_hash": command.definition_hash,
                "content_identity_hash": digest(parse_json(placement.content_identity_json)),
                "course_binding": {
                    "scope_key": scope_key_value,
                    "placement_key": place_key,
                    "start_role": role,
                    "definition_hash": command.definition_hash,
                    "content_version": placement.content_version,
                    "epoch": epoch,
                    "policy_version": POLICY_VERSION,
                },
            }
            template_payload = parse_json(template.existing_template_json)
            for field in (
                "program_id", "target", "profile_name", "definition_json",
                "resume_nonce", "resume_key_version", "resume_digest",
                "course_id", "enrollment_id", "course_item_link_id", "active_counted",
            ):
                if field in template_payload:
                    attempt_item[field] = template_payload[field]
            if "active_counted" not in attempt_item:
                attempt_item["active_counted"] = False
            actions.append(_put(attempt_item, if_not_exists=True))
            response = {
                "attemptId": attempt_id,
                "state": "created",
                "courseId": command.course_id,
                "enrollmentId": command.enrollment_id,
                "courseItemLinkId": command.placement_id,
                "definitionHash": command.definition_hash,
                "createdAt": _rfc3339(now),
                "role": role,
                "condition": condition,
            }
        if role == "final_assessment":
            new_final = deepcopy(final)
            new_final["phase"] = "active"
            new_final["active_attempt_id"] = attempt_id
            new_final["revision"] = final["revision"] + 1
            new_final["updated_at"] = now
            # The policy verdict allowed a resting phase (free, or passed for a
            # D130 re-attempt); the commit binds exactly the phase it read.
            actions.append(_put(new_final, if_match={"revision": final["revision"], "phase": final["phase"], "epoch": epoch}))
        if placement.kind in {"training", "assessment"}:
            if item_row is None:
                item_row = {
                    **course_item_key(scope_key_value, epoch, place_key),
                    "schema_version": SCHEMA_VERSION,
                    "scope_key": scope_key_value,
                    "placement_key": place_key,
                    "epoch": epoch,
                    "source_id": placement.source_id,
                    "public_link_id": placement.public_link_id,
                    "content_version": placement.content_version,
                    "completed": False,
                    "passed": None,
                    "revision": 0,
                }
                actions.append(_put(item_row, if_not_exists=True))
            else:
                # D130: a completed item may be started again; only its revision is bound.
                actions.append(_check(course_item_key(scope_key_value, epoch, place_key), {"revision": item_row["revision"]}))
        create_row = {
            **create_receipt_key(auth.session_id, command.request_id),
            "request_digest": request_digest,
            "kind": kind,
            "principal": auth.principal,
            "bound_session_id": auth.session_id,
            "scope_key": scope_key_value,
            "placement_key": place_key,
            "definition_hash": command.definition_hash,
            "content_version": placement.content_version,
            "epoch": epoch,
            "start_id": start_id,
            "attempt_id": attempt_id,
            "response_json": _json_field(response),
        }
        actions.append(_put(create_row, if_not_exists=True))
        if not self._commit(actions):
            return None
        return StartReceipt(
            created=True,
            start_id=start_id,
            attempt_id=attempt_id,
            scope_key=scope_key_value,
            placement_key=place_key,
            definition_hash=command.definition_hash,
            content_version=placement.content_version,
            bound_session_id=auth.session_id,
            epoch=epoch,
            response_json=response,
        )


def _start_receipt_from_row(row, *, created):
    return StartReceipt(
        created=created,
        start_id=row.get("start_id"),
        attempt_id=row.get("attempt_id"),
        scope_key=row["scope_key"],
        placement_key=row["placement_key"],
        definition_hash=row["definition_hash"],
        content_version=row["content_version"],
        bound_session_id=row["bound_session_id"],
        epoch=row["epoch"],
        response_json=_parse_field(row["response_json"]),
    )

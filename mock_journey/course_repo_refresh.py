"""Course gate use-case of DynamoCourseRepository: epoch rows and refresh.

Split out of course_state.py (S7-04/X3-04); method bodies are unchanged.
"""

from copy import deepcopy

from mock_journey.course_contracts import (
    AssignmentBinding, CourseBundle, GateView, InventoryTicket, RefreshTicket, sealed_bundle,
)
from mock_journey.course_errors import CourseError
from mock_journey.course_policy import empty_progress, learner_key, placement_key, scope_key
from mock_journey.course_records import (
    _json_field, _parse_field, _unavailable, bundle_record, scope_identity_list,
)
from mock_journey.course_repo_core import SCHEMA_VERSION, CourseRepositoryCore, _check, _merge_final, _put
from mock_journey.storage_keys import course_final_key, course_head_key, learner_head_key
from mock_journey.models import AuthContext
from mock_journey.typed import digest, json_bytes, parse_json


class RefreshOps(CourseRepositoryCore):
    """ensure_epoch/begin_refresh/apply_refresh against HEAD and FINAL of the current epoch."""

    def ensure_epoch(self, auth: AuthContext, binding: AssignmentBinding) -> GateView:
        if type(binding) is not AssignmentBinding:
            raise CourseError("INVALID_REQUEST")
        if binding.scope.learner.principal != auth.principal:
            raise CourseError("NOT_FOUND")
        scope_key_value = scope_key(binding.scope)
        for _ in range(self.settings.max_conflict_retries):
            session, user, now = self._session_user(auth)
            epoch = user["epoch"]
            head = self._get(course_head_key(scope_key_value, epoch))
            final = self._get(course_final_key(scope_key_value, epoch))
            if head is None and final is None:
                head_item = {
                    **course_head_key(scope_key_value, epoch),
                    "schema_version": SCHEMA_VERSION,
                    "scope_key": scope_key_value,
                    "epoch": epoch,
                    "revision": 0,
                    "refresh_generation": 0,
                    "gate_state": "waiting",
                    "reason": "arc_progress_unavailable",
                    "definition_hash": None,
                    "bundle_ref": None,
                    "completed_placements": [],
                    "course_complete": False,
                    "progress_json": _json_field(empty_progress()),
                    "scope_identity": scope_identity_list(binding.scope),
                    "public_course_id": binding.public_ids.course_id,
                    "public_enrollment_id": binding.public_ids.enrollment_id,
                    "progress_id": binding.public_ids.progress_id,
                    "learner_key": learner_key(binding.scope.learner),
                    "reconciliation_reason": None,
                    "updated_at": now,
                }
                final_item = {
                    **course_final_key(scope_key_value, epoch),
                    "schema_version": SCHEMA_VERSION,
                    "scope_key": scope_key_value,
                    "epoch": epoch,
                    "revision": 0,
                    "phase": "free",
                    "active_attempt_id": None,
                    "passed_attempt_id": None,
                    "updated_at": now,
                }
                if self._commit(self._session_user_guards(auth, user) + [
                    _put(head_item, if_not_exists=True),
                    _put(final_item, if_not_exists=True),
                ]):
                    return GateView(scope_key_value, epoch, "waiting", "arc_progress_unavailable", 0, None)
                continue
            if head is None or final is None:
                _unavailable()
            if head.get("scope_key") != scope_key_value or final.get("scope_key") != scope_key_value:
                _unavailable()
            if head.get("scope_identity") != scope_identity_list(binding.scope):
                _unavailable()
            if head.get("epoch") != epoch or final.get("epoch") != epoch:
                _unavailable()
            return GateView(
                scope_key_value, epoch, head["gate_state"], head.get("reason"),
                head["revision"], head.get("definition_hash"),
            )
        _unavailable()

    def begin_refresh(self, auth: AuthContext, binding: AssignmentBinding, inventory: InventoryTicket) -> RefreshTicket:
        scope_key_value = scope_key(binding.scope)
        for _ in range(self.settings.max_conflict_retries):
            session, user, now = self._session_user(auth)
            if inventory.epoch != user["epoch"]:
                raise CourseError("ARC_PROGRESS_UNAVAILABLE")
            learner = self._inventory_row(inventory.learner_key, user["epoch"])
            if learner is None or learner.get("inventory_generation") != inventory.generation:
                raise CourseError("ARC_PROGRESS_UNAVAILABLE")
            head = self._get(course_head_key(scope_key_value, user["epoch"]))
            final = self._get(course_final_key(scope_key_value, user["epoch"]))
            if head is None or final is None:
                _unavailable()
            generation = head.get("refresh_generation")
            revision = head.get("revision")
            if type(generation) is not int or type(revision) is not int:
                _unavailable()
            updated = deepcopy(head)
            updated["refresh_generation"] = generation + 1
            updated["revision"] = revision + 1
            updated["updated_at"] = now
            if self._commit(self._session_user_guards(auth, user) + [
                _check(learner_head_key(inventory.learner_key, user["epoch"]), {
                    "inventory_generation": inventory.generation, "epoch": user["epoch"],
                }),
                _put(updated, if_match={"refresh_generation": generation, "revision": revision, "epoch": user["epoch"]}),
            ]):
                return RefreshTicket(
                    scope_key_value, user["epoch"], inventory.generation, generation + 1, revision + 1,
                )
        _unavailable()

    def apply_refresh(self, auth: AuthContext, ticket: RefreshTicket, bundle_or_error) -> GateView:
        for _ in range(self.settings.max_conflict_retries):
            session, user, now = self._session_user(auth)
            head = self._get(course_head_key(ticket.scope_key, user["epoch"]))
            if ticket.epoch != user["epoch"]:
                return self._gate_from_head(ticket.scope_key, user["epoch"], head)
            learner_key_value = (head or {}).get("learner_key")
            current_learner = self._inventory_row(learner_key_value, user["epoch"]) if type(learner_key_value) is str else None
            if current_learner is None or current_learner.get("inventory_generation") != ticket.inventory_generation:
                return self._gate_from_head(ticket.scope_key, user["epoch"], head)
            if head is None or head.get("refresh_generation") != ticket.generation or head.get("epoch") != ticket.epoch:
                return self._gate_from_head(ticket.scope_key, user["epoch"], head)
            # Session/USER checks are added at commit time with a fresh clock.
            guards = [
                _check(learner_head_key(learner_key_value, ticket.epoch), {
                    "inventory_generation": ticket.inventory_generation,
                    "revision": current_learner["revision"], "epoch": ticket.epoch,
                }),
            ]
            if isinstance(bundle_or_error, CourseError):
                reason = "contract_pending" if bundle_or_error.code == "CONTRACT_PENDING" else "arc_progress_unavailable"
                updated = deepcopy(head)
                updated["gate_state"] = "waiting"
                updated["reason"] = reason
                updated["revision"] = head["revision"] + 1
                updated["updated_at"] = now
                if self._commit(self._session_user_guards(auth, user) + guards + [
                    _put(updated, if_match={
                        "refresh_generation": ticket.generation, "revision": head["revision"], "epoch": ticket.epoch,
                    }),
                ]):
                    return self._gate_from_head(ticket.scope_key, ticket.epoch, updated)
                continue
            if type(bundle_or_error) is not CourseBundle:
                raise CourseError("INVALID_REQUEST")
            bundle = sealed_bundle(bundle_or_error)
            if scope_key(bundle.scope) != ticket.scope_key:
                _unavailable()
            final = self._get(course_final_key(ticket.scope_key, ticket.epoch))
            if final is None:
                _unavailable()
            guards.append(_check(course_final_key(ticket.scope_key, ticket.epoch), {
                "revision": final["revision"], "epoch": ticket.epoch,
                "phase": final["phase"], "active_attempt_id": final.get("active_attempt_id"),
            }))
            prior = self._load_bundle(head)
            progress_input = _merge_final(_parse_field(head["progress_json"]), final)
            revised_final = prior is not None and _assessment_identity(prior) != _assessment_identity(bundle)
            final_evidence = final.get("phase") != "free" or progress_input.get("passed_final") is True
            assessment_reconciliation = head.get("assessment_reconciliation_required") is True or (revised_final and final_evidence)
            progress_input["assessment_reconciliation_required"] = assessment_reconciliation
            if assessment_reconciliation:
                # Keep original ITEM/result evidence and the enrollment pass
                # lock; suppress its application to the replacement definition.
                suppressed = {placement_key(ticket.scope_key, candidate.placements[-1].source_id)
                              for candidate in (prior, bundle) if candidate and candidate.placements}
                progress_input["completed_placements"] = [key for key in progress_input.get("completed_placements", [])
                                                            if key not in suppressed]
                for key in suppressed:
                    item = (progress_input.get("items") or {}).get(key)
                    if type(item) is dict:
                        item["completed"], item["passed"] = False, None
            conflict = assessment_reconciliation or self._progress_conflict(head, bundle)
            record = json_bytes(bundle_record(bundle))
            blob_digest = self.blob_store.put_bytes(record)
            updated = deepcopy(head)
            updated["bundle_ref"] = blob_digest
            updated["definition_hash"] = bundle.definition_hash
            updated["source_revision"] = bundle.source_revision if type(bundle.source_revision) in (str, int) else None
            updated["revision"] = head["revision"] + 1
            updated["updated_at"] = now
            # A new definition must not retain a projection that claims a
            # replaced assessment was completed. Original ITEM/attempt evidence
            # remains unchanged; this HEAD is the current-definition projection.
            progress = parse_json(self.policy.aggregate_progress(bundle, json_bytes(progress_input)))
            updated["progress_json"] = _json_field(progress)
            updated["completed_placements"] = progress["completed_placements"]
            updated["course_complete"] = progress["course_complete"]
            updated["assessment_reconciliation_required"] = assessment_reconciliation
            if conflict:
                updated["gate_state"] = "reconciliation_required"
                updated["reason"] = "progress_reconciliation_required"
                updated["reconciliation_reason"] = "progress_reconciliation_required"
                updated["source_progress_ref"] = self.blob_store.put_bytes(bundle.source_progress_json)
            else:
                updated["gate_state"] = "ready"
                updated["reason"] = None
                updated["reconciliation_reason"] = None
            if self._commit(self._session_user_guards(auth, user) + guards + [
                _put(updated, if_match={
                    "refresh_generation": ticket.generation, "revision": head["revision"], "epoch": ticket.epoch,
                }),
            ]):
                return self._gate_from_head(ticket.scope_key, ticket.epoch, updated)
        _unavailable()

    def _progress_conflict(self, head, bundle: CourseBundle) -> bool:
        # Only the explicit internal fixture has a known empty-progress contract.
        # Official ARC progress mapping is gated; neither null nor a plausible key
        # list establishes its meaning or permits silently discarding remote work.
        source = parse_json(bundle.source_progress_json)
        return not (type(source) is dict and source.get("synthetic") is True
                    and "progress" in source and source["progress"] is None)


def _assessment_identity(bundle):
    if not bundle.placements:
        return None
    item = bundle.placements[-1]
    return (type(item.source_id), item.source_id, item.kind, item.content_version,
            digest(parse_json(item.content_identity_json)),
            None if item.execution_json is None else digest(parse_json(item.execution_json)))

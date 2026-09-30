"""Learner inventory use-case of DynamoCourseRepository: reserve, apply and load.

Split out of course_state.py (S7-04/X3-04); method bodies are unchanged.
"""

from copy import deepcopy

from mock_journey.course_contracts import InventoryTicket, InventoryView, LearnerContext
from mock_journey.course_errors import CourseError
from mock_journey.course_policy import learner_key
from mock_journey.course_records import _unavailable, assignment_record
from mock_journey.course_repo_core import SCHEMA_VERSION, CourseRepositoryCore, _put
from mock_journey.storage_keys import learner_head_key, principal_locator_key
from mock_journey.models import AuthContext


class InventoryOps(CourseRepositoryCore):
    """begin/apply/load inventory. Each retry re-reads SESSION, USER and the learner row."""

    def begin_inventory(self, auth: AuthContext, learner: LearnerContext) -> InventoryTicket:
        if type(learner) is not LearnerContext:
            raise CourseError("INVALID_REQUEST")
        if learner.principal != auth.principal:
            raise CourseError("NOT_FOUND")
        key_value = learner_key(learner)
        for _ in range(self.settings.max_conflict_retries):
            session, user, now = self._session_user(auth)
            epoch = user["epoch"]
            row = self._get(learner_head_key(key_value, epoch))
            locator = {
                **principal_locator_key(auth.principal, epoch),
                "principal": auth.principal,
                "epoch": epoch,
                "learner_key": key_value,
            }
            if row is None:
                item = {
                    **learner_head_key(key_value, epoch),
                    "schema_version": SCHEMA_VERSION,
                    "learner_key": key_value,
                    "epoch": epoch,
                    "revision": 1,
                    "inventory_generation": 1,
                    "state": "waiting",
                    "reason": "arc_progress_unavailable",
                    "assignments": [],
                    "provider": learner.provider,
                    "tenant_id": learner.tenant_id,
                    "learner_id": learner.learner_id,
                    "principal": learner.principal,
                    "is_dummy": learner.is_dummy,
                    "updated_at": now,
                }
                if self._commit(self._session_user_guards(auth, user) + [
                    _put(item, if_not_exists=True),
                    _put(locator, if_missing_or_match={"epoch": epoch, "learner_key": key_value}),
                ]):
                    return InventoryTicket(key_value, epoch, 1)
                continue
            if row.get("learner_key") != key_value or row.get("epoch") != epoch:
                _unavailable()
            generation = row.get("inventory_generation")
            revision = row.get("revision")
            if type(generation) is not int or type(revision) is not int:
                _unavailable()
            updated = deepcopy(row)
            updated["inventory_generation"] = generation + 1
            updated["revision"] = revision + 1
            updated["updated_at"] = now
            if self._commit(self._session_user_guards(auth, user) + [
                _put(updated, if_match={"inventory_generation": generation, "revision": revision, "epoch": epoch}),
                _put(locator, if_missing_or_match={"epoch": epoch, "learner_key": key_value}),
            ]):
                return InventoryTicket(key_value, epoch, generation + 1)
        _unavailable()

    def begin_inventory_for_session(self, auth: AuthContext) -> InventoryTicket | None:
        """Reserve refresh order before upstream resolution using the stored identity."""
        _, user, _ = self._session_user(auth)
        locator = self._get(principal_locator_key(auth.principal, user["epoch"]))
        if locator is None:
            return None
        key_value = locator.get("learner_key")
        if type(key_value) is not str:
            _unavailable()
        row = self._inventory_row(key_value, user["epoch"])
        if row is None:
            _unavailable()
        learner = LearnerContext(**{key: row.get(key) for key in (
            "provider", "tenant_id", "learner_id", "principal", "is_dummy",
        )})
        if learner.principal != auth.principal or learner_key(learner) != key_value:
            _unavailable()
        return self.begin_inventory(auth, learner)

    def apply_inventory(self, auth: AuthContext, ticket: InventoryTicket, result_or_error):
        for _ in range(self.settings.max_conflict_retries):
            session, user, now = self._session_user(auth)
            current = self._inventory_row(ticket.learner_key, user["epoch"])
            if ticket.epoch != user["epoch"] or current is None or current.get("inventory_generation") != ticket.generation:
                row = current if ticket.epoch == user["epoch"] else self._inventory_row(ticket.learner_key, user["epoch"])
                return self._inventory_view(row, ticket.learner_key, user["epoch"])
            if isinstance(result_or_error, CourseError):
                reason = "contract_pending" if result_or_error.code == "CONTRACT_PENDING" else "arc_progress_unavailable"
                updated = deepcopy(current)
                updated["state"] = "waiting"
                updated["reason"] = reason
                updated["revision"] = current["revision"] + 1
                updated["updated_at"] = now
            else:
                if type(result_or_error) is not tuple:
                    raise CourseError("INVALID_REQUEST")
                records = [assignment_record(item) for item in result_or_error]
                if len(records) > self.settings.max_assignments:
                    raise CourseError("UPSTREAM_CONTRACT_MISMATCH")
                updated = deepcopy(current)
                updated["state"] = "ready"
                updated["reason"] = None
                updated["assignments"] = records
                updated["revision"] = current["revision"] + 1
                updated["updated_at"] = now
            if self._commit(self._session_user_guards(auth, user) + [
                _put(updated, if_match={
                    "inventory_generation": ticket.generation, "revision": current["revision"], "epoch": ticket.epoch,
                }),
            ]):
                return self._inventory_view(updated, ticket.learner_key, ticket.epoch)
        _unavailable()

    def load_inventory(self, auth: AuthContext, learner: LearnerContext) -> InventoryView:
        """GET path loader. Never begins inventory or provider refresh."""
        if type(learner) is not LearnerContext:
            raise CourseError("INVALID_REQUEST")
        if learner.principal != auth.principal:
            raise CourseError("NOT_FOUND")
        _, user, _ = self._session_user(auth)
        key_value = learner_key(learner)
        row = self._inventory_row(key_value, user["epoch"])
        return self._inventory_view(row, key_value, user["epoch"])

    def load_inventory_for_session(self, auth: AuthContext) -> InventoryView:
        """GET loader from stored principal locator. Does not call a provider."""
        _, user, _ = self._session_user(auth)
        locator = self._get(principal_locator_key(auth.principal, user["epoch"]))
        if locator is None or type(locator.get("learner_key")) is not str:
            raise CourseError("ARC_PROGRESS_UNAVAILABLE")
        key_value = locator["learner_key"]
        row = self._inventory_row(key_value, user["epoch"])
        return self._inventory_view(row, key_value, user["epoch"])

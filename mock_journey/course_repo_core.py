"""Shared base of DynamoCourseRepository (S7-04/X3-04 split of course_state.py).

The constructor and the injected attributes every use-case mixin relies on,
the CourseStore/CourseBlobStore protocols, clock/UUID reads, guarded
get/commit, the session/USER checks every commit carries, the action builders
and the snapshot readers. Row keys come from mock_journey.storage_keys. Inject a
CourseStore; do not import state.py helpers.
"""

from copy import deepcopy
from typing import Protocol

from mock_journey.course_contracts import CourseBundle, GateView, InventoryView, require_uuid
from mock_journey.course_errors import CourseError
from mock_journey.course_policy import CoursePolicy
from mock_journey.course_primitives import require_text
from mock_journey.course_records import _unavailable, assignment_from_record, bundle_from_record
from mock_journey.course_settings import CourseSettings
from mock_journey.storage_keys import learner_head_key, session_key, user_key
from mock_journey.typed import parse_json


SCHEMA_VERSION = 1
DYNAMODB_ITEM_MAX_BYTES = 400 * 1024


class CourseStore(Protocol):
    """Dynamo-like get/transact without copying state.py private encode/transact.

    get_item(key): consistent read of one {"PK", "SK"} row as Python types
    (never AttributeValue maps); a missing row is None. transact(actions):
    all-or-nothing; True committed, False condition/transaction conflict
    (retry), other failures CourseError TEMPORARILY_UNAVAILABLE. Actions are
    op=put (if_not_exists / if_match / if_missing_or_match) or
    op=condition_check (if_match, optional integer if_greater); one action per
    key and at most CourseSettings.max_transaction_actions. The DynamoDB mapping
    is state.DynamoCourseStore. (The former W5 work-order text is kept in
    tests/vcc_hook_requests.py.)
    """

    def get_item(self, key: dict) -> dict | None: ...
    def transact(self, actions: list) -> bool: ...


class CourseBlobStore(Protocol):
    """Existing private object storage: immutable bytes in, sha256 hex out; no HTTP.

    put_bytes runs before the control transaction; a failed commit leaves an
    unreferenced object that is never deleted automatically.
    """

    def put_bytes(self, body: bytes) -> str: ...
    def get_bytes(self, digest: str) -> bytes | None: ...


def _action_key(action):
    if action["op"] == "put":
        return (action["item"]["PK"], action["item"]["SK"])
    key = action["key"]
    return (key["PK"], key["SK"])


def _put(item, *, if_not_exists=False, if_match=None, if_missing_or_match=None):
    action = {"op": "put", "key": {"PK": item["PK"], "SK": item["SK"]}, "item": item}
    if if_not_exists:
        action["if_not_exists"] = True
    if if_match is not None:
        action["if_match"] = if_match
    if if_missing_or_match is not None:
        action["if_missing_or_match"] = if_missing_or_match
    return action


def _check(key, if_match, *, if_greater=None):
    action = {"op": "condition_check", "key": key, "if_match": if_match}
    if if_greater is not None:
        action["if_greater"] = if_greater
    return action


class CourseRepositoryCore:
    """Shared reads, guards and commit for the course repository use-case mixins.

    The attributes the mixins use are all set here. Every use-case reads its
    snapshot and builds its conditions within one retry iteration
    (ARCHITECTURE §8-6); these helpers never cache rows between calls.
    """

    store: CourseStore
    settings: CourseSettings
    policy: CoursePolicy
    blob_store: CourseBlobStore

    def __init__(
        self, store: CourseStore, settings: CourseSettings, policy: CoursePolicy, blob_store: CourseBlobStore,
        *, clock, uuid_factory,
    ):
        if type(settings) is not CourseSettings or type(policy) is not CoursePolicy:
            raise TypeError("Course repository requires CourseSettings and CoursePolicy.")
        self.store = store
        self.settings = settings
        self.policy = policy
        self.blob_store = blob_store
        self.clock = clock
        self.uuid_factory = uuid_factory

    def _now(self):
        value = self.clock()
        if type(value) is float:
            value = int(value)
        if type(value) is not int:
            _unavailable()
        return value

    def _uuid(self):
        return require_uuid(str(self.uuid_factory()))

    def _get(self, key):
        try:
            item = self.store.get_item(key)
        except CourseError:
            raise
        except Exception:
            _unavailable()
        return deepcopy(item) if item is not None else None

    def _commit(self, actions):
        keys = [_action_key(action) for action in actions]
        if len(keys) != len(set(keys)):
            _unavailable()
        if len(actions) > self.settings.max_transaction_actions:
            _unavailable()
        if not actions:
            return True
        try:
            return self.store.transact(actions)
        except CourseError:
            raise
        except Exception:
            _unavailable()

    def _check_session(self, session, auth, now):
        if not session or session.get("session_id") != auth.session_id or session.get("principal") != auth.principal:
            raise CourseError("SESSION_REQUIRED")
        if session.get("status") == "revoked":
            raise CourseError("SESSION_REVOKED")
        if session.get("status") != "active":
            raise CourseError("SESSION_REQUIRED")
        expires_at = session.get("expires_at")
        revision = session.get("revision")
        if type(expires_at) is not int or type(revision) is not int:
            _unavailable()
        if expires_at <= now:
            raise CourseError("SESSION_EXPIRED")
        if revision != auth.revision or expires_at != auth.expires_at:
            raise CourseError("SESSION_REQUIRED")

    def _require_user(self, user, principal):
        if not user or user.get("principal") != principal or type(user.get("revision")) is not int:
            _unavailable()
        require_text(user.get("epoch"), code="TEMPORARILY_UNAVAILABLE", check_utf8=False)
        return user

    def _session_user(self, auth):
        now = self._now()
        session = self._get(session_key(auth.session_id))
        user = self._get(user_key(auth.principal))
        self._check_session(session, auth, now)
        return session, self._require_user(user, auth.principal), now

    def _session_user_guards(self, auth, user):
        """Session and USER commit checks with v1 session expiry parity.

        The clock is read again at commit time, as v1 _session_condition does.
        A session that expired after the read is refused before the write, and
        the stored expires_at must still be later than that time at commit.
        """
        now = self._now()
        # _check_session already bound the stored expires_at to auth.expires_at.
        if auth.expires_at <= now:
            raise CourseError("SESSION_EXPIRED")
        return [
            _check(session_key(auth.session_id), {
                "status": "active", "revision": auth.revision, "principal": auth.principal,
            }, if_greater={"expires_at": now}),
            _check(user_key(auth.principal), {"epoch": user["epoch"], "revision": user["revision"]}),
        ]

    def _inventory_row(self, learner_key_value, epoch):
        row = self._get(learner_head_key(learner_key_value, epoch))
        if row is None:
            return None
        if row.get("learner_key") != learner_key_value or row.get("epoch") != epoch:
            _unavailable()
        return row

    def _inventory_view(self, row, learner_key_value, epoch) -> InventoryView:
        if row is None:
            return InventoryView(learner_key_value, epoch, 0, 0, "waiting", "arc_progress_unavailable", ())
        assignments = tuple(assignment_from_record(item) for item in (row.get("assignments") or []))
        return InventoryView(
            learner_key_value, epoch, row["inventory_generation"], row["revision"],
            row["state"], row.get("reason"), assignments,
        )

    def _gate_from_head(self, scope_key_value, epoch, head) -> GateView:
        if head is None:
            return GateView(scope_key_value, epoch, "waiting", "arc_progress_unavailable", 0, None)
        return GateView(
            scope_key_value, epoch, head["gate_state"], head.get("reason"),
            head["revision"], head.get("definition_hash"),
        )

    def _load_bundle(self, head) -> CourseBundle | None:
        ref = head.get("bundle_ref")
        if type(ref) is not str or not ref:
            return None
        body = self.blob_store.get_bytes(ref)
        if body is None:
            _unavailable()
        return bundle_from_record(parse_json(body) if type(body) is bytes else body)


def _merge_final(progress, final):
    payload = dict(progress)
    block = dict(payload.get("final") or {})
    block["phase"] = final.get("phase") or block.get("phase") or "free"
    block["active_attempt_id"] = final.get("active_attempt_id")
    block["passed_attempt_id"] = final.get("passed_attempt_id")
    for key in ("passed_placement_key", "passed_definition_hash", "passed_content_version"):
        if key in final:
            block[key] = final[key]
    payload["final"] = block
    if final.get("phase") == "passed":
        payload["passed_final"] = True
    return payload

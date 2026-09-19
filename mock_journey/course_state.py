"""Epoch-scoped course control rows. Inject a CourseStore; do not import state.py helpers."""

from copy import deepcopy
from datetime import datetime, timezone
from typing import Protocol
import hashlib

from mock_journey.course_contracts import (
    POLICY_VERSION, START_KINDS, EXECUTION_KEYS,
    AssignmentBinding, AttemptTemplate, ContentReport, CourseBinding, CourseBundle, CourseScope, CourseView,
    GateView, InventoryTicket, InventoryView, LearnerContext, Placement, PublicIds, RefreshTicket,
    StartCommand, StartReceipt, StoredProgressReceipt,
    require_member, require_uuid, sealed_bundle,
)
from mock_journey.course_errors import CourseError
from mock_journey.course_policy import (
    CoursePolicy, empty_progress, learner_key, merge_intervals, placement_key, scope_key,
)
from mock_journey.course_settings import CourseSettings
from mock_journey.models import AuthContext
from mock_journey.typed import canonical_bytes, digest, json_bytes, parse_json


SCHEMA_VERSION = 1
DYNAMODB_ITEM_MAX_BYTES = 400 * 1024

HOOK_REQUESTS = """
W5 hook contract (vcc-internal-v1 / vcc-policy-v1). W5 owns integration_tests/test_vcc_*.py
and the mapping onto DynamoDB TransactWriteItems / get_item. Do not copy state.py _encode/_write.

CourseStore.get_item(key: {'PK': str, 'SK': str}) -> dict | None
  Consistent read of one control row. Python types (str/int/bool/list/dict/None), never Dynamo
  AttributeValue wrapping. Missing row is None. SDK/integrity failure -> CourseError TEMPORARILY_UNAVAILABLE.

CourseStore.transact(actions: list[dict]) -> bool
  Atomic write. True = all actions committed. False = condition or transaction conflict (retry).
  Other failures -> CourseError TEMPORARILY_UNAVAILABLE. Never partial apply.
  Duplicate PK+SK in one list is forbidden (repository refuses before this call).
  len(actions) <= CourseSettings.max_transaction_actions (fixture 20).
  W5 maps op=put to TransactWrite Put and op=condition_check to ConditionCheck.
  Conditions:
    if_not_exists: attribute_not_exists(PK)
    if_match: AND of #field = :value for each pair (merge into the Put; never extra Check on same key)
    if_missing_or_match: attribute_not_exists(PK) OR (all match pairs)
  Session/USER rows keep existing PK/SK (SESSION#id/AUTH, USER#principal/STATE).

CourseBlobStore.put_bytes(body: bytes) -> str
  Immutable private object, return sha256 hex. Put BEFORE the control transact. Failed commit
  leaves an unreferenced object; do not auto-delete (T4). get_bytes(digest) -> bytes | None.

ensure_epoch(auth, binding) -> GateView
  Read USER epoch + COURSE HEAD + FINAL for that epoch. Create HEAD(waiting, empty completions)
  and FINAL(free) together. Both present: verify original IDs/hash, no-op. One missing: 503.
  ITEM is not created. Errors: SESSION_*, TEMPORARILY_UNAVAILABLE.

begin_inventory(auth, learner) -> InventoryTicket
  CAS inventory_generation += 1 on COURSE_LEARNER#learner_key / EPOCH#epoch#HEAD.
  Missing learner HEAD: create generation=1 waiting. Failure: SESSION_*, 503.

apply_inventory(auth, ticket, result_or_error) -> InventoryView
  Apply only ticket.generation == current and ticket.epoch == USER epoch. Stale success/fail: no-op.
  CourseError: learner waiting (arc_progress_unavailable or contract_pending); do not fan-out
  delete course rows. tuple[AssignmentBinding]: ready, including empty (V06). Parent of refresh.

begin_refresh(auth, binding, inventory: InventoryTicket) -> RefreshTicket
  After ensure_epoch. CAS HEAD.refresh_generation += 1. Keep ready until apply confirms failure.
  Ticket stores parent inventory_generation. Uninitialized stays waiting.

apply_refresh(auth, ticket, bundle_or_error) -> GateView
  Match ticket.generation, USER epoch, parent inventory_generation. Parent generation change: no-op.
  CourseError: waiting. CourseBundle full reconcile success: ready + bundle_ref. Value conflict
  (source_progress vs local completed set): reconciliation_required, keep local evidence.

load_view(auth, ids) / load_start_view(auth, command) -> CourseView
  Auth + ownership + digest re-check. No enrollment/calc/submit writes.

find_created(auth, command, *, kind) -> StartReceipt | None
  SESSION#id / COURSE_CREATE#request_id (not CREATE#). Current auth+ownership+digest first.
  Same digest: stored receipt. Different body: IDEMPOTENCY_CONFLICT. Do not gate-block replay.

start(auth, command, *, kind, view, template) -> StartReceipt
  Replay COURSE_CREATE before can_start. New start CAS: session/user/learner ready+assignment,
  HEAD revision/epoch/ready/hash, training ITEM missing-or-not-complete, FINAL free when
  role=final_assessment. content: START+locator, start_id only. attempt: ATTEMPT, attempt_id only.
  Max 4 conflicts then TEMPORARILY_UNAVAILABLE. No partial commit.

report(...) -> StoredProgressReceipt
  Locator COURSE_START#start_id/META then original-epoch START. Bound session, version, digest.
  Same digest: immutable stored receipt. Different body: 409. historical_only when START.epoch !=
  USER epoch (isCompleted/isPassed/courseStatus all null; no current HEAD/ITEM/FINAL writes).
  Else REPORT+START+ITEM+HEAD atomic. Gate waiting still allows existing start reports.

Race schedules for integration_tests (W5):
  V06 inventory q1-success after q2-fail; refresh q1-success after q2-fail; parent inventory
  generation bump then stale bundle; inventory fail vs new start on same learner CAS.
  V07 create commit, drop response, set gate waiting, replay same session/id/body.
  V08 HEAD+FINAL init vs one-row corruption 503; real-student logout keeps epoch.
  V10 two incomplete training starts allowed; complete-then-start 409; start-then-complete keeps attempt.
  V11 two final starts, one wins FINAL.phase=active.
  V16/V17/V20 previous-epoch report/finalize does not touch current HEAD/ITEM/FINAL.
  V22 4 conflicts -> 503 and write count 0; action keys unique; actions <= 20.
"""


class CourseStore(Protocol):
    """W5 hook: Dynamo-like get/transact without copying state.py private encode/transact."""

    def get_item(self, key: dict) -> dict | None: ...
    def transact(self, actions: list) -> bool: ...


class CourseBlobStore(Protocol):
    """W5 hook: existing private object storage. Digest in, bytes out. No HTTP."""

    def put_bytes(self, body: bytes) -> str: ...
    def get_bytes(self, digest: str) -> bytes | None: ...


class InMemoryBlobStore:
    """Unit-test blob hook. W5 replaces this with JourneyStorage."""

    def __init__(self):
        self.objects = {}

    def put_bytes(self, body: bytes) -> str:
        if type(body) is not bytes:
            raise CourseError("TEMPORARILY_UNAVAILABLE")
        digest_value = hashlib.sha256(body).hexdigest()
        self.objects[digest_value] = body
        return digest_value

    def get_bytes(self, digest_value: str):
        return self.objects.get(digest_value)


class InMemoryCourseStore:
    """Atomic fake for CourseStore.action shapes. No sleep; conflicts are explicit."""

    def __init__(self):
        self.items = {}
        self.calls = []
        self.conflict_remaining = 0
        self.always_conflict = False

    def seed(self, item):
        self.items[(item["PK"], item["SK"])] = deepcopy(item)

    def get_item(self, key: dict):
        item = self.items.get((key["PK"], key["SK"]))
        return deepcopy(item) if item is not None else None

    def transact(self, actions: list) -> bool:
        self.calls.append(deepcopy(actions))
        if self.always_conflict:
            return False
        if self.conflict_remaining > 0:
            self.conflict_remaining -= 1
            return False
        snapshot = deepcopy(self.items)
        writes = []
        for action in actions:
            key = (action["key"]["PK"], action["key"]["SK"])
            current = snapshot.get(key)
            if not _action_allowed(current, action):
                return False
            if action["op"] == "put":
                writes.append((key, deepcopy(action["item"])))
        for key, item in writes:
            snapshot[key] = item
        self.items = snapshot
        return True


def _action_allowed(current, action):
    match = action.get("if_match")
    missing_or = action.get("if_missing_or_match")
    if action.get("if_not_exists"):
        return current is None
    if missing_or is not None:
        if current is None:
            return True
        return _fields_match(current, missing_or)
    if match is not None:
        if current is None:
            return False
        return _fields_match(current, match)
    if action["op"] == "condition_check":
        return current is not None
    return current is not None or action["op"] == "put"


def _fields_match(item, expected):
    for key, value in expected.items():
        if item.get(key) != value:
            return False
    return True


def _unavailable():
    raise CourseError("TEMPORARILY_UNAVAILABLE")


def _session_key(session_id):
    return {"PK": f"SESSION#{session_id}", "SK": "AUTH"}


def _user_key(principal):
    return {"PK": f"USER#{principal}", "SK": "STATE"}


def _learner_key_row(learner_key_value, epoch):
    return {"PK": f"COURSE_LEARNER#{learner_key_value}", "SK": f"EPOCH#{epoch}#HEAD"}


def _head_key(scope_key_value, epoch):
    return {"PK": f"COURSE#{scope_key_value}", "SK": f"EPOCH#{epoch}#HEAD"}


def _final_key(scope_key_value, epoch):
    return {"PK": f"COURSE#{scope_key_value}", "SK": f"EPOCH#{epoch}#FINAL"}


def _item_key(scope_key_value, epoch, placement_key_value):
    return {"PK": f"COURSE#{scope_key_value}", "SK": f"EPOCH#{epoch}#ITEM#{placement_key_value}"}


def _start_key(scope_key_value, epoch, start_id):
    return {"PK": f"COURSE#{scope_key_value}", "SK": f"EPOCH#{epoch}#START#{start_id}"}


def _report_key(scope_key_value, epoch, start_id, report_id):
    return {"PK": f"COURSE#{scope_key_value}", "SK": f"EPOCH#{epoch}#REPORT#{start_id}#{report_id}"}


def _locator_key(start_id):
    return {"PK": f"COURSE_START#{start_id}", "SK": "META"}


def _create_key(session_id, request_id):
    return {"PK": f"SESSION#{session_id}", "SK": f"COURSE_CREATE#{request_id}"}


def _attempt_key(attempt_id):
    return {"PK": f"ATTEMPT#{attempt_id}", "SK": "META"}


def _principal_key(principal, epoch):
    return {"PK": f"COURSE_PRINCIPAL#{principal}", "SK": f"EPOCH#{epoch}"}


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


def _check(key, if_match):
    return {"op": "condition_check", "key": key, "if_match": if_match}


def _json_field(value):
    return json_bytes(value).decode("utf-8")


def _parse_field(value):
    if type(value) is bytes:
        return parse_json(value)
    if type(value) is str:
        return parse_json(value.encode("utf-8"))
    if type(value) in (dict, list):
        return parse_json(json_bytes(value))
    _unavailable()


def _rfc3339(seconds):
    return datetime.fromtimestamp(int(seconds), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def start_request_digest(command: StartCommand) -> str:
    return digest({
        "clientRequestId": command.request_id,
        "courseId": command.course_id,
        "enrollmentId": command.enrollment_id,
        "courseItemLinkId": command.placement_id,
        "definitionHash": command.definition_hash,
    })


def report_request_digest(course_id, enrollment_id, placement_id, report: ContentReport) -> str:
    event = {"type": report.event_type}
    if report.event_type == "video_segments":
        event["intervalsMs"] = [list(pair) for pair in report.intervals_ms]
    elif report.event_type == "document_confirmed":
        event["displayReportId"] = report.display_report_id
    return digest({
        "courseId": course_id,
        "enrollmentId": enrollment_id,
        "courseItemLinkId": placement_id,
        "startId": report.start_id,
        "reportId": report.report_id,
        "contentVersion": report.content_version,
        "event": event,
    })


def bundle_record(bundle: CourseBundle) -> dict:
    return {
        "scope": {
            "learner": {
                "provider": bundle.scope.learner.provider,
                "tenant_id": bundle.scope.learner.tenant_id,
                "learner_id": bundle.scope.learner.learner_id,
                "principal": bundle.scope.learner.principal,
                "is_dummy": bundle.scope.learner.is_dummy,
            },
            "enrollment_id": bundle.scope.enrollment_id,
            "course_id": bundle.scope.course_id,
        },
        "public_ids": {
            "course_id": bundle.public_ids.course_id,
            "enrollment_id": bundle.public_ids.enrollment_id,
            "progress_id": bundle.public_ids.progress_id,
        },
        "source_revision": bundle.source_revision,
        "mapping_version": bundle.mapping_version,
        "definition_hash": bundle.definition_hash,
        "placements": [_placement_record(item) for item in bundle.placements],
        "course_json": parse_json(bundle.course_json),
        "source_progress_json": parse_json(bundle.source_progress_json),
    }


def _placement_record(item: Placement) -> dict:
    return {
        "source_id": item.source_id,
        "public_link_id": item.public_link_id,
        "public_item_id": item.public_item_id,
        "position": item.position,
        "kind": item.kind,
        "content_version": item.content_version,
        "content_identity": parse_json(item.content_identity_json),
        "detail": parse_json(item.detail_json),
        "execution": None if item.execution_json is None else parse_json(item.execution_json),
        "execution_status": item.execution_status,
        "duration_ms": item.duration_ms,
    }


def bundle_from_record(record) -> CourseBundle:
    payload = record if type(record) is dict else _parse_field(record)
    learner = payload["scope"]["learner"]
    scope = CourseScope(
        LearnerContext(
            learner["provider"], learner["tenant_id"], learner["learner_id"],
            learner["principal"], learner["is_dummy"],
        ),
        payload["scope"]["enrollment_id"], payload["scope"]["course_id"],
    )
    public = payload["public_ids"]
    placements = []
    for item in payload["placements"]:
        execution = item["execution"]
        placements.append(Placement(
            source_id=item["source_id"],
            public_link_id=item["public_link_id"],
            public_item_id=item["public_item_id"],
            position=item["position"],
            kind=item["kind"],
            content_version=item["content_version"],
            content_identity_json=item["content_identity"],
            detail_json=item["detail"],
            execution_json=execution,
            execution_status=item["execution_status"],
            duration_ms=item["duration_ms"],
        ))
    return sealed_bundle(CourseBundle(
        scope=scope,
        public_ids=PublicIds(public["course_id"], public["enrollment_id"], public["progress_id"]),
        source_revision=payload["source_revision"],
        mapping_version=payload["mapping_version"],
        definition_hash=payload["definition_hash"],
        placements=tuple(placements),
        course_json=payload["course_json"],
        source_progress_json=payload["source_progress_json"],
    ))


def assignment_record(binding: AssignmentBinding) -> dict:
    learner = binding.scope.learner
    return {
        "provider": learner.provider,
        "tenant_id": learner.tenant_id,
        "learner_id": learner.learner_id,
        "principal": learner.principal,
        "is_dummy": learner.is_dummy,
        "enrollment_id": binding.scope.enrollment_id,
        "course_id": binding.scope.course_id,
        "public_course_id": binding.public_ids.course_id,
        "public_enrollment_id": binding.public_ids.enrollment_id,
        "progress_id": binding.public_ids.progress_id,
        "scope_identity": scope_identity_list(binding.scope),
        "scope_key": scope_key(binding.scope),
        "learner_key": learner_key(learner),
    }


def scope_identity_list(scope: CourseScope):
    return [
        scope.learner.provider, scope.learner.tenant_id, scope.learner.learner_id,
        scope.enrollment_id, scope.course_id,
    ]


def assignment_from_record(record) -> AssignmentBinding:
    stored_key = record.get("scope_key")
    learner = LearnerContext(
        record["provider"], record["tenant_id"], record["learner_id"],
        record["principal"], record["is_dummy"],
    )
    scope = CourseScope(learner, record["enrollment_id"], record["course_id"])
    computed = scope_key(scope)
    if stored_key is not None and stored_key != computed:
        _unavailable()
    if record.get("learner_key") not in (None, learner_key(learner)):
        _unavailable()
    return AssignmentBinding(
        scope,
        PublicIds(record["public_course_id"], record["public_enrollment_id"], record["progress_id"]),
    )


class DynamoCourseRepository:
    """T4 course repository. Clock and UUID factories are keyword-only injections."""

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
        epoch = user.get("epoch")
        if type(epoch) is not str or not epoch:
            _unavailable()
        return user

    def _session_user(self, auth):
        now = self._now()
        session = self._get(_session_key(auth.session_id))
        user = self._get(_user_key(auth.principal))
        self._check_session(session, auth, now)
        return session, self._require_user(user, auth.principal), now

    def begin_inventory(self, auth: AuthContext, learner: LearnerContext) -> InventoryTicket:
        if type(learner) is not LearnerContext:
            raise CourseError("INVALID_REQUEST")
        if learner.principal != auth.principal:
            raise CourseError("NOT_FOUND")
        key_value = learner_key(learner)
        for _ in range(self.settings.max_conflict_retries):
            session, user, now = self._session_user(auth)
            epoch = user["epoch"]
            row = self._get(_learner_key_row(key_value, epoch))
            locator = {
                **_principal_key(auth.principal, epoch),
                "principal": auth.principal,
                "epoch": epoch,
                "learner_key": key_value,
            }
            if row is None:
                item = {
                    **_learner_key_row(key_value, epoch),
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
                if self._commit([
                    _check(_session_key(auth.session_id), {"status": "active", "revision": auth.revision, "principal": auth.principal}),
                    _check(_user_key(auth.principal), {"epoch": epoch, "revision": user["revision"]}),
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
            if self._commit([
                _check(_session_key(auth.session_id), {"status": "active", "revision": auth.revision, "principal": auth.principal}),
                _check(_user_key(auth.principal), {"epoch": epoch, "revision": user["revision"]}),
                _put(updated, if_match={"inventory_generation": generation, "revision": revision, "epoch": epoch}),
                _put(locator, if_missing_or_match={"epoch": epoch, "learner_key": key_value}),
            ]):
                return InventoryTicket(key_value, epoch, generation + 1)
        _unavailable()

    def begin_inventory_for_session(self, auth: AuthContext) -> InventoryTicket | None:
        """Reserve refresh order before upstream resolution using the stored identity."""
        _, user, _ = self._session_user(auth)
        locator = self._get(_principal_key(auth.principal, user["epoch"]))
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
            if self._commit([
                _check(_session_key(auth.session_id), {"status": "active", "revision": auth.revision, "principal": auth.principal}),
                _check(_user_key(auth.principal), {"epoch": user["epoch"], "revision": user["revision"]}),
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
        locator = self._get(_principal_key(auth.principal, user["epoch"]))
        if locator is None or type(locator.get("learner_key")) is not str:
            raise CourseError("ARC_PROGRESS_UNAVAILABLE")
        key_value = locator["learner_key"]
        row = self._inventory_row(key_value, user["epoch"])
        return self._inventory_view(row, key_value, user["epoch"])

    def _inventory_row(self, learner_key_value, epoch):
        row = self._get(_learner_key_row(learner_key_value, epoch))
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

    def ensure_epoch(self, auth: AuthContext, binding: AssignmentBinding) -> GateView:
        if type(binding) is not AssignmentBinding:
            raise CourseError("INVALID_REQUEST")
        if binding.scope.learner.principal != auth.principal:
            raise CourseError("NOT_FOUND")
        scope_key_value = scope_key(binding.scope)
        for _ in range(self.settings.max_conflict_retries):
            session, user, now = self._session_user(auth)
            epoch = user["epoch"]
            head = self._get(_head_key(scope_key_value, epoch))
            final = self._get(_final_key(scope_key_value, epoch))
            if head is None and final is None:
                head_item = {
                    **_head_key(scope_key_value, epoch),
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
                    **_final_key(scope_key_value, epoch),
                    "schema_version": SCHEMA_VERSION,
                    "scope_key": scope_key_value,
                    "epoch": epoch,
                    "revision": 0,
                    "phase": "free",
                    "active_attempt_id": None,
                    "passed_attempt_id": None,
                    "updated_at": now,
                }
                if self._commit([
                    _check(_session_key(auth.session_id), {"status": "active", "revision": auth.revision, "principal": auth.principal}),
                    _check(_user_key(auth.principal), {"epoch": epoch, "revision": user["revision"]}),
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
            head = self._get(_head_key(scope_key_value, user["epoch"]))
            final = self._get(_final_key(scope_key_value, user["epoch"]))
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
            if self._commit([
                _check(_session_key(auth.session_id), {"status": "active", "revision": auth.revision, "principal": auth.principal}),
                _check(_user_key(auth.principal), {"epoch": user["epoch"], "revision": user["revision"]}),
                _check(_learner_key_row(inventory.learner_key, user["epoch"]), {
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
            head = self._get(_head_key(ticket.scope_key, user["epoch"]))
            if ticket.epoch != user["epoch"]:
                return self._gate_from_head(ticket.scope_key, user["epoch"], head)
            learner_key_value = (head or {}).get("learner_key")
            current_learner = self._inventory_row(learner_key_value, user["epoch"]) if type(learner_key_value) is str else None
            if current_learner is None or current_learner.get("inventory_generation") != ticket.inventory_generation:
                return self._gate_from_head(ticket.scope_key, user["epoch"], head)
            if head is None or head.get("refresh_generation") != ticket.generation or head.get("epoch") != ticket.epoch:
                return self._gate_from_head(ticket.scope_key, user["epoch"], head)
            guards = [
                _check(_session_key(auth.session_id), {"status": "active", "revision": auth.revision, "principal": auth.principal}),
                _check(_user_key(auth.principal), {"epoch": user["epoch"], "revision": user["revision"]}),
                _check(_learner_key_row(learner_key_value, ticket.epoch), {
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
                if self._commit(guards + [
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
            final = self._get(_final_key(ticket.scope_key, ticket.epoch))
            if final is None:
                _unavailable()
            guards.append(_check(_final_key(ticket.scope_key, ticket.epoch), {
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
            if self._commit(guards + [
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

    def load_view(self, auth: AuthContext, ids: PublicIds) -> CourseView:
        session, user, now = self._session_user(auth)
        head, bundle, inventory, gate, progress, binding = self._view_parts(auth, user, ids.course_id, ids.enrollment_id)
        if binding.public_ids != ids:
            raise CourseError("NOT_FOUND")
        return CourseView(
            scope_key(bundle.scope), binding.public_ids, bundle, json_bytes(progress), gate, inventory,
        )

    def load_start_view(self, auth: AuthContext, command: StartCommand) -> CourseView:
        session, user, now = self._session_user(auth)
        head, bundle, inventory, gate, progress, binding = self._view_parts(
            auth, user, command.course_id, command.enrollment_id,
        )
        return CourseView(
            scope_key(bundle.scope), binding.public_ids, bundle, json_bytes(progress), gate, inventory,
        )

    def _view_parts(self, auth, user, course_id, enrollment_id):
        locator = self._get(_principal_key(auth.principal, user["epoch"]))
        learner_key_value = None
        if locator is not None:
            learner_key_value = locator.get("learner_key")
        if type(learner_key_value) is not str:
            learner_key_value = user.get("course_learner_key")
        if type(learner_key_value) is not str:
            raise CourseError("NOT_FOUND")
        inventory_row = self._inventory_row(learner_key_value, user["epoch"])
        inventory = self._inventory_view(inventory_row, learner_key_value, user["epoch"])
        binding = None
        for item in inventory.assignments:
            if item.public_ids.course_id == course_id and item.public_ids.enrollment_id == enrollment_id:
                binding = item
                break
        if binding is None:
            raise CourseError("NOT_FOUND")
        scope_key_value = scope_key(binding.scope)
        head = self._get(_head_key(scope_key_value, user["epoch"]))
        final = self._get(_final_key(scope_key_value, user["epoch"]))
        if head is None or final is None:
            _unavailable()
        if head.get("scope_key") != scope_key_value:
            _unavailable()
        bundle = self._load_bundle(head)
        if bundle is None:
            raise CourseError("ARC_PROGRESS_UNAVAILABLE")
        progress = _merge_final(_parse_field(head["progress_json"]), final)
        gate = self._gate_from_head(scope_key_value, user["epoch"], head)
        return head, bundle, inventory, gate, progress, binding

    def find_created(self, auth: AuthContext, command: StartCommand, *, kind: str) -> StartReceipt | None:
        require_member(kind, START_KINDS)
        session, user, now = self._session_user(auth)
        row = self._get(_create_key(auth.session_id, command.request_id))
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
        for _ in range(self.settings.max_conflict_retries):
            replay = self.find_created(auth, command, kind=kind)
            if replay is not None:
                return replay
            session, user, now = self._session_user(auth)
            fresh, head, final = self._fresh_view(auth, user, view)
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

    def _fresh_view(self, auth, user, view: CourseView) -> tuple[CourseView, dict, dict]:
        locator = self._get(_principal_key(auth.principal, user["epoch"]))
        learner_key_value = (locator or {}).get("learner_key") or user.get("course_learner_key")
        if type(learner_key_value) is not str:
            learner_key_value = learner_key(view.bundle.scope.learner)
        inventory_row = self._inventory_row(learner_key_value, user["epoch"])
        inventory = self._inventory_view(inventory_row, learner_key_value, user["epoch"])
        head = self._get(_head_key(view.scope_key, user["epoch"]))
        final = self._get(_final_key(view.scope_key, user["epoch"]))
        if head is None or final is None:
            raise CourseError("ARC_PROGRESS_UNAVAILABLE")
        bundle = self._load_bundle(head) or view.bundle
        progress = _merge_final(_parse_field(head["progress_json"]), final)
        gate = self._gate_from_head(view.scope_key, user["epoch"], head)
        return CourseView(view.scope_key, view.public_ids, bundle, json_bytes(progress), gate, inventory), head, final

    def _commit_start(self, auth, user, now, command, kind, view, placement, role, template, head, final):
        epoch = user["epoch"]
        scope_key_value = view.scope_key
        place_key = placement_key(scope_key_value, placement.source_id)
        # These are exactly the rows used by can_start; never adopt a newer
        # revision after the verdict. A collision causes a complete re-read.
        inventory = view.inventory
        learner_key_value = inventory.learner_key
        item_row = self._get(_item_key(scope_key_value, epoch, place_key))
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
        actions = [
            _check(_session_key(auth.session_id), {
                "status": "active", "revision": auth.revision, "principal": auth.principal,
            }),
            _check(_user_key(auth.principal), {"epoch": epoch, "revision": user["revision"]}),
            _check(_learner_key_row(learner_key_value, epoch), {
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
                **_start_key(scope_key_value, epoch, start_id),
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
                **_locator_key(start_id),
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
                **_attempt_key(attempt_id),
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
            actions.append(_put(new_final, if_match={"revision": final["revision"], "phase": "free", "epoch": epoch}))
        if placement.kind in {"training", "assessment"}:
            if item_row is not None and item_row.get("completed") is True:
                raise CourseError("ITEM_ALREADY_COMPLETED")
            if item_row is None:
                item_row = {
                    **_item_key(scope_key_value, epoch, place_key),
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
                actions.append(_check(_item_key(scope_key_value, epoch, place_key), {"completed": False, "revision": item_row["revision"]}))
        create_row = {
            **_create_key(auth.session_id, command.request_id),
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

    def report(
        self, auth: AuthContext, *, course_id: int, enrollment_id: int, placement_id: int, report: ContentReport,
    ) -> StoredProgressReceipt:
        if type(report) is not ContentReport:
            raise CourseError("INVALID_REQUEST")
        for _ in range(self.settings.max_conflict_retries):
            result = self._report_once(auth, course_id, enrollment_id, placement_id, report)
            if result is not None:
                return result
        raise CourseError("TEMPORARILY_UNAVAILABLE")

    def _report_once(self, auth, course_id, enrollment_id, placement_id, report):
        session, user, now = self._session_user(auth)
        locator = self._get(_locator_key(report.start_id))
        if locator is None or locator.get("principal") != auth.principal:
            raise CourseError("NOT_FOUND")
        if locator.get("bound_session_id") != auth.session_id:
            raise CourseError("NOT_FOUND")
        scope_key_value = locator["scope_key"]
        start_epoch = locator["epoch"]
        start = self._get(_start_key(scope_key_value, start_epoch, report.start_id))
        if start is None:
            raise CourseError("NOT_FOUND")
        if start.get("bound_session_id") != auth.session_id:
            raise CourseError("NOT_FOUND")
        if start.get("course_id") != course_id or start.get("enrollment_id") != enrollment_id:
            raise CourseError("NOT_FOUND")
        if start.get("public_link_id") != placement_id:
            raise CourseError("NOT_FOUND")
        computed = placement_key(scope_key_value, start["source_id"])
        if start.get("placement_key") != computed:
            _unavailable()
        request_digest = report_request_digest(course_id, enrollment_id, placement_id, report)
        existing = self._get(_report_key(scope_key_value, start_epoch, report.start_id, report.report_id))
        if existing is not None:
            if existing.get("request_digest") != request_digest:
                raise CourseError("IDEMPOTENCY_CONFLICT")
            return StoredProgressReceipt(
                scope_key_value, report.start_id, report.report_id,
                _parse_field(existing["response_json"]),
            )
        historical = start_epoch != user["epoch"]
        head = None
        bundle = None
        evidence = _start_evidence(start, historical=historical, current_version=None)
        if not historical:
            head = self._get(_head_key(scope_key_value, user["epoch"]))
            bundle = self._load_bundle(head) if head else None
            if bundle is not None:
                current = None
                for item in bundle.placements:
                    if (item.public_link_id == placement_id and type(item.source_id) is type(start["source_id"])
                            and item.source_id == start["source_id"]):
                        current = item.content_version
                        saved_identity = start.get("content_identity_hash")
                        evidence["current_content_identity_matches"] = (
                            saved_identity == digest(parse_json(item.content_identity_json))
                            if type(saved_identity) is str
                            else start.get("definition_hash") == bundle.definition_hash
                        )
                        break
                evidence["current_content_version"] = current
                evidence["current_content_missing"] = current is None
        receipt_body = parse_json(self.policy.evaluate_content(json_bytes(evidence), report))
        report_row = {
            **_report_key(scope_key_value, start_epoch, report.start_id, report.report_id),
            "schema_version": SCHEMA_VERSION,
            "start_id": report.start_id,
            "report_id": report.report_id,
            "request_digest": request_digest,
            "event_type": report.event_type,
            "content_version": report.content_version,
            "response_json": _json_field(receipt_body),
            "historical_only": historical,
        }
        new_start = deepcopy(start)
        new_start["revision"] = start["revision"] + 1
        new_start["report_count"] = start.get("report_count", 0) + 1
        if report.event_type == "video_segments" and not historical:
            merged = merge_intervals(list(start.get("merged_intervals_ms") or []) + [list(pair) for pair in report.intervals_ms])
            new_start["merged_intervals_ms"] = merged
            if receipt_body.get("isCompleted") is True:
                new_start["completed"] = True
        if report.event_type == "document_displayed" and not historical:
            displayed = list(start.get("displayed") or [])
            displayed.append({"report_id": report.report_id, "content_version": report.content_version})
            new_start["displayed"] = displayed
            if receipt_body.get("isCompleted") is True:
                new_start["completed"] = True
        if report.event_type == "document_confirmed" and not historical:
            pending = list(start.get("pending_confirmations") or [])
            pending.append({
                "report_id": report.report_id,
                "display_report_id": report.display_report_id,
                "content_version": report.content_version,
            })
            new_start["pending_confirmations"] = pending
            if receipt_body.get("isCompleted") is True:
                new_start["completed"] = True
        # Typed JSON includes names, escaped text and per-value tags. This is a
        # conservative budget below DynamoDB's item limit, not an exact storage
        # size estimate. Reject before any REPORT/START/progress write.
        if len(canonical_bytes(new_start)) > DYNAMODB_ITEM_MAX_BYTES:
            raise CourseError("PROGRESS_CAPACITY_EXCEEDED")
        actions = [
            _check(_session_key(auth.session_id), {
                "status": "active", "revision": auth.revision, "principal": auth.principal,
            }),
            _check(_user_key(auth.principal), {"epoch": user["epoch"], "revision": user["revision"]}),
            _put(report_row, if_not_exists=True),
            _put(new_start, if_match={"revision": start["revision"]}),
        ]
        if historical:
            if self._commit(actions):
                return StoredProgressReceipt(scope_key_value, report.start_id, report.report_id, receipt_body)
            return None
        if head is None:
            _unavailable()
        if bundle is None:
            raise CourseError("ARC_PROGRESS_UNAVAILABLE")
        place_key = start["placement_key"]
        item_row = self._get(_item_key(scope_key_value, user["epoch"], place_key))
        completed_now = receipt_body.get("isCompleted") is True
        if item_row is None:
            item_row = {
                **_item_key(scope_key_value, user["epoch"], place_key),
                "schema_version": SCHEMA_VERSION,
                "scope_key": scope_key_value,
                "placement_key": place_key,
                "epoch": user["epoch"],
                "source_id": start["source_id"],
                "public_link_id": placement_id,
                "content_version": start["content_version"],
                "completed": completed_now,
                "passed": None,
                "revision": 0,
            }
            actions.append(_put(item_row, if_not_exists=True))
        else:
            new_item = deepcopy(item_row)
            new_item["revision"] = item_row["revision"] + 1
            if item_row.get("completed") is True:
                new_item["completed"] = True
            else:
                new_item["completed"] = completed_now
            actions.append(_put(new_item, if_match={"revision": item_row["revision"]}))
        progress = _parse_field(head["progress_json"])
        completed = list(progress.get("completed_placements") or [])
        if completed_now and place_key not in completed:
            if item_row.get("completed") is True or completed_now:
                completed.append(place_key)
        items = dict(progress.get("items") or {})
        items[place_key] = {
            "source_id": start["source_id"],
            "public_link_id": placement_id,
            "kind": start.get("content_kind"),
            "content_version": start["content_version"],
            "completed": True if (item_row.get("completed") is True or completed_now) else False,
            "passed": None,
        }
        progress["completed_placements"] = completed
        progress["items"] = items
        aggregated = parse_json(self.policy.aggregate_progress(bundle, json_bytes(progress)))
        new_head = deepcopy(head)
        new_head["revision"] = head["revision"] + 1
        new_head["completed_placements"] = aggregated["completed_placements"]
        new_head["course_complete"] = aggregated["course_complete"]
        new_head["progress_json"] = _json_field(aggregated)
        new_head["updated_at"] = now
        actions.append(_put(new_head, if_match={"revision": head["revision"], "epoch": user["epoch"]}))
        receipt_body = dict(receipt_body)
        if receipt_body.get("application") != "historical_only":
            receipt_body["courseStatus"] = aggregated["course_status"]
        report_row["response_json"] = _json_field(receipt_body)
        if self._commit(actions):
            return StoredProgressReceipt(scope_key_value, report.start_id, report.report_id, receipt_body)
        return None


def _start_evidence(start, *, historical, current_version):
    return {
        "start_id": start["start_id"],
        "public_link_id": start["public_link_id"],
        "kind": start.get("content_kind"),
        "content_version": start["content_version"],
        "current_content_version": current_version,
        "duration_ms": start.get("duration_ms"),
        "historical_only": historical,
        "completed": start.get("completed") is True,
        "course_status": "IN_PROGRESS",
        "merged_intervals_ms": list(start.get("merged_intervals_ms") or []),
        "displayed": list(start.get("displayed") or []),
        "pending_confirmations": list(start.get("pending_confirmations") or []),
        "report_count": start.get("report_count") or 0,
    }


def _assessment_identity(bundle):
    if not bundle.placements:
        return None
    item = bundle.placements[-1]
    return (type(item.source_id), item.source_id, item.kind, item.content_version,
            digest(parse_json(item.content_identity_json)),
            None if item.execution_json is None else digest(parse_json(item.execution_json)))


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

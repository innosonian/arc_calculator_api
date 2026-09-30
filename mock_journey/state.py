"""DynamoDB control-state transactions for the Mock Journey.

The caller injects a low-level client. This module creates no clients, reads no
environment, and stores no bearer/resume secret or calculator payload. Integer
control state and an opaque definition JSON string avoid profile type coercion.
"""

from copy import deepcopy
from decimal import Decimal
import time
import uuid

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import BotoCoreError, ClientError

from mock_journey.errors import JourneyError
from mock_journey.settings import MAX_CONFLICT_RETRIES
from mock_journey.storage_keys import attempt_key, course_final_key, course_head_key, row_key, session_key, user_key
from services.operational_logs import record_event


_SERIALIZER = TypeSerializer()
_DESERIALIZER = TypeDeserializer()
_SESSION_FIELDS = (
    "session_id", "principal", "token_hash", "issued_at", "expires_at", "status", "revision",
)


def _unavailable():
    return JourneyError("TEMPORARILY_UNAVAILABLE")


def _optional_course_binding(attempt):
    """A stored attempt row's course_binding, or None for a legacy row; an unusable value is unavailable."""
    from mock_journey.course_contracts import binding_from_row
    try:
        return binding_from_row(attempt, null_is_legacy=False)
    except ValueError:
        raise _unavailable() from None


class _ReadConflict(Exception):
    """A transaction read competes for the same bounded command retry budget."""


def _encode(item):
    def check(value):
        if value is None or type(value) in (str, int, bool):
            return
        if type(value) is dict and all(type(key) is str for key in value):
            for nested in value.values():
                check(nested)
            return
        if type(value) is list:
            for nested in value:
                check(nested)
            return
        raise _unavailable()

    check(item)
    return {key: _SERIALIZER.serialize(value) for key, value in item.items()}


def _decode(item):
    def integer_controls(value):
        if isinstance(value, Decimal):
            if not value.is_finite() or value != value.to_integral_value():
                raise _unavailable()
            return int(value)
        if isinstance(value, dict):
            return {key: integer_controls(nested) for key, nested in value.items()}
        if isinstance(value, list):
            return [integer_controls(nested) for nested in value]
        if value is None or type(value) in (str, bool):
            return value
        raise _unavailable()

    try:
        return integer_controls({key: _DESERIALIZER.deserialize(value) for key, value in item.items()})
    except (ValueError, TypeError, KeyError):
        raise _unavailable() from None


def legacy_slot_key(attempt):
    """USER.slots key of a legacy (pre-D103 v1) attempt: "program:target" (catalog.definition_key)."""
    return f'{attempt["program_id"]}:{attempt["target"]}'


def empty_legacy_slot():
    """A reset legacy USER slot (logout, R3)."""
    return {"completed": False, "completed_by_attempt": None, "completed_at": None, "open_attempts": 0}


def legacy_progress(attempt, user, checked, now):
    """Progress of a finalized legacy (pre-D103 v1) attempt: (progress_application, slot completion or None).

    Pure: no read or write. Same reason order as course_submission._progress
    (Q17: v2 is final). The USER slot is read only once the earlier reasons do
    not apply, and an unusable slot is TEMPORARILY_UNAVAILABLE like every other
    legacy slot read. The second value is the update the caller applies to the
    attempt's slot when the progress is APPLIED.
    """
    slot_key = legacy_slot_key(attempt)
    if attempt["epoch"] != user["epoch"]:
        reason = "PROGRESS_RESET"
    elif checked["goal"].get("status") == "pending_policy":
        reason = "GOAL_POLICY_UNRESOLVED"
    elif DynamoStateRepository._slot(user, slot_key)["completed"]:
        reason = "ALREADY_COMPLETED"
    elif not checked["program_completed"]:
        reason = "REQUIREMENTS_NOT_MET"
    else:
        return ({"applied": True, "applied_epoch": attempt["epoch"], "reason": "APPLIED"},
                {"completed": True, "completed_by_attempt": attempt["attempt_id"], "completed_at": now})
    return {"applied": False, "applied_epoch": None, "reason": reason}, None


def extend_condition(action, clause, names, values=None):
    """Append " AND clause" to a Put/ConditionCheck action in place; values are encoded like the rest."""
    ((_, body),) = action.items()
    body["ConditionExpression"] += " AND " + clause
    body["ExpressionAttributeNames"].update(names)
    if values is not None:
        body["ExpressionAttributeValues"].update(_encode(values))
    return action


# Public names of the kernel that jobs.py and relay_progress.py share with this
# module (X3-05). The underscore names stay bound to the same objects.
ReadConflict = _ReadConflict
encode_item = _encode
decode_item = _decode
unavailable = _unavailable


class DynamoStateRepository:
    def __init__(self, client, table_name, *, clock=time.time, max_conflict_retries=MAX_CONFLICT_RETRIES):
        if (not table_name or type(max_conflict_retries) is not int
                or not 1 <= max_conflict_retries <= MAX_CONFLICT_RETRIES):
            raise ValueError("Invalid state repository configuration.")
        self.client = client
        self.table_name = table_name
        self.clock = clock
        self.max_conflict_retries = max_conflict_retries

    def _now(self):
        return int(self.clock())

    def _read(self, keys):
        try:
            response = self.client.transact_get_items(TransactItems=[
                {"Get": {"TableName": self.table_name, "Key": _encode(key)}} for key in keys
            ])
            rows = response["Responses"]
            if len(rows) != len(keys):
                raise _unavailable()
            return [_decode(row["Item"]) if row.get("Item") else None for row in rows]
        except ClientError as error:
            if self._conflict(error):
                raise _ReadConflict() from None
            raise _unavailable() from None
        except (BotoCoreError, KeyError, TypeError):
            raise _unavailable() from None

    def _read_only(self, keys):
        for _ in range(self.max_conflict_retries):
            try:
                return self._read(keys)
            except _ReadConflict:
                continue
        raise _unavailable()

    def _get(self, key):
        try:
            response = self.client.get_item(TableName=self.table_name, Key=_encode(key), ConsistentRead=True)
            return _decode(response["Item"]) if response.get("Item") else None
        except (ClientError, BotoCoreError, KeyError, TypeError):
            raise _unavailable() from None

    @staticmethod
    def _conflict(error):
        code = error.response.get("Error", {}).get("Code")
        if code in {"ConditionalCheckFailedException", "TransactionConflictException"}:
            return True
        if code == "TransactionCanceledException":
            reasons = error.response.get("CancellationReasons") or []
            codes = [reason.get("Code", "None") for reason in reasons]
            return bool(codes) and any(code != "None" for code in codes) and all(
                code in {"None", "ConditionalCheckFailed", "TransactionConflict"} for code in codes
            )
        return False

    def _write(self, actions):
        try:
            self.client.transact_write_items(TransactItems=actions)
            return True
        except ClientError as error:
            if self._conflict(error):
                return False
            raise _unavailable() from None
        except BotoCoreError:
            raise _unavailable() from None

    def _put(self, item):
        return {"Put": {
            "TableName": self.table_name, "Item": _encode(item),
            "ConditionExpression": "attribute_not_exists(PK)",
        }}

    def _condition(self, key, expression, names, values):
        return {"ConditionCheck": {
            "TableName": self.table_name, "Key": _encode(key),
            "ConditionExpression": expression, "ExpressionAttributeNames": names,
            "ExpressionAttributeValues": _encode(values),
        }}

    def _replace(self, item, expression, names, values):
        # A conditional replacement uses the coherent snapshot, including future
        # unrelated fields. It is one action per item, never check+put together.
        action = self._put(item)
        action["Put"].update(
            ConditionExpression=expression,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=_encode(values),
        )
        return action

    def control_get(self, key):
        """Public CourseStore read. Python types; missing row is None."""
        if type(key) is not dict or set(key) != {"PK", "SK"}:
            raise _unavailable()
        if any(type(key[name]) is not str or not key[name] for name in ("PK", "SK")):
            raise _unavailable()
        return self._get({"PK": key["PK"], "SK": key["SK"]})

    def control_transact(self, actions):
        """Public CourseStore write. True committed, False condition/conflict."""
        if type(actions) is not list or not actions:
            raise _unavailable()
        return self._write([self._course_control_action(action) for action in actions])

    def _course_control_action(self, action):
        if type(action) is not dict or action.get("op") not in ("put", "condition_check"):
            raise _unavailable()
        if action["op"] == "put":
            item = action.get("item")
            if (type(item) is not dict or item.get("PK") is None or item.get("SK") is None
                    or "if_greater" in action):
                raise _unavailable()
            expression, names, values = self._course_condition(action, item)
            encoded = {"Put": {"TableName": self.table_name, "Item": _encode(item)}}
            if expression:
                encoded["Put"]["ConditionExpression"] = expression
                if names:
                    encoded["Put"]["ExpressionAttributeNames"] = names
                if values:
                    encoded["Put"]["ExpressionAttributeValues"] = _encode(values)
            return encoded
        key = action.get("key")
        if type(key) is not dict or set(key) != {"PK", "SK"}:
            raise _unavailable()
        match = action.get("if_match")
        if type(match) is not dict or not match:
            raise _unavailable()
        expression, names, values = self._field_equals(match, "c")
        expression = "attribute_exists(#pk) AND " + expression
        names["#pk"] = "PK"
        greater = action.get("if_greater")
        if greater is not None:
            # Course session checks: expires_at > commit-time now (v1 parity).
            if type(greater) is not dict or not greater:
                raise _unavailable()
            for index, (field, value) in enumerate(greater.items()):
                if type(field) is not str or not field or type(value) is not int:
                    raise _unavailable()
                names[f"#g{index}"] = field
                values[f":g{index}"] = value
                expression += f" AND #g{index} > :g{index}"
        return self._condition({"PK": key["PK"], "SK": key["SK"]}, expression, names, values)

    def _course_condition(self, action, item):
        if action.get("if_not_exists"):
            return "attribute_not_exists(#pk)", {"#pk": "PK"}, {}
        match = action.get("if_match")
        if match is not None:
            if type(match) is not dict or not match:
                raise _unavailable()
            return self._field_equals(match, "m")
        missing = action.get("if_missing_or_match")
        if missing is not None:
            if type(missing) is not dict or not missing:
                raise _unavailable()
            expression, names, values = self._field_equals(missing, "o")
            names["#pk"] = "PK"
            return "(attribute_not_exists(#pk) OR (" + expression + "))", names, values
        return "attribute_not_exists(#pk)", {"#pk": "PK"}, {}

    @staticmethod
    def _field_equals(expected, prefix):
        names, values, parts = {}, {}, []
        for index, (field, value) in enumerate(expected.items()):
            if type(field) is not str or not field:
                raise _unavailable()
            name, token = f"#{prefix}{index}", f":{prefix}{index}"
            names[name] = field
            values[token] = value
            parts.append(f"{name} = {token}")
        return " AND ".join(parts), names, values

    @staticmethod
    def _check_session(session, auth, now, *, allow_revoked=False):
        if not session or session.get("session_id") != auth.session_id or session.get("principal") != auth.principal:
            raise JourneyError("SESSION_REQUIRED")
        if session.get("status") == "revoked":
            if allow_revoked and session.get("logout_epoch"):
                return
            raise JourneyError("SESSION_REVOKED")
        if session.get("status") != "active":
            raise JourneyError("SESSION_REQUIRED")
        expires_at = session.get("expires_at")
        if type(expires_at) is not int:
            raise _unavailable()
        if expires_at <= now:
            raise JourneyError("SESSION_EXPIRED")
        if session.get("revision") != auth.revision or expires_at != auth.expires_at:
            raise JourneyError("SESSION_REQUIRED")

    def _session_condition(self, auth, now):
        return self._condition(
            session_key(auth.session_id),
            "#s = :active AND #r = :revision AND #p = :principal AND #e > :now AND #e = :expiry",
            {"#s": "status", "#r": "revision", "#p": "principal", "#e": "expires_at"},
            {":active": "active", ":revision": auth.revision, ":principal": auth.principal,
             ":now": now, ":expiry": auth.expires_at},
        )

    @staticmethod
    def _require_user(user, principal):
        # Principal/epoch/revision only (R1). New USER rows carry no slots; the
        # slots of a legacy (pre-D103 v1) row are neither required nor removed.
        if not user or user.get("principal") != principal or type(user.get("revision")) is not int or not user.get("epoch"):
            raise _unavailable()
        return user

    @staticmethod
    def _slot(user, slot_key):
        """A slot of a stored legacy (pre-D103 v1) USER row; used only for legacy attempts."""
        slots = user.get("slots")
        if type(slots) is not dict:
            raise _unavailable()
        slot = slots.get(slot_key)
        if (type(slot) is not dict or type(slot.get("completed")) is not bool
                or type(slot.get("open_attempts")) is not int or slot["open_attempts"] < 0):
            raise _unavailable()
        return slot

    def _user_action(self, old, new=None):
        expression = "#e = :epoch AND #r = :revision"
        names = {"#e": "epoch", "#r": "revision"}
        values = {":epoch": old["epoch"], ":revision": old["revision"]}
        if new is None:
            return self._condition(user_key(old["principal"]), expression, names, values)
        return self._replace(new, expression, names, values)

    @staticmethod
    def _check_attempt(attempt, auth):
        if not attempt or attempt.get("principal") != auth.principal or attempt.get("bound_session_id") != auth.session_id:
            raise JourneyError("NOT_FOUND")

    def _attempt_action(self, old, new):
        return self._replace(
            new, "#r = :revision AND #b = :bound AND #p = :principal AND #s = :state",
            {"#r": "revision", "#b": "bound_session_id", "#p": "principal", "#s": "state"},
            {":revision": old["revision"], ":bound": old["bound_session_id"],
             ":principal": old["principal"], ":state": old["state"]},
        )

    def _close_open_attempt(self, attempt, user, now):
        """The USER row releasing a same-epoch counted legacy attempt's slot, or None."""
        if attempt["epoch"] != user["epoch"] or not attempt["active_counted"]:
            return None
        slot_key = legacy_slot_key(attempt)
        if self._slot(user, slot_key)["open_attempts"] <= 0:
            raise _unavailable()
        changed = deepcopy(user)
        changed["slots"][slot_key]["open_attempts"] -= 1
        changed.update(revision=user["revision"] + 1, updated_at=now)
        return changed

    # -- Public kernel for jobs.py / relay_progress.py (X3-05) ------------------
    # Each name delegates to the underscore method through the instance, so a
    # caller or test that replaces e.g. ``state._write`` on an instance still
    # intercepts every use. Requests, conditions and errors are unchanged.
    def now(self):
        return self._now()

    def read_rows(self, keys):
        return self._read(keys)

    def get_row(self, key):
        return self._get(key)

    def write(self, actions):
        return self._write(actions)

    def put_action(self, item):
        return self._put(item)

    def condition_action(self, key, expression, names, values):
        return self._condition(key, expression, names, values)

    def replace_action(self, item, expression, names, values):
        return self._replace(item, expression, names, values)

    def session_condition(self, auth, now):
        return self._session_condition(auth, now)

    def user_action(self, old, new=None):
        return self._user_action(old, new)

    def attempt_action(self, old, new):
        return self._attempt_action(old, new)

    def check_session(self, session, auth, now, *, allow_revoked=False):
        return self._check_session(session, auth, now, allow_revoked=allow_revoked)

    def check_attempt(self, attempt, auth):
        return self._check_attempt(attempt, auth)

    def require_user(self, user, principal):
        return self._require_user(user, principal)

    def legacy_slot(self, user, slot_key):
        return self._slot(user, slot_key)

    def close_open_attempt(self, attempt, user, now):
        return self._close_open_attempt(attempt, user, now)

    def get_session(self, session_id):
        return self._get(session_key(session_id))

    def create_session(self, session):
        if any(key not in session for key in _SESSION_FIELDS) or session["status"] != "active":
            raise _unavailable()
        now = self._now()
        # A new USER row has no slots (Q10). An existing row, legacy slots
        # included, is never replaced here (conditional put).
        user = {
            **user_key(session["principal"]),
            "principal": session["principal"], "epoch": str(uuid.uuid4()), "revision": 0,
            "updated_at": now,
        }
        try:
            action = self._put(user)["Put"]
            self.client.put_item(**action)
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise _unavailable() from None
        except BotoCoreError:
            raise _unavailable() from None
        stored = {**session_key(session["session_id"]), **{key: session[key] for key in _SESSION_FIELDS}}
        if not self._write([self._put(stored)]):
            raise _unavailable()

    def read_session_user(self, auth):
        """One coherent SESSION+USER read: the session is valid and the USER row is usable."""
        session, user = self._read_only([
            session_key(auth.session_id), user_key(auth.principal),
        ])
        self._check_session(session, auth, self._now())
        return self._require_user(user, auth.principal)

    def get_attempt(self, auth, attempt_id):
        session, attempt = self._read_only([
            session_key(auth.session_id), attempt_key(attempt_id),
        ])
        self._check_session(session, auth, self._now())
        self._check_attempt(attempt, auth)
        return attempt

    def logout(self, auth):
        for _ in range(self.max_conflict_retries):
            try:
                session, user = self._read([
                    session_key(auth.session_id), user_key(auth.principal),
                ])
            except _ReadConflict:
                continue
            self._check_session(session, auth, self._now(), allow_revoked=True)
            if session["status"] == "revoked":
                return
            self._require_user(user, auth.principal)
            # Legacy (pre-D103 v1) row only (R3): its slots are reset as before. A
            # row without slots never gains them.
            legacy_slots = "slots" in user
            if legacy_slots and type(user["slots"]) is not dict:
                raise _unavailable()
            now = self._now()
            self._check_session(session, auth, now)
            epoch = str(uuid.uuid4())
            updated = {**session, "status": "revoked", "revision": session["revision"] + 1,
                       "revoked_at": now, "logout_epoch": epoch}
            new_user = deepcopy(user)
            new_user.update(epoch=epoch, revision=user["revision"] + 1, updated_at=now)
            if legacy_slots:
                new_user["slots"] = {key: empty_legacy_slot() for key in user["slots"]}
            session_action = self._session_condition(auth, now)["ConditionCheck"]
            session_action.pop("Key")
            session_action["Item"] = _encode(updated)
            if self._write([{"Put": session_action}, self._user_action(user, new_user)]):
                record_event("progress_reset", session_id=auth.session_id, progress_epoch=epoch)
                return
        raise _unavailable()

    def cancel_attempt(self, auth, attempt_id, reason):
        for _ in range(self.max_conflict_retries):
            try:
                session, attempt, user = self._read([
                    session_key(auth.session_id), attempt_key(attempt_id),
                    user_key(auth.principal),
                ])
            except _ReadConflict:
                continue
            self._check_session(session, auth, self._now())
            self._check_attempt(attempt, auth)
            if attempt["state"] == "cancelled":
                return
            if attempt["state"] != "created":
                raise JourneyError("INVALID_STATE")
            self._require_user(user, auth.principal)
            now = self._now()
            self._check_session(session, auth, now)
            updated = {**attempt, "state": "cancelled", "revision": attempt["revision"] + 1,
                       "active_counted": False, "cancelled_at": now, "cancel_reason": reason}
            new_user = self._close_open_attempt(attempt, user, now)
            actions = [self._session_condition(auth, now), self._attempt_action(attempt, updated),
                       self._user_action(user, new_user)]
            binding = _optional_course_binding(attempt)
            if (binding is not None and binding["start_role"] == "final_assessment"
                    and attempt["epoch"] == user["epoch"]):
                head, final = self._read_only([
                    course_head_key(binding["scope_key"], attempt["epoch"]),
                    course_final_key(binding["scope_key"], attempt["epoch"]),
                ])
                if (head is None or final is None or final.get("phase") != "active"
                        or final.get("active_attempt_id") != attempt_id):
                    raise _unavailable()
                from mock_journey.course_policy import resting_phase
                for row, changes in ((head, {}), (final, {"phase": resting_phase(final), "active_attempt_id": None})):
                    changed = {**row, **changes, "revision": row["revision"] + 1}
                    expression = "#r = :r AND #e = :e"
                    names = {"#r": "revision", "#e": "epoch"}
                    values = {":r": row["revision"], ":e": attempt["epoch"]}
                    if row is final:
                        expression += " AND #a = :a AND #p = :p"
                        names.update({"#a": "active_attempt_id", "#p": "phase"})
                        values.update({":a": attempt_id, ":p": "active"})
                    actions.append(self._replace(changed, expression, names, values))
            if self._write(actions):
                record_event("attempt_cancelled", attempt_id=attempt_id, reason=reason)
                return
        raise _unavailable()

    def get_attempt_for_reauthorization(self, attempt_id):
        return self._get(attempt_key(attempt_id))

    def reauthorize_attempt(self, auth, attempt_id, expected_resume_digest):
        for _ in range(self.max_conflict_retries):
            try:
                session, attempt = self._read([
                    session_key(auth.session_id), attempt_key(attempt_id),
                ])
            except _ReadConflict:
                continue
            self._check_session(session, auth, self._now())
            if not attempt or attempt.get("principal") != auth.principal or attempt.get("resume_digest") != expected_resume_digest:
                raise JourneyError("NOT_FOUND")
            if attempt["state"] == "cancelled":
                raise JourneyError("INVALID_STATE")
            if attempt["bound_session_id"] == auth.session_id:
                return attempt
            old_session = self.get_session(attempt["bound_session_id"])
            if not old_session or old_session.get("principal") != auth.principal:
                raise _unavailable()
            now = self._now()
            self._check_session(session, auth, now)
            if old_session.get("status") == "active" and old_session["expires_at"] > now:
                raise JourneyError("INVALID_STATE")
            if old_session.get("status") not in {"active", "revoked"}:
                raise _unavailable()
            old_condition = self._condition(
                session_key(attempt["bound_session_id"]),
                "#r = :revision AND #p = :principal AND (#s = :revoked OR (#s = :active AND #e <= :now))",
                {"#r": "revision", "#p": "principal", "#s": "status", "#e": "expires_at"},
                {":revision": old_session["revision"], ":principal": auth.principal,
                 ":revoked": "revoked", ":active": "active", ":now": now},
            )
            updated = {**attempt, "bound_session_id": auth.session_id, "revision": attempt["revision"] + 1}
            attempt_action = extend_condition(self._attempt_action(attempt, updated), "#digest = :digest",
                                              {"#digest": "resume_digest"}, {":digest": expected_resume_digest})
            if self._write([self._session_condition(auth, now), old_condition, attempt_action]):
                return updated
        raise _unavailable()


class DynamoCourseStore:
    """CourseStore adapter over DynamoStateRepository public control hooks."""

    def __init__(self, state):
        if type(state) is not DynamoStateRepository:
            raise ValueError("Invalid course store.")
        self._state = state

    def get_item(self, key):
        from mock_journey.course_errors import CourseError
        try:
            return self._state.control_get(key)
        except JourneyError as error:
            raise CourseError(error.code) from None

    def transact(self, actions):
        from mock_journey.course_errors import CourseError
        try:
            return self._state.control_transact(actions)
        except JourneyError as error:
            raise CourseError(error.code) from None

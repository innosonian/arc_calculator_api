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
from services.operational_logs import record_event


_SERIALIZER = TypeSerializer()
_DESERIALIZER = TypeDeserializer()
_TEMPLATE_FIELDS = (
    "attempt_id", "principal", "creator_session_id", "bound_session_id",
    "program_id", "target", "profile_name", "definition_json",
    "resume_nonce", "resume_key_version", "resume_digest",
)
_COURSE_BINDING_FIELDS = (
    "scope_key", "placement_key", "start_role", "definition_hash",
    "content_version", "epoch", "policy_version",
)
_SESSION_FIELDS = (
    "session_id", "principal", "token_hash", "issued_at", "expires_at", "status", "revision",
)


def _unavailable():
    return JourneyError("TEMPORARILY_UNAVAILABLE")


def _optional_course_binding(template):
    if "course_binding" not in template:
        return None
    from mock_journey.course_contracts import CourseBinding
    value = template["course_binding"]
    if type(value) is CourseBinding:
        return {key: getattr(value, key) for key in _COURSE_BINDING_FIELDS}
    if type(value) is dict and set(value) == set(_COURSE_BINDING_FIELDS):
        CourseBinding(**{key: value[key] for key in _COURSE_BINDING_FIELDS})
        return {key: value[key] for key in _COURSE_BINDING_FIELDS}
    raise _unavailable()


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


def _key(kind, value, suffix):
    return {"PK": f"{kind}#{value}", "SK": suffix}


class DynamoStateRepository:
    def __init__(self, client, table_name, *, clock=time.time, max_conflict_retries=8):
        if not table_name or type(max_conflict_retries) is not int or not 1 <= max_conflict_retries <= 8:
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
            if type(item) is not dict or item.get("PK") is None or item.get("SK") is None:
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
            _key("SESSION", auth.session_id, "AUTH"),
            "#s = :active AND #r = :revision AND #p = :principal AND #e > :now AND #e = :expiry",
            {"#s": "status", "#r": "revision", "#p": "principal", "#e": "expires_at"},
            {":active": "active", ":revision": auth.revision, ":principal": auth.principal,
             ":now": now, ":expiry": auth.expires_at},
        )

    @staticmethod
    def _require_user(user, principal):
        if not user or user.get("principal") != principal or type(user.get("revision")) is not int or not user.get("epoch") or type(user.get("slots")) is not dict:
            raise _unavailable()
        return user

    @staticmethod
    def _slot(user, slot_key):
        slot = user["slots"].get(slot_key)
        if (type(slot) is not dict or type(slot.get("completed")) is not bool
                or type(slot.get("open_attempts")) is not int or slot["open_attempts"] < 0):
            raise _unavailable()
        return slot

    def _user_action(self, old, new=None):
        expression = "#e = :epoch AND #r = :revision"
        names = {"#e": "epoch", "#r": "revision"}
        values = {":epoch": old["epoch"], ":revision": old["revision"]}
        if new is None:
            return self._condition(_key("USER", old["principal"], "STATE"), expression, names, values)
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

    def get_session(self, session_id):
        return self._get(_key("SESSION", session_id, "AUTH"))

    def create_session(self, session, slot_keys):
        if any(key not in session for key in _SESSION_FIELDS) or session["status"] != "active":
            raise _unavailable()
        now = self._now()
        user = {
            **_key("USER", session["principal"], "STATE"),
            "principal": session["principal"], "epoch": str(uuid.uuid4()), "revision": 0,
            "slots": {key: {"completed": False, "completed_by_attempt": None,
                            "completed_at": None, "open_attempts": 0} for key in slot_keys},
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
        stored = {**_key("SESSION", session["session_id"], "AUTH"), **{key: session[key] for key in _SESSION_FIELDS}}
        if not self._write([self._put(stored)]):
            raise _unavailable()

    def get_progress(self, auth):
        session, user = self._read_only([
            _key("SESSION", auth.session_id, "AUTH"), _key("USER", auth.principal, "STATE"),
        ])
        self._check_session(session, auth, self._now())
        return self._require_user(user, auth.principal)

    def get_attempt(self, auth, attempt_id):
        session, attempt = self._read_only([
            _key("SESSION", auth.session_id, "AUTH"), _key("ATTEMPT", attempt_id, "META"),
        ])
        self._check_session(session, auth, self._now())
        self._check_attempt(attempt, auth)
        return attempt

    def get_created_attempt(self, auth, client_request_id, request_digest):
        """Replay stored creation before consulting the current execution provider.

        This is a read optimization, not a replacement for create_attempt's
        transaction: a concurrent creator can still win after this read misses.
        """
        session, idem = self._read_only([
            _key("SESSION", auth.session_id, "AUTH"),
            _key("SESSION", auth.session_id, f"CREATE#{client_request_id}"),
        ])
        self._check_session(session, auth, self._now())
        if not idem:
            return None
        if idem.get("request_digest") != request_digest:
            raise JourneyError("IDEMPOTENCY_CONFLICT")
        return self.get_attempt(auth, idem["attempt_id"])

    def create_attempt(self, auth, client_request_id, request_digest, template):
        if (any(key not in template for key in _TEMPLATE_FIELDS)
                or template["principal"] != auth.principal
                or template["creator_session_id"] != auth.session_id
                or template["bound_session_id"] != auth.session_id):
            raise _unavailable()
        idem_key = _key("SESSION", auth.session_id, f"CREATE#{client_request_id}")
        for _ in range(self.max_conflict_retries):
            try:
                session, user, idem = self._read([
                    _key("SESSION", auth.session_id, "AUTH"), _key("USER", auth.principal, "STATE"), idem_key,
                ])
            except _ReadConflict:
                continue
            self._check_session(session, auth, self._now())
            if idem:
                if idem.get("request_digest") != request_digest:
                    raise JourneyError("IDEMPOTENCY_CONFLICT")
                return self.get_attempt(auth, idem["attempt_id"])
            self._require_user(user, auth.principal)
            slot_key = f'{template["program_id"]}:{template["target"]}'
            slot = self._slot(user, slot_key)
            if slot["completed"]:
                raise JourneyError("PROGRAM_ALREADY_COMPLETED")
            now = self._now()
            self._check_session(session, auth, now)
            attempt = {
                **_key("ATTEMPT", template["attempt_id"], "META"),
                **{key: template[key] for key in _TEMPLATE_FIELDS},
                "epoch": user["epoch"], "created_at": now, "state": "created", "revision": 0,
                "active_counted": True, "evaluation": None, "progress_application": None,
            }
            binding = _optional_course_binding(template)
            counted = True
            if binding is not None:
                attempt["course_binding"] = binding
                counted = template.get("active_counted") is True
                attempt["active_counted"] = counted
                for field in ("course_id", "enrollment_id", "course_item_link_id"):
                    if field in template:
                        attempt[field] = template[field]
            new_user = deepcopy(user)
            if counted:
                new_user["slots"][slot_key]["open_attempts"] += 1
            new_user["revision"] += 1
            new_user["updated_at"] = now
            user_action = self._user_action(user, new_user)
            if counted:
                user_action["Put"]["ConditionExpression"] += " AND #slots.#slot.#completed = :false"
                user_action["Put"]["ExpressionAttributeNames"].update(
                    {"#slots": "slots", "#slot": slot_key, "#completed": "completed"})
                user_action["Put"]["ExpressionAttributeValues"].update(_encode({":false": False}))
            idem = {**idem_key, "request_digest": request_digest, "attempt_id": attempt["attempt_id"]}
            if self._write([self._session_condition(auth, now), user_action, self._put(attempt), self._put(idem)]):
                return attempt
        raise _unavailable()

    def logout(self, auth):
        for _ in range(self.max_conflict_retries):
            try:
                session, user = self._read([
                    _key("SESSION", auth.session_id, "AUTH"), _key("USER", auth.principal, "STATE"),
                ])
            except _ReadConflict:
                continue
            self._check_session(session, auth, self._now(), allow_revoked=True)
            if session["status"] == "revoked":
                return
            self._require_user(user, auth.principal)
            now = self._now()
            self._check_session(session, auth, now)
            epoch = str(uuid.uuid4())
            updated = {**session, "status": "revoked", "revision": session["revision"] + 1,
                       "revoked_at": now, "logout_epoch": epoch}
            new_user = deepcopy(user)
            new_user.update(epoch=epoch, revision=user["revision"] + 1, updated_at=now)
            new_user["slots"] = {key: {"completed": False, "completed_by_attempt": None,
                                       "completed_at": None, "open_attempts": 0} for key in user["slots"]}
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
                    _key("SESSION", auth.session_id, "AUTH"), _key("ATTEMPT", attempt_id, "META"),
                    _key("USER", auth.principal, "STATE"),
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
            new_user = None
            if attempt["epoch"] == user["epoch"] and attempt["active_counted"]:
                slot_key = f'{attempt["program_id"]}:{attempt["target"]}'
                if self._slot(user, slot_key)["open_attempts"] <= 0:
                    raise _unavailable()
                new_user = deepcopy(user)
                new_user["slots"][slot_key]["open_attempts"] -= 1
                new_user.update(revision=user["revision"] + 1, updated_at=now)
            actions = [self._session_condition(auth, now), self._attempt_action(attempt, updated),
                       self._user_action(user, new_user)]
            binding = _optional_course_binding(attempt)
            if (binding is not None and binding["start_role"] == "final_assessment"
                    and attempt["epoch"] == user["epoch"]):
                pk = f"COURSE#{binding['scope_key']}"
                head, final = self._read_only([
                    {"PK": pk, "SK": f"EPOCH#{attempt['epoch']}#HEAD"},
                    {"PK": pk, "SK": f"EPOCH#{attempt['epoch']}#FINAL"},
                ])
                if (head is None or final is None or final.get("phase") != "active"
                        or final.get("active_attempt_id") != attempt_id):
                    raise _unavailable()
                for row, changes in ((head, {}), (final, {"phase": "free", "active_attempt_id": None})):
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
        return self._get(_key("ATTEMPT", attempt_id, "META"))

    def reauthorize_attempt(self, auth, attempt_id, expected_resume_digest):
        for _ in range(self.max_conflict_retries):
            try:
                session, attempt = self._read([
                    _key("SESSION", auth.session_id, "AUTH"), _key("ATTEMPT", attempt_id, "META"),
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
                _key("SESSION", attempt["bound_session_id"], "AUTH"),
                "#r = :revision AND #p = :principal AND (#s = :revoked OR (#s = :active AND #e <= :now))",
                {"#r": "revision", "#p": "principal", "#s": "status", "#e": "expires_at"},
                {":revision": old_session["revision"], ":principal": auth.principal,
                 ":revoked": "revoked", ":active": "active", ":now": now},
            )
            updated = {**attempt, "bound_session_id": auth.session_id, "revision": attempt["revision"] + 1}
            attempt_action = self._attempt_action(attempt, updated)
            attempt_action["Put"]["ConditionExpression"] += " AND #digest = :digest"
            attempt_action["Put"]["ExpressionAttributeNames"]["#digest"] = "resume_digest"
            attempt_action["Put"]["ExpressionAttributeValues"].update(_encode({":digest": expected_resume_digest}))
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

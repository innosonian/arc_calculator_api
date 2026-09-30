"""Typed JourneyService commands behind the /api/v2 hooks (D103).

The removed v1 body commands checked each control field as a nonempty str of
at most 256 UTF-8 bytes (INVALID_REQUEST) before any state access, and recorded
login_succeeded / attempt_reauthorized only after the state write. The removed
reauthorize command also read the rebound row into its attempt view before the
event, so a row that view could not show failed (503) without the event. The
typed commands keep those checks, their order and the events; these tests pin
them without HTTP, then once more through the public v2 routes.
"""

import json
from types import SimpleNamespace

import pytest

from mock_journey.errors import JourneyError
from mock_journey.service import CANCEL_REASONS, JourneyService
from services.operational_logs import log_context
from tests.journey_support import EventLog, JourneyStore, V2Journey, dummy_course


ATTEMPT = "a1234567-1234-4234-9234-123456789abc"
# The fields the removed attempt view read from a rebound row (and from its definition_json).
VIEWABLE = {
    "attempt_id": ATTEMPT, "state": "created", "program_id": "mock-cpr", "target": "child", "epoch": 0,
    "profile_name": "cpr", "definition_json": json.dumps({
        "condition": {}, "calculation_profile": "cpr", "goal": {}, "catalog_version": "c", "profile_version": "p",
    }),
}
AUTH = SimpleNamespace(session_id="session-a", principal="dummy-tester", revision=0, expires_at=86410)
# (value, accepted): 256 UTF-8 bytes is the inclusive limit; str length is not the measure.
TEXT_BOUNDARY = [
    ("x" * 256, True), ("x" * 257, False), ("가" * 85 + "x", True), ("가" * 86, False),
    ("", False), (None, False), (256, False), (b"x", False), (["x"], False), ("\ud800", False),
]


class Recorder:
    def __init__(self):
        self.records = []

    def record(self, category, event, fields):
        self.records.append((event, fields))
        return True


class Calls:
    """State/auth double that records the order of every call it receives."""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.calls.append((name, args))
            return self.results.get(name)
        return call


def service():
    state, auth = Calls(), Calls()
    state.results = {"get_attempt_for_reauthorization": {"attempt_id": ATTEMPT}, "reauthorize_attempt": VIEWABLE}
    auth.results = {
        "login": ({"session_id": "5e551000-0000-4000-8000-000000000001", "expires_at": 86410}, "s1.token"),
        "verify_resume": "digest-a",
    }
    return JourneyService(state, auth, object()), state, auth


def run(operation):
    recorder = Recorder()
    with log_context(recorder, request_id="command-test"):
        result = operation()
    return result, [event for event, _ in recorder.records], recorder.records


def assert_invalid(operation):
    with pytest.raises(JourneyError) as raised:
        run(operation)
    assert raised.value.code == "INVALID_REQUEST"


def test_calculation_is_required_at_construction():
    with pytest.raises(ValueError):
        JourneyService(object(), object(), None)


@pytest.mark.parametrize("value,accepted", TEXT_BOUNDARY)
@pytest.mark.parametrize("field", ["login_id", "password"])
def test_login_fields_are_checked_before_authentication(field, value, accepted):
    journey, state, auth = service()
    values = {"login_id": "test@test.com", "password": "2222", field: value}
    if not accepted:
        assert_invalid(lambda: journey.login_command(values["login_id"], values["password"]))
        assert auth.calls == [] and state.calls == []
        return
    (session, token), events, _ = run(lambda: journey.login_command(values["login_id"], values["password"]))
    assert auth.calls == [("login", (values["login_id"], values["password"]))]
    assert (session, token) == auth.results["login"]
    assert events == ["login_succeeded"]


def test_login_event_follows_the_session_write_and_carries_the_session_id():
    journey, state, auth = service()
    _, events, records = run(lambda: journey.login_command("test@test.com", "2222"))
    assert events == ["login_succeeded"]
    assert records[0][1]["session_id"] == "5e551000-0000-4000-8000-000000000001"


def test_failed_login_records_no_success():
    journey, state, auth = service()

    def refuse(*args):
        auth.calls.append(("login", args))
        raise JourneyError("LOGIN_FAILED")

    auth.login = refuse
    recorder = Recorder()
    with log_context(recorder, request_id="command-test"), pytest.raises(JourneyError) as raised:
        journey.login_command("test@test.com", "wrong")
    assert raised.value.code == "LOGIN_FAILED" and recorder.records == []


def test_check_session_is_one_coherent_session_user_read():
    journey, state, auth = service()
    assert journey.check_session(AUTH) is None
    assert state.calls == [("read_session_user", (AUTH,))]


@pytest.mark.parametrize("value,accepted", TEXT_BOUNDARY)
def test_reauthorize_shape_is_checked_before_the_attempt_is_read(value, accepted):
    journey, state, auth = service()
    if not accepted:
        assert_invalid(lambda: journey.reauthorize_command(AUTH, ATTEMPT, value))
        assert state.calls == [] and auth.calls == []
        return
    result, events, records = run(lambda: journey.reauthorize_command(AUTH, ATTEMPT, value))
    assert result is None
    assert [name for name, _ in state.calls] == ["get_attempt_for_reauthorization", "reauthorize_attempt"]
    assert auth.calls == [("verify_resume", ({"attempt_id": ATTEMPT}, "dummy-tester", value))]
    assert state.calls[1] == ("reauthorize_attempt", (AUTH, ATTEMPT, "digest-a"))
    assert events == ["attempt_reauthorized"] and records[0][1]["attempt_id"] == ATTEMPT


def test_invalid_proof_is_not_found_after_the_read_and_records_nothing():
    journey, state, auth = service()

    def refuse(*args):
        auth.calls.append(("verify_resume", args))
        raise JourneyError("NOT_FOUND")

    auth.verify_resume = refuse
    recorder = Recorder()
    with log_context(recorder, request_id="command-test"), pytest.raises(JourneyError) as raised:
        journey.reauthorize_command(AUTH, ATTEMPT, "wrong")
    assert raised.value.code == "NOT_FOUND"
    assert [name for name, _ in state.calls] == ["get_attempt_for_reauthorization"]
    assert recorder.records == []


def _definition_without(key):
    return json.dumps({name: value for name, value in json.loads(VIEWABLE["definition_json"]).items() if name != key})


# (label, rebound row, exception the removed attempt view raised for it)
UNVIEWABLE = [
    ("definition_not_json", {**VIEWABLE, "definition_json": "{not json"}, ValueError),
    ("definition_not_text", {**VIEWABLE, "definition_json": 7}, TypeError),
    ("definition_not_object", {**VIEWABLE, "definition_json": "[]"}, TypeError),
    ("definition_without_goal", {**VIEWABLE, "definition_json": _definition_without("goal")}, KeyError),
    ("definition_without_profile_version", {**VIEWABLE, "definition_json": _definition_without("profile_version")},
     KeyError),
    ("row_without_definition", {key: value for key, value in VIEWABLE.items() if key != "definition_json"}, KeyError),
    ("row_without_profile_name", {key: value for key, value in VIEWABLE.items() if key != "profile_name"}, KeyError),
    ("row_without_epoch", {key: value for key, value in VIEWABLE.items() if key != "epoch"}, KeyError),
]


@pytest.mark.parametrize("row,error", [case[1:] for case in UNVIEWABLE], ids=[case[0] for case in UNVIEWABLE])
def test_reauthorized_row_the_view_could_not_show_fails_after_the_write_and_before_the_event(row, error):
    journey, state, auth = service()
    state.results["reauthorize_attempt"] = row
    recorder = Recorder()
    with log_context(recorder, request_id="command-test"), pytest.raises(error):
        journey.reauthorize_command(AUTH, ATTEMPT, "r1.v1.proof")
    assert [name for name, _ in state.calls] == ["get_attempt_for_reauthorization", "reauthorize_attempt"]
    assert recorder.records == []


@pytest.mark.parametrize("reason", ["user_cancelled", "connection_lost", "USER_STOPPED", "x" * 257, "", None])
def test_cancel_accepts_only_the_internal_reasons_before_state_access(reason):
    journey, state, auth = service()
    assert_invalid(lambda: journey.cancel_command(AUTH, ATTEMPT, reason))
    assert state.calls == []


@pytest.mark.parametrize("reason", CANCEL_REASONS)
def test_cancel_passes_an_internal_reason_to_the_state_transaction(reason):
    journey, state, auth = service()
    assert run(lambda: journey.cancel_command(AUTH, ATTEMPT, reason))[0] is None
    assert state.calls == [("cancel_attempt", (AUTH, ATTEMPT, reason))]


def test_internal_cancel_reasons_are_the_wire_mapping_values():
    from mock_journey.course_contracts import CANCEL_REASON_WIRE_TO_INTERNAL
    assert set(CANCEL_REASONS) == set(CANCEL_REASON_WIRE_TO_INTERNAL.values())


# -- the same checks through the public /api/v2 routes ---------------------------------------

@pytest.fixture
def h():
    return V2Journey(JourneyStore.memory(), events=EventLog())


def operations(h):
    return [(record["event"], record["fields"].get("error_code")) for record in h.events.operations()]


def test_v2_password_over_256_utf8_bytes_is_invalid_request_without_a_session(h):
    # A loginId other than the Dummy login never reaches the command (CONTRACT_PENDING).
    body = {"loginId": "test@test.com", "password": "가" * 86}
    assert h.expect(400, "POST", "/api/v2/sessions/", body=body)["code"] == "INVALID_REQUEST"
    assert [row for row in h.store.rows() if row["PK"].startswith(("SESSION#", "USER#"))] == []
    assert operations(h) == [("login_failed", "INVALID_REQUEST")]


def test_v2_login_at_256_bytes_reaches_authentication(h):
    body = {"loginId": "test@test.com", "password": "x" * 256}
    assert h.expect(401, "POST", "/api/v2/sessions/", body=body)["code"] == "LOGIN_FAILED"
    assert operations(h) == [("login_failed", "LOGIN_FAILED")]


def test_v2_reauthorize_distinguishes_control_shape_from_invalid_proof(h):
    course = dummy_course("mock-cpr", "child")
    token = h.login().token
    attempt_id = h.start(token, course, course.practice_link_id)["attemptId"]
    before = h.store.row(f"ATTEMPT#{attempt_id}", "META")
    requests = []
    h.store.client.before_call = lambda operation, request: requests.append(repr(request))
    assert h.reauthorize(token, attempt_id, "x" * 257, expected=400)["code"] == "INVALID_REQUEST"
    assert not any(f"ATTEMPT#{attempt_id}" in request for request in requests)
    assert h.reauthorize(token, attempt_id, "r1.v1.wrong", expected=404)["code"] == "NOT_FOUND"
    assert any(f"ATTEMPT#{attempt_id}" in request for request in requests)
    assert h.store.row(f"ATTEMPT#{attempt_id}", "META") == before
    assert "attempt_reauthorized" not in [event for event, _ in operations(h)]


def _damage(h, attempt_id, damage):
    key = {"PK": {"S": f"ATTEMPT#{attempt_id}"}, "SK": {"S": "META"}}
    item = h.store.client.get_item(TableName=h.store.table, Key=key, ConsistentRead=True)["Item"]
    if damage == "definition_not_json":
        item["definition_json"] = {"S": "{not json"}
    elif damage == "definition_without_goal":
        definition = json.loads(item["definition_json"]["S"])
        definition.pop("goal")
        item["definition_json"] = {"S": json.dumps(definition)}
    else:
        item.pop("profile_name")
    h.store.client.put_item(TableName=h.store.table, Item=item)


@pytest.mark.parametrize("damage", ["definition_not_json", "definition_without_goal", "row_without_profile_name"])
def test_v2_reauthorize_of_a_row_the_view_could_not_show_is_503_without_the_event(h, damage):
    """As before D103: the rebinding write commits, then the view read fails before attempt_reauthorized."""
    course = dummy_course("mock-cpr", "child")
    first = h.login()
    started = h.start(first.token, course, course.practice_link_id)
    attempt_id = started["attemptId"]
    h.logout(first.token)
    second = h.login()
    _damage(h, attempt_id, damage)
    seen = len(h.events.operations())
    reply = h.reauthorize(second.token, attempt_id, started["resumeCredential"], expected=503)
    assert reply["code"] == "TEMPORARILY_UNAVAILABLE"
    after = [(record["event"], record["fields"].get("error_code")) for record in h.events.operations()[seen:]]
    assert ("attempt_reauthorized", None) not in after
    assert ("request_rejected", "TEMPORARILY_UNAVAILABLE") in after
    assert h.store.row(f"ATTEMPT#{attempt_id}", "META")["bound_session_id"] == second.session_id


def test_v2_reauthorize_of_a_viewable_row_records_the_event_once(h):
    """Positive control for the test above: the same flow without damage."""
    course = dummy_course("mock-cpr", "child")
    first = h.login()
    started = h.start(first.token, course, course.practice_link_id)
    h.logout(first.token)
    second = h.login()
    seen = len(h.events.operations())
    assert h.reauthorize(second.token, started["attemptId"], started["resumeCredential"])["attemptId"] == started[
        "attemptId"]
    events = [record["event"] for record in h.events.operations()[seen:]]
    assert events.count("attempt_reauthorized") == 1 and "request_rejected" not in events

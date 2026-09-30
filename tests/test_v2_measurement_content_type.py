"""/api/v2 measurement upload Content-Type resolution (D103: ported from the removed v1 upload).

POST /api/v2/attempts/{attemptId}/calculation/ resolves the upload Content-Type
once, after authentication and before the attempt is read or the measurement
parser runs (mock_journey.course_wiring.measurement_event):

* a Content-Type duplicated by letter case (in headers or in
  multiValueHeaders), a multiValueHeaders value that is not exactly one item,
  or a headers/multiValueHeaders disagreement is ambiguous: the v2 envelope
  400 INVALID_REQUEST, no ATTEMPT read, no parser call, no JOB, no object;
* a present Content-Type value, from headers or multiValueHeaders, must be one
  the removed v1 upload accepted (a nonempty str without a comma, CR or LF);
  otherwise 400 INVALID_REQUEST before the attempt read, as in v1;
* a multiValueHeaders-only Content-Type is then copied once into the headers of
  a new event for the parser; the caller's event is not changed, and a valid
  headers Content-Type reaches the parser unchanged.

The representation rules are course_http.header_representation, shared with
the Authorization header.

The application, sessions, started attempt and the "nothing accepted"/"never
read" checks are the shared boundary harness (tests/v2_boundary_support.py):
the real course_v2 composition on the in-memory DynamoDB client, prepared once
per module and forked for every test.
"""

from copy import deepcopy
import json

import pytest

from mock_journey.course_errors import CourseError
from mock_journey.course_wiring import measurement_event
from tests.journey_support import MULTIPART_CONTENT_TYPE
from tests.v2_boundary_support import (  # noqa: F401 (prepared_world, world fixtures)
    assert_invalid_request, assert_nothing_accepted, attempt_reads, attempt_row, call, forbid_parser, job_rows,
    parser_spy, prepared_world, rejected_events, saved_measurement, upload_event, world,
)


UNKNOWN_ATTEMPT = "00000000-0000-4000-8000-000000000000"


# (name, headers change, multiValueHeaders). "{CT}" is the valid multipart Content-Type, so only
# the ambiguity -- never an invalid media type -- causes each rejection.
AMBIGUOUS = [
    ("headers_multi_conflict", None, {"content-type": ["application/json"]}),
    ("two_multi_values", None, {"content-type": ["{CT}", "{CT}"]}),
    ("case_duplicate_headers", {"content-type": "{CT}"}, None),
    ("case_duplicate_multi", None, {"Content-Type": ["{CT}"], "content-type": ["{CT}"]}),
    ("empty_multi_list", None, {"content-type": []}),
    ("multi_not_a_list", None, {"content-type": "{CT}"}),
    ("multi_only_two_values", "drop", {"content-type": ["{CT}", "{CT}"]}),
]


def ambiguous_event(world, headers_change, multi, **kwargs):
    def fill(value):
        if type(value) is list:
            return [fill(item) for item in value]
        return value.replace("{CT}", MULTIPART_CONTENT_TYPE)

    value = upload_event(world, **kwargs)
    if headers_change == "drop":
        value["headers"].pop("Content-Type")
    elif headers_change is not None:
        value["headers"].update({key: fill(item) for key, item in headers_change.items()})
    if multi is not None:
        value["multiValueHeaders"] = {key: fill(item) for key, item in multi.items()}
    return value


@pytest.mark.parametrize("name,headers_change,multi", AMBIGUOUS, ids=[row[0] for row in AMBIGUOUS])
def test_ambiguous_content_type_is_refused_before_attempt_read_parser_or_acceptance(
        world, monkeypatch, name, headers_change, multi):
    forbid_parser(monkeypatch)
    value = ambiguous_event(world, headers_change, multi)
    before = deepcopy(value)
    assert_invalid_request(call(world, value))
    assert value == before
    assert attempt_reads(world) == []
    assert_nothing_accepted(world)
    rejected = rejected_events(world)
    assert [(r["fields"]["error_code"], r["fields"]["http_status"]) for r in rejected] == [("INVALID_REQUEST", 400)]
    assert not any(record["event"] == "calculation_accepted" for record in world.events.operations())


@pytest.mark.parametrize("who", ["other_session", "unknown_attempt"])
def test_ambiguity_is_decided_before_ownership_so_no_attempt_existence_is_revealed(world, monkeypatch, who):
    forbid_parser(monkeypatch)
    attempt_id = UNKNOWN_ATTEMPT if who == "unknown_attempt" else world.attempt_id
    value = ambiguous_event(world, None, {"content-type": ["application/json"]},
                            token=world.other.token, attempt_id=attempt_id)
    assert_invalid_request(call(world, value))
    assert attempt_reads(world, attempt_id) == []
    assert_nothing_accepted(world)


@pytest.mark.parametrize("credential", ["missing", "expired", "revoked"])
def test_authentication_is_checked_before_the_content_type(world, monkeypatch, credential):
    forbid_parser(monkeypatch)
    value = ambiguous_event(world, {"content-type": "{CT}"}, None)
    expected = (401, "SESSION_REQUIRED")
    if credential == "missing":
        value["headers"].pop("Authorization")
    elif credential == "expired":
        world.h.advance(86400)
        expected = (401, "SESSION_EXPIRED")
    else:
        world.h.logout(world.other.token)
        value["headers"]["Authorization"] = "Bearer " + world.other.token
        expected = (403, "SESSION_REVOKED")
    reply = call(world, value)
    assert (reply.status, reply.error["code"]) == expected
    assert attempt_reads(world) == []
    assert_nothing_accepted(world)


def test_multi_value_only_content_type_is_normalized_once_without_changing_the_event(world, monkeypatch):
    from mock_journey.legacy_bridge import MeasurementInputError, parse_measurement

    value = upload_event(world)
    value["headers"].pop("Content-Type")
    value["multiValueHeaders"] = {"content-type": [MULTIPART_CONTENT_TYPE]}
    before = deepcopy(value)
    # Positive control: the parser itself does not read multiValueHeaders.
    with pytest.raises(MeasurementInputError):
        parse_measurement(deepcopy(value))
    seen = parser_spy(monkeypatch)
    reply = call(world, value)
    assert reply.status == 202 and reply.data["calculationStatus"] == "pending"
    assert value == before
    assert len(seen) == 1
    parsed_event, parsed_copy = seen[0]
    assert parsed_event is not value
    assert parsed_copy["headers"] == {**before["headers"], "Content-Type": MULTIPART_CONTENT_TYPE}
    assert parsed_copy["multiValueHeaders"] == before["multiValueHeaders"]
    assert {key: parsed_copy[key] for key in ("httpMethod", "path", "body", "isBase64Encoded")} == {
        key: before[key] for key in ("httpMethod", "path", "body", "isBase64Encoded")}
    assert saved_measurement(world) == world.measurement
    assert len(job_rows(world)) == 1 and attempt_row(world)["state"] == "queued"


@pytest.mark.parametrize("multi", [None, {"Content-Type": ["{CT}"]}, {"content-type": ["{CT}"]}],
                         ids=["single_header_only", "matching_multi", "matching_multi_lowercase"])
def test_consistent_content_type_reaches_the_parser_unchanged(world, monkeypatch, multi):
    value = ambiguous_event(world, None, multi)
    before = deepcopy(value)
    seen = parser_spy(monkeypatch)
    reply = call(world, value)
    assert reply.status == 202 and reply.data["calculationStatus"] == "pending"
    assert len(seen) == 1 and seen[0][0] is value and seen[0][1] == before
    assert saved_measurement(world) == world.measurement
    assert len(job_rows(world)) == 1
    assert rejected_events(world) == []


# multiValueHeaders-only values the removed v1 upload refused (400) instead of adopting.
NOT_ADOPTED = [
    ("not_a_string", 123),
    ("empty", ""),
    ("carriage_return_line_feed", "{CT}\r\nX-Injected: 1"),
    ("line_feed", "{CT}\n"),
    ("comma", "{CT}, text/plain"),
]


@pytest.mark.parametrize("value", [row[1] for row in NOT_ADOPTED], ids=[row[0] for row in NOT_ADOPTED])
def test_multi_value_only_content_type_the_v1_upload_refused_is_not_adopted(world, monkeypatch, value):
    forbid_parser(monkeypatch)
    item = value.replace("{CT}", MULTIPART_CONTENT_TYPE) if type(value) is str else value
    event = ambiguous_event(world, "drop", None)
    event["multiValueHeaders"] = {"content-type": [item]}
    before = deepcopy(event)
    assert_invalid_request(call(world, event))
    assert event == before
    assert attempt_reads(world) == []
    assert_nothing_accepted(world)


# Values given in headers (alone, or with the same multi-value) follow the same value rule
# as the removed v1 upload (D103 port, completed by the integrator): a Content-Type that is
# not a nonempty str, or that carries a comma, CR or LF, is refused with 400 before the
# parser runs and before any attempt is read. (Before the port v2 answered 422 for most of
# these and accepted CR/LF with 202.)
HEADER_VALUES = [
    ("not_a_string", 123),
    ("none", None),
    ("empty", ""),
    ("carriage_return_line_feed", "{CT}\r\nX-Injected: 1"),
    ("line_feed", "{CT}\nX-Injected: 1"),
    ("comma", "{CT}, text/plain"),
]


@pytest.mark.parametrize("with_multi", [False, True], ids=["headers_only", "with_same_multi"])
@pytest.mark.parametrize("value", [row[1] for row in HEADER_VALUES], ids=[row[0] for row in HEADER_VALUES])
def test_headers_content_type_value_the_v1_upload_refused_is_rejected_before_the_parser(
        world, monkeypatch, value, with_multi):
    forbid_parser(monkeypatch)
    item = value.replace("{CT}", MULTIPART_CONTENT_TYPE) if type(value) is str else value
    event = upload_event(world)
    event["headers"]["Content-Type"] = item
    if with_multi:
        event["multiValueHeaders"] = {"Content-Type": [item]}
    before = deepcopy(event)
    assert_invalid_request(call(world, event))
    assert event == before
    assert attempt_reads(world) == []
    assert_nothing_accepted(world)


def test_measurement_event_contract_without_http():
    single = {"headers": {"Content-Type": "a"}, "multiValueHeaders": {"Content-Type": ["a"]}, "body": "x"}
    assert measurement_event(single) is single
    headers = {"Authorization": "Bearer t"}
    multi_only = {"headers": headers, "multiValueHeaders": {"content-type": ["a"]}, "body": "x"}
    normalized = measurement_event(multi_only)
    assert normalized is not multi_only
    assert normalized == {**multi_only, "headers": {"Authorization": "Bearer t", "Content-Type": "a"}}
    assert multi_only["headers"] is headers and headers == {"Authorization": "Bearer t"}
    no_headers = {"multiValueHeaders": {"CONTENT-TYPE": ["a"]}}
    assert measurement_event(no_headers) == {**no_headers, "headers": {"Content-Type": "a"}}
    absent = {"headers": None, "multiValueHeaders": None}
    assert measurement_event(absent) is absent
    for value in (123, "", None, "a\r\nb", "a\nb", "a\rb", "a, b"):
        for refused in ({"headers": {"Content-Type": value}},
                        {"headers": {"Content-Type": value}, "multiValueHeaders": {"content-type": [value]}}):
            with pytest.raises(CourseError) as raised:
                measurement_event(refused)
            assert raised.value.code == "INVALID_REQUEST"
    for bad in ({"headers": [], "multiValueHeaders": {}}, {"headers": {}, "multiValueHeaders": "x"},
                {"headers": {"Content-Type": "a"}, "multiValueHeaders": {"content-type": ["b"]}},
                {"headers": {"Content-Type": "a", "CONTENT-TYPE": "a"}},
                {"multiValueHeaders": {"content-type": [123]}}, {"multiValueHeaders": {"content-type": [""]}},
                {"multiValueHeaders": {"content-type": ["a\r\nb"]}},
                {"multiValueHeaders": {"content-type": ["a\rb"]}},
                {"multiValueHeaders": {"content-type": ["a, b"]}},
                {"multiValueHeaders": {"content-type": [None]}}, "not-an-event"):
        with pytest.raises(CourseError) as raised:
            measurement_event(bad)
        assert raised.value.code == "INVALID_REQUEST"


def test_rejection_body_is_the_fixed_v2_envelope(world, monkeypatch):
    forbid_parser(monkeypatch)
    reply = call(world, ambiguous_event(world, None, {"content-type": ["{CT}", "{CT}"]}))
    assert_invalid_request(reply)
    assert set(reply.body) == {"success", "error", "timestamp"}
    assert reply.error == {"code": "INVALID_REQUEST", "message": "Invalid request.", "details": None}
    assert MULTIPART_CONTENT_TYPE not in json.dumps(reply.raw)

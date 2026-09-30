"""REST proxy header resolution shared by every /api/v2 route (mock_journey.course_http).

course_http.header_representation resolves one header from ``headers`` and
``multiValueHeaders`` without judging its value; _single_header adds the value
rules for Authorization and the JSON Content-Type, and
course_wiring.measurement_event uses the same representation rules for the
upload Content-Type (tests/test_v2_measurement_content_type.py).

The positive Bearer representation case replaces the removed v1
extract_bearer test (``AUTHORIZATION: bearer ...`` with the same
``Authorization`` multi-value is one credential).
"""

import pytest

from mock_journey.course_errors import CourseError
from mock_journey.course_http import _bearer, _single_header, header_representation
from tests.journey_support import DUMMY_LOGIN, EventLog, JourneyStore, V2Journey


def refused(operation, code="INVALID_REQUEST"):
    with pytest.raises(CourseError) as raised:
        operation()
    assert raised.value.code == code


# (event, expected (values, multi_only))
RESOLVED = [
    ({}, ((), False)),
    ({"headers": None, "multiValueHeaders": None}, ((), False)),
    ({"headers": {"Other": "x"}, "multiValueHeaders": {"Other": ["x", "y"]}}, ((), False)),
    ({"headers": {"X-Name": "a"}}, (("a",), False)),
    ({"headers": {"x-name": "a"}, "multiValueHeaders": {"X-NAME": ["a"]}}, (("a",), False)),
    ({"multiValueHeaders": {"X-Name": ["a"]}}, (("a",), True)),
    ({"headers": {}, "multiValueHeaders": {"x-name": [""]}}, (("",), True)),
    ({"headers": {1: "ignored", "X-Name": "a"}}, (("a",), False)),
]

AMBIGUOUS = [
    {"headers": []},
    {"multiValueHeaders": "x"},
    {"headers": {"X-Name": "a", "x-name": "a"}},
    {"multiValueHeaders": {"X-Name": ["a"], "x-name": ["a"]}},
    {"multiValueHeaders": {"X-Name": "a"}},
    {"multiValueHeaders": {"X-Name": []}},
    {"multiValueHeaders": {"X-Name": ["a", "a"]}},
    {"headers": {"X-Name": "a"}, "multiValueHeaders": {"x-name": ["b"]}},
    {"headers": {"X-Name": "a"}, "multiValueHeaders": {"x-name": []}},
]


@pytest.mark.parametrize("event,expected", RESOLVED)
def test_representation_resolves_one_value_and_says_whether_only_multi_carried_it(event, expected):
    assert header_representation(event, "x-name") == expected


@pytest.mark.parametrize("event", AMBIGUOUS)
def test_ambiguous_representation_is_invalid_request(event):
    refused(lambda: header_representation(event, "x-name"))
    refused(lambda: _single_header(event, "x-name"))


@pytest.mark.parametrize("value", [1, None, "", "a\rb", "a\nb", ["a"]])
@pytest.mark.parametrize("multi", [False, True])
def test_single_header_refuses_values_that_are_not_one_clean_line(value, multi):
    event = {"multiValueHeaders": {"X-Name": [value]}} if multi else {"headers": {"X-Name": value}}
    refused(lambda: _single_header(event, "x-name"))


def test_single_header_keeps_a_clean_value_including_a_comma():
    assert _single_header({"headers": {"X-Name": "a, b"}}, "x-name") == "a, b"
    assert _single_header({"multiValueHeaders": {"x-name": ["a; b=c"]}}, "x-name") == "a; b=c"
    assert _single_header({}, "x-name") is None


def test_bearer_scheme_is_case_insensitive_across_agreeing_representations():
    assert _bearer({"headers": {"AUTHORIZATION": "bearer opaque"},
                    "multiValueHeaders": {"Authorization": ["bearer opaque"]}}) == "opaque"
    assert _bearer({"multiValueHeaders": {"authorization": ["BEARER opaque"]}}) == "opaque"


@pytest.mark.parametrize("event,code", [
    ({}, "SESSION_REQUIRED"),
    ({"headers": {"Authorization": "Bearer "}}, "SESSION_REQUIRED"),
    ({"headers": {"Authorization": "Bearer token extra"}}, "SESSION_REQUIRED"),
    ({"headers": {"Authorization": "Basic token"}}, "SESSION_REQUIRED"),
    ({"headers": {"Authorization": "Bearer token", "authorization": "Bearer token"}}, "INVALID_REQUEST"),
    ({"multiValueHeaders": {"Authorization": ["Bearer token", "Bearer token"]}}, "INVALID_REQUEST"),
    ({"headers": {"Authorization": "Bearer first"}, "multiValueHeaders": {"authorization": ["Bearer second"]}},
     "INVALID_REQUEST"),
    ({"headers": {"Authorization": ["Bearer token"]}}, "INVALID_REQUEST"),
])
def test_missing_or_ambiguous_bearer_is_refused(event, code):
    refused(lambda: _bearer(event), code)


# -- through the public routes --------------------------------------------------------------

@pytest.fixture
def h():
    return V2Journey(JourneyStore.memory(), events=EventLog())


def test_v2_session_accepts_one_bearer_given_in_both_representations_with_other_casing(h):
    token = h.login().token
    event = {"httpMethod": "GET", "path": "/api/v2/session/", "headers": {"AUTHORIZATION": "bearer " + token},
             "multiValueHeaders": {"Authorization": ["bearer " + token]}, "body": ""}
    reply = h.call("GET", event["path"], event=event)
    assert reply.status == 200, reply.body


def test_v2_login_json_content_type_follows_the_same_representation_rules(h):
    import json

    def login(headers, multi):
        event = {"httpMethod": "POST", "path": "/api/v2/sessions/", "headers": headers,
                 "multiValueHeaders": multi, "body": json.dumps(DUMMY_LOGIN), "isBase64Encoded": False}
        return h.call("POST", event["path"], event=event)

    assert login({}, {"content-type": ["application/json"]}).status == 201
    assert login({"CONTENT-TYPE": "application/json"}, {"Content-Type": ["application/json"]}).status == 201
    for headers, multi in (({"Content-Type": "application/json"}, {"content-type": ["text/plain"]}),
                           ({"Content-Type": "application/json", "content-type": "application/json"}, {}),
                           ({}, {"content-type": ["application/json", "application/json"]})):
        reply = login(headers, multi)
        assert (reply.status, reply.error["code"]) == (400, "INVALID_REQUEST"), reply.body

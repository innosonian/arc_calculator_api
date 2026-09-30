"""Characterization of the legacy wire parser and validator surface.

The expected values below are written from the reference wire contract (fixed
400 messages, V1 defaults, alias names, falsy fallbacks) and the D03 parity
rules. They are not copied from a current-output snapshot. The parser and validator
calls below go to the neutral ``services.http.legacy_request`` module that
owns them (D126). ``lambda_handler`` (regression helper) and
``mock_journey.legacy_bridge`` (the public measurement path) must expose the
same objects for the names they still use; the identity tests pin that.
"""

import base64
import json

import pytest

import lambda_handler
import mock_journey.legacy_bridge as bridge
from mock_journey.errors import JourneyError
from services.http import legacy_request
from services.http.schemas import DEFAULT_CONDITION


GUIDELINE = "This guideline is not supported."
TARGET = "This target is not supported."
TRAINING = "This training type is not supported."
CPR_FILE = "CPR file is required."
SANITIZED = "Invalid request data."
MULTIPART_REQUIRED = "Content-Type must be multipart/form-data"
EMPTY_BODY = "Empty body"

V1_DEFAULT_CONDITION = {
    "mode": "training", "target": "adult", "training_type": "cpr",
    "guideline": "AHA2020", "cpr_cycle_type": "302", "is_2rescuers": False,
}
DOCUMENT_KEYS = [
    "DeviceInfo", "Organization", "Dummy", "Open_Skill", "ResultSummary", "Custom",
    "ResultByCycle", "CalculationService", "Certification", "Usage", "ResultByCriteria", "Institution",
]
CREDENTIAL_KEYS = [
    "access_token", "refresh_token", "client_id", "client_secret", "token_expired",
    "access_token_url", "send_result_url", "source_endpoint",
]
MULTIPART_KEYS = ["cpr_b64_data", "aed_b64_data", "condition", "vp_event_list", "hstm_document",
                  *DOCUMENT_KEYS, *CREDENTIAL_KEYS]
FORM_KEYS = ["cpr_b64_data", "aed_b64_data", "condition", "vp_event_list", "hstm_document", *DOCUMENT_KEYS,
             "access_token", "refresh_token", "client_id", "client_secret", "access_token_url",
             "send_result_url", "source_endpoint", "token_expired"]

# D126: lambda_handler imports only what _run_trusted_calculation itself calls...
REEXPORTED_NAMES = ("ClientError", "_MSG_SANITIZED_CLIENT_ERROR", "_get_content_type", "_parse_multipart_body",
                    "_validate_legacy_document", "_validate_request")
# ...and no longer carries the historic test-only surface.
DROPPED_NAMES = ("_MSG_UNSUPPORTED_GUIDELINE", "_MSG_UNSUPPORTED_TARGET", "_MSG_UNSUPPORTED_TRAINING_TYPE",
                 "_MSG_CPR_FILE_REQUIRED", "_SUPPORTED_GUIDELINES", "_SUPPORTED_TARGETS",
                 "_SUPPORTED_TRAINING_TYPES", "_VP_EVENT_IDS", "DEFAULT_CONDITION")
BRIDGE_NAMES = ("ClientError", "_get_content_type", "_parse_multipart_body", "_validate_legacy_document",
                "_validate_request")

_BOUNDARY = "p1-legacy-request-boundary"
_CONTENT_TYPE = f"multipart/form-data; boundary={_BOUNDARY}"


def multipart(parts):
    """Ordered (name, payload) parts; repeated names are allowed on purpose."""
    chunks = []
    for name, payload in parts:
        payload = payload.encode("utf-8") if isinstance(payload, str) else payload
        chunks.append(f'--{_BOUNDARY}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode() + payload + b"\r\n")
    return b"".join(chunks) + f"--{_BOUNDARY}--\r\n".encode()


def parse_multipart(parts, *, headers=None, encoded=True):
    body = multipart(parts)
    wire = base64.b64encode(body).decode("ascii") if encoded else body.decode("utf-8")
    return legacy_request._parse_multipart_body(wire, headers or {"Content-Type": _CONTENT_TYPE}, encoded)


def form_body(fields):
    from urllib.parse import urlencode
    return base64.urlsafe_b64encode(urlencode(fields).encode("utf-8")).decode("ascii")


def valid_body(**overrides):
    body = {"condition": {"guideline": "ARC2020", "target": "adult", "training_type": "cpr"},
            "cpr_b64_data": b"\x01"}
    body.update(overrides)
    return body


def rejected(function, body):
    with pytest.raises(legacy_request.ClientError) as raised:
        function(body)
    assert type(raised.value) is legacy_request.ClientError
    return str(raised.value)


# --- surface and identity ---------------------------------------------------

def test_fixed_messages_sets_and_event_ids_keep_their_values_and_types():
    assert legacy_request._MSG_UNSUPPORTED_GUIDELINE == GUIDELINE
    assert legacy_request._MSG_UNSUPPORTED_TARGET == TARGET
    assert legacy_request._MSG_UNSUPPORTED_TRAINING_TYPE == TRAINING
    assert legacy_request._MSG_CPR_FILE_REQUIRED == CPR_FILE
    assert legacy_request._MSG_SANITIZED_CLIENT_ERROR == SANITIZED
    assert type(legacy_request._SUPPORTED_GUIDELINES) is set
    assert legacy_request._SUPPORTED_GUIDELINES == {"AHA2020", "ARC2020", "ARC2025", "ERC2020", "STD2015"}
    assert type(legacy_request._SUPPORTED_TARGETS) is set
    assert legacy_request._SUPPORTED_TARGETS == {"adult", "child", "infant"}
    assert type(legacy_request._SUPPORTED_TRAINING_TYPES) is set
    assert legacy_request._SUPPORTED_TRAINING_TYPES == {"cpr", "compression_only", "ventilation_only"}
    # Membership uses == on a tuple (0.0 and False are equal to event 0).
    assert legacy_request._VP_EVENT_IDS == (0, 1, 10, 11, 20, 21)
    assert type(legacy_request._VP_EVENT_IDS) is tuple


def test_client_error_is_one_value_error_class_named_for_the_log_allowlist():
    error = legacy_request.ClientError
    assert issubclass(error, ValueError) and error.__name__ == "ClientError"
    assert bridge.ClientError is error
    assert lambda_handler.ClientError is error


@pytest.mark.parametrize("name", REEXPORTED_NAMES)
def test_lambda_handler_reexports_the_neutral_objects(name):
    assert getattr(lambda_handler, name) is getattr(legacy_request, name)


@pytest.mark.parametrize("name", DROPPED_NAMES)
def test_lambda_handler_no_longer_carries_the_test_only_surface(name):
    # The owner module keeps the name; the entry module does not re-export it (D126).
    assert hasattr(legacy_request, name) or name == "DEFAULT_CONDITION"
    assert not hasattr(lambda_handler, name)


@pytest.mark.parametrize("name", BRIDGE_NAMES)
def test_public_bridge_uses_the_same_parser_objects(name):
    assert getattr(bridge, name) is getattr(legacy_request, name)


def test_trusted_helper_and_monkeypatch_seams_stay_in_lambda_handler():
    # Tests replace these module globals; _run_trusted_calculation looks them up at call time.
    import main
    from services.http.service import parse_body

    assert lambda_handler.run_calculator is main.run_calculator
    assert lambda_handler.parse_body is parse_body
    assert lambda_handler._run_trusted_calculation.__module__ == "lambda_handler"
    assert lambda_handler.run.__module__ == "lambda_handler"
    assert DEFAULT_CONDITION == V1_DEFAULT_CONDITION


# --- request validation ------------------------------------------------------

@pytest.mark.parametrize("body,message", [
    ({"condition": ["ARC2020"], "cpr_b64_data": b"\x01"}, SANITIZED),
    ({"condition": [], "cpr_b64_data": b"\x01"}, GUIDELINE),
    ({}, GUIDELINE),
    (valid_body(condition={"guideline": "arc2020", "target": "x", "training_type": "x"}, cpr_b64_data=b""), GUIDELINE),
    (valid_body(condition={"guideline": ["ARC2020"], "target": "adult", "training_type": "cpr"}), GUIDELINE),
    (valid_body(condition={"guideline": "HSTM2015", "target": "adult", "training_type": "cpr"}), GUIDELINE),
    (valid_body(condition={"guideline": "ARC2025", "target": "Adult", "training_type": "x"}), TARGET),
    (valid_body(condition={"guideline": "STD2015", "target": "infant", "training_type": "CPR"}), TRAINING),
    (valid_body(condition={"guideline": "ERC2020", "target": "child", "training_type": {"cpr": 1}}), TRAINING),
    (valid_body(cpr_b64_data=b"", vp_event_list="bad"), CPR_FILE),
    (valid_body(cpr_b64_data=None), CPR_FILE),
    (valid_body(vp_event_list="bad"), SANITIZED),
    (valid_body(vp_event_list=[1]), SANITIZED),
    (valid_body(vp_event_list=[{"event": 0, "timestamp": "1"}]), SANITIZED),
    (valid_body(vp_event_list=[{"event": 99, "timestamp": 1}]), SANITIZED),
    (valid_body(vp_event_list=[{"event": 0, "last_timestamp": 0, "timestamp": None}]), SANITIZED),
    (valid_body(Organization="org"), SANITIZED),
    (valid_body(Organization={"org_id": 5}), SANITIZED),
    (valid_body(Usage={"Regional_Option": 3}), SANITIZED),
])
def test_validate_request_first_violation_and_fixed_message(body, message):
    assert rejected(legacy_request._validate_request, body) == message


@pytest.mark.parametrize("overrides", [
    {},
    {"vp_event_list": {}}, {"vp_event_list": 0}, {"vp_event_list": ""}, {"vp_event_list": None},
    {"vp_event_list": [{"event": 0.0, "timestamp": True}]},
    {"vp_event_list": [{"event": 21, "last_timestamp": 0, "timestamp": 5.5}]},
    {"Organization": []}, {"Organization": {"org_id": 0}}, {"Organization": {"org_id": "o"}},
    {"Usage": "not-a-dict"}, {"Usage": {"Regional_Option": ""}}, {"Usage": {"Regional_Option": "uk"}},
])
def test_validate_request_keeps_reference_falsy_and_numeric_acceptance(overrides):
    assert legacy_request._validate_request(valid_body(**overrides)) is None


@pytest.mark.parametrize("body", [
    {"hstm_document": [1, 2]},
    {"hstm_document": "abc"},
    {"hstm_document": {"ResultSummary": 5}},
    {"ResultByCycle": 7},
    {"Organization": "x"},
    {"hstm_document": {"Dummy": ["x"]}},
    {"Usage": "x"},
    {"Usage": "x", "ResultSummary": {"HstreamId": ""}},
])
def test_validate_legacy_document_rejections_are_sanitized(body):
    assert rejected(legacy_request._validate_legacy_document, body) == SANITIZED


@pytest.mark.parametrize("body", [
    {},
    {"hstm_document": [("ResultSummary", {"HstreamId": "h"})]},
    {"hstm_document": {"Dummy": []}},
    {"Organization": None, "ResultSummary": None},
    {"Usage": "x", "ResultSummary": {"HstreamId": "h"}},
    {"Usage": "x", "ResultSummary": {"hstreamId": "h"}},
    # A truthy nested document replaces the top-level fields entirely.
    {"hstm_document": {"Open_Skill": {}}, "Organization": "ignored"},
])
def test_validate_legacy_document_acceptances(body):
    assert legacy_request._validate_legacy_document(body) is None


# --- multipart parser -------------------------------------------------------

@pytest.mark.parametrize("headers", [{}, {"Content-Type": "application/json"}, {"content-type": None}])
def test_multipart_requires_multipart_content_type(headers):
    with pytest.raises(legacy_request.ClientError) as raised:
        legacy_request._parse_multipart_body("eA==", headers, True)
    assert str(raised.value) == MULTIPART_REQUIRED


def test_multipart_empty_body_is_rejected_after_content_type():
    with pytest.raises(legacy_request.ClientError) as raised:
        legacy_request._parse_multipart_body("", {"CONTENT-TYPE": _CONTENT_TYPE}, True)
    assert str(raised.value) == EMPTY_BODY


def test_content_type_lookup_is_case_insensitive_first_match():
    assert legacy_request._get_content_type({"x": "1", "CoNtEnT-TyPe": "a/b", "content-type": "c/d"}) == "a/b"
    assert legacy_request._get_content_type(None) is None
    assert legacy_request._get_content_type({}) is None


def test_multipart_defaults_key_order_and_fresh_default_condition():
    first = parse_multipart([("unknown", "ignored")])
    second = parse_multipart([("unknown", "ignored")])
    assert list(first) == MULTIPART_KEYS
    assert first["cpr_b64_data"] == b"" and first["aed_b64_data"] == b""
    assert first["condition"] == V1_DEFAULT_CONDITION
    assert list(first["condition"]) == list(V1_DEFAULT_CONDITION)
    assert first["condition"] is not second["condition"]
    assert first["condition"] is not DEFAULT_CONDITION
    assert first["vp_event_list"] == []
    assert all(first[key] is None for key in ["hstm_document", *DOCUMENT_KEYS, *CREDENTIAL_KEYS])


def test_multipart_field_semantics_and_last_alias_wins():
    document = base64.urlsafe_b64encode(json.dumps({"Dummy": {"a": 1}}).encode()).decode()
    body = parse_multipart([
        ("rawHexBPfile", b"\x00\x01"), ("aedHexBPfile", b"\x02"),
        ("condition", json.dumps({"guideline": "ARC2020", "extra": 1})),
        ("vp_event_list", json.dumps([{"event": 0, "timestamp": 1}])),
        ("Organization", json.dumps({"org_id": "o"})), ("Custom", "{broken"),
        ("hstm_document_b64", document),
        ("access_token", "first"), ("hstm_access_token", "second"),
        ("hstm_token_expired", "YES"), ("client_secret", "s"),
    ])
    assert body["cpr_b64_data"] == b"\x00\x01" and body["aed_b64_data"] == b"\x02"
    assert body["condition"] == {**V1_DEFAULT_CONDITION, "guideline": "ARC2020", "extra": 1}
    assert list(body["condition"]) == [*V1_DEFAULT_CONDITION, "extra"]
    assert body["vp_event_list"] == [{"event": 0, "timestamp": 1}]
    assert body["Organization"] == {"org_id": "o"} and body["Custom"] is None
    assert body["hstm_document"] == {"Dummy": {"a": 1}}
    assert body["access_token"] == "second" and body["client_secret"] == "s"
    assert body["token_expired"] is True


@pytest.mark.parametrize("raw,expected", [("1", True), ("Y", True), ("true", True), ("no", False), ("", False)])
def test_multipart_token_expired_conversion(raw, expected):
    assert parse_multipart([("token_expired", raw)])["token_expired"] is expected


@pytest.mark.parametrize("condition", ["[1, 2]", "{broken", "null"])
def test_multipart_non_object_condition_falls_back_to_default(condition):
    assert parse_multipart([("condition", condition)])["condition"] == V1_DEFAULT_CONDITION


def test_multipart_invalid_nested_document_and_vp_list_fall_back():
    body = parse_multipart([("hstm_document", "{x"), ("vp_event_list", "{x")])
    assert body["hstm_document"] is None and body["vp_event_list"] == []
    assert parse_multipart([("hstm_document_b64", "%%%")])["hstm_document"] is None


def test_multipart_plain_text_body_when_not_base64_encoded():
    body = parse_multipart([("client_id", "c")], encoded=False)
    assert body["client_id"] == "c"


# --- form parser and shared default ------------------------------------------

def test_form_parser_key_order_and_defaults():
    from services.http.service import parse_body

    body = parse_body(form_body({"hstm_client_id": "c", "token_expired": "no", "hstm_token_expired": "yes"}))
    assert list(body) == FORM_KEYS
    assert body["condition"] == V1_DEFAULT_CONDITION
    assert body["client_id"] == "c" and body["token_expired"] is False
    assert body["vp_event_list"] == [] and body["cpr_b64_data"] == b""


@pytest.mark.parametrize("fields", [{}, {"condition": "{broken"}])
def test_form_default_condition_is_a_detached_copy(fields):
    from services.http.schemas import DEFAULT_CONDITION
    from services.http.service import parse_body

    first, second = parse_body(form_body(fields)), parse_body(form_body(fields))
    assert first["condition"] == V1_DEFAULT_CONDITION
    assert list(first["condition"]) == list(V1_DEFAULT_CONDITION)
    # A caller that edits its parsed condition must not reach the module default.
    assert first["condition"] is not DEFAULT_CONDITION
    assert first["condition"] is not second["condition"]


def test_form_condition_json_is_not_merged_with_defaults():
    from services.http.service import parse_body

    assert parse_body(form_body({"condition": json.dumps({"guideline": "ARC2020"})}))["condition"] == {
        "guideline": "ARC2020"}


# --- public bridge ------------------------------------------------------------

def test_bridge_maps_every_validator_rejection_to_one_fixed_error():
    event = {"headers": {"Content-Type": _CONTENT_TYPE}, "isBase64Encoded": True,
             "body": base64.b64encode(multipart([("rawHexBPfile", b"\x01")])).decode("ascii")}
    with pytest.raises(bridge.MeasurementInputError) as raised:
        bridge.parse_measurement(event)  # default AHA2020 condition passes; CPR length check fails
    assert isinstance(raised.value, JourneyError) and raised.value.code == "MEASUREMENT_INPUT_INVALID"
    event["headers"] = {"Content-Type": "text/plain"}
    event["body"] = "!!!"
    with pytest.raises(bridge.MeasurementInputError):
        bridge.parse_measurement(event)


# --- ratio rule shared by coaching text and compression-count scoring --------

@pytest.mark.parametrize("raw", ["152", "302", 152, " 152", "152 ", "15:2", "", None, "1520"])
def test_comp_vent_targets_matches_the_scoring_key_when_the_key_is_present(raw):
    # comp_vent_targets (services/http/schemas.py) feeds the pinned coaching
    # modules; CycleWithScore._get_comp_cnt_key (pinned) picks the scoring
    # border. Both compare the raw value with "152" and must agree. A missing
    # key differs on purpose (KeyError vs 30:2) and is not part of this rule.
    from types import SimpleNamespace

    from calculators.cycle_evaluator import CycleWithScore
    from services.http.schemas import comp_vent_targets

    condition = {"cpr_cycle_type": raw}
    fake = SimpleNamespace(calculation_config=SimpleNamespace(condition=condition))
    assert comp_vent_targets(condition) == ((15, 2) if raw == "152" else (30, 2))
    assert CycleWithScore._get_comp_cnt_key(fake) == ("comp_cnt_152" if raw == "152" else "comp_cnt_302")

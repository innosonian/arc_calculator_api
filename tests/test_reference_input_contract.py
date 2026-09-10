"""Reference form-wire compatibility; fixtures contain synthetic values only."""

import base64
import binascii
import json
from urllib.parse import urlencode

import pytest

from services.http.schemas import DEFAULT_CONDITION
from services.http.service import parse_body


def _form(parts):
    return base64.urlsafe_b64encode(urlencode(parts).encode("utf-8")).decode("ascii")


def _b64(value):
    return base64.urlsafe_b64encode(value).decode("ascii")


def test_form_decodes_outer_envelope_and_both_binary_parts():
    cpr = bytes([0, 168, 255, 13, 10, 128])
    aed = bytes([180, 0, 255, 17])
    parsed = parse_body(_form({"cpr_b64_data": _b64(cpr), "aed_b64_data": _b64(aed)}))
    assert parsed["cpr_b64_data"] == cpr
    assert parsed["aed_b64_data"] == aed


def test_omitted_parts_use_reference_defaults_without_requiring_cpr():
    parsed = parse_body(_form({}))
    assert parsed["cpr_b64_data"] == b""
    assert parsed["aed_b64_data"] == b""
    assert parsed["vp_event_list"] == []
    assert parsed["condition"] == {
        "mode": "training",
        "target": "adult",
        "training_type": "cpr",
        "guideline": "AHA2020",
        "cpr_cycle_type": "302",
        "is_2rescuers": False,
    }
    assert parsed["token_expired"] is None
    assert parsed["hstm_document"] is None


@pytest.mark.parametrize("condition", [{"target": "child"}, None, [], 0, False])
def test_form_condition_keeps_decoded_type_without_merging_defaults(condition):
    parsed = parse_body(_form({"condition": json.dumps(condition)}))
    assert parsed["condition"] == condition
    assert type(parsed["condition"]) is type(condition)


def test_malformed_json_uses_reference_fallbacks():
    parsed = parse_body(_form({"condition": "{", "vp_event_list": "[", "Custom": "{"}))
    assert parsed["condition"] == DEFAULT_CONDITION
    assert parsed["vp_event_list"] == []
    assert parsed["Custom"] is None


@pytest.mark.parametrize("events", [None, {}, 7, [{"event": 0, "timestamp": 20}]])
def test_form_vp_json_is_not_coerced_or_validated_by_parser(events):
    parsed = parse_body(_form({"vp_event_list": json.dumps(events)}))
    assert parsed["vp_event_list"] == events
    assert type(parsed["vp_event_list"]) is type(events)


def test_hstm_document_base64_fallback_and_top_level_json_values():
    document = {"ResultSummary": {"HstreamId": "synthetic-user"}}
    parsed = parse_body(_form({
        "hstm_document": "{}",
        "hstm_document_b64": _b64(json.dumps(document).encode("utf-8")),
        "ResultSummary": '{"CycleNum":3}',
        "Certification": '{"Target":"child"}',
        "DeviceInfo": "[]",
        "Custom": "false",
    }))
    assert parsed["hstm_document"] == document
    assert parsed["ResultSummary"] == {"CycleNum": 3}
    assert parsed["Certification"] == {"Target": "child"}
    assert parsed["DeviceInfo"] == []
    assert parsed["Custom"] is False


def test_legacy_aliases_and_primary_field_precedence():
    parsed = parse_body(_form({
        "access_token": "synthetic-primary",
        "hstm_access_token": "synthetic-alias",
        "hstm_refresh_token": "synthetic-refresh",
        "hstm_client_id": "synthetic-client",
        "hstm_client_secret": "synthetic-secret",
        "hstm_access_token_url": "https://example.invalid/token",
        "hstm_send_result_url": "https://example.invalid/submit",
        "hstm_source_endpoint": "synthetic-source",
        "token_expired": "false",
        "hstm_token_expired": "true",
    }))
    assert parsed["access_token"] == "synthetic-primary"
    assert parsed["refresh_token"] == "synthetic-refresh"
    assert parsed["client_id"] == "synthetic-client"
    assert parsed["client_secret"] == "synthetic-secret"
    assert parsed["access_token_url"] == "https://example.invalid/token"
    assert parsed["send_result_url"] == "https://example.invalid/submit"
    assert parsed["source_endpoint"] == "synthetic-source"
    assert parsed["token_expired"] is False


@pytest.mark.parametrize("wire, expected", [("TRUE", True), ("yes", True), ("1", True), ("y", True), (" true ", False), ("0", False)])
def test_expiry_flag_preserves_reference_case_and_whitespace_rules(wire, expected):
    assert parse_body(_form({"hstm_token_expired": wire}))["token_expired"] is expected


def test_duplicate_form_fields_keep_first_value():
    parsed = parse_body(_form([("condition", '{"target":"child"}'), ("condition", '{"target":"adult"}')]))
    assert parsed["condition"] == {"target": "child"}


def test_reference_url_decoding_order_is_preserved():
    # Reference unquotes before parse_qs; an encoded literal plus becomes a space.
    parsed = parse_body(_form({"client_id": "synthetic+client"}))
    assert parsed["client_id"] == "synthetic client"


def test_invalid_binary_base64_is_not_silently_replaced_with_empty_data():
    with pytest.raises(binascii.Error):
        parse_body(_form({"cpr_b64_data": "x"}))

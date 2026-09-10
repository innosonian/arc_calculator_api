"""Durable projection rejects extra payloads without changing legacy parsing."""

from copy import deepcopy
import json

import pytest

from mock_journey.errors import JourneyError
from mock_journey.legacy_bridge import parse_measurement
from mock_journey.projection import ProjectionSchema, project_input, typed_identity
from mock_journey import typed
from services.http.schemas import DEFAULT_CONDITION
from services.legacy_response import DocumentSelection, finalize_legacy_response
from tests._synth import comp_session, condition_json, multipart_event


def measurement_fixture():
    condition = {**DEFAULT_CONDITION, "guideline": "ARC2025"}
    definition = {"condition": condition, "calculation_profile": {},
                  "goal": {"kind": "cycles", "required": 3}, "catalog_version": "mock-catalog-v1",
                  "profile_version": "tester-v1", "adapter_version": "test-adapter-v1", "projection_version": "test-projection-v1"}
    body = {"cpr_b64_data": b"actual-collected-measurement", "aed_b64_data": b"",
            "condition": deepcopy(condition), "vp_event_list": []}
    schema = ProjectionSchema("test-projection-v1", {"CompressionDepth": {"%_Good": "scalar"}})
    return body, definition, schema


def test_identity_distinguishes_types_presence_order_and_negative_zero():
    values = [None, False, 0, 0.0, -0.0, "0", [], {}, {"x": None}, {"x": 0}, [1, 2], [2, 1]]
    assert len({typed.digest(value) for value in values}) == len(values)
    assert typed.digest({"a": 1, "b": 2}) == typed.digest({"b": 2, "a": 1})


@pytest.mark.parametrize("value", [float("nan"), float("inf"), b"binary-in-json", {1: "invalid"}, {"x": object()}, "\ud800"])
def test_non_json_values_are_never_coerced_to_strings(value):
    with pytest.raises((ValueError, UnicodeError)):
        typed.canonical_bytes(value)


def test_existing_multipart_parser_is_used_without_calculation():
    raw = comp_session(60)
    parsed = parse_measurement(multipart_event({
        "rawHexBPfile": raw, "condition": condition_json(training_type="compression_only", guideline="ARC2025"),
        "Usage": json.dumps({"Email": "한글+example@example.invalid", "validNum": 123}),
    }))
    assert parsed["cpr_b64_data"] == raw
    assert parsed["Usage"]["Email"] == "한글+example@example.invalid"
    assert parsed["condition"]["training_type"] == "compression_only"


def test_credentials_do_not_enter_any_projected_context_or_identity():
    body, definition, schema = measurement_fixture()
    body.update(access_token="private-one", client_secret="private-two")
    body["Custom"] = {"CertificateAdult": True, "client_secret": "private-three"}
    body["hstm_document"] = {"Usage": {"validNum": 12, "access_token": "private-four"}}
    definition["calculation_profile"] = {"Custom": {"CertificateAdult": True, "client_secret": "private-five"}}
    projected = project_input(body, definition, schema)
    assert "private-" not in json.dumps(projected.payload)
    first = typed_identity(projected)
    body["access_token"] = "changed"
    assert typed_identity(project_input(body, definition, schema)) == first
    assert body["Custom"]["client_secret"] == "private-three"


@pytest.mark.parametrize("field, value", [
    ("Custom", {"debug": {"client_secret": "private"}}),
    ("Usage", {"Email": {"token": "private"}}),
    ("Usage", {"Email": []}),
    ("vp_event_list", [{"event": 0, "timestamp": 1, "extra": "private"}]),
    ("hstm_document", {"ResultByCriteria": {"CompressionDepth": {"unknown": "private"}}}),
    ("hstm_document", {"DeviceInfo": {"token": "private"}}),
])
def test_unknown_nested_fields_and_scalar_containers_are_rejected(field, value):
    body, definition, schema = measurement_fixture()
    body[field] = value
    with pytest.raises(JourneyError) as raised:
        project_input(body, definition, schema)
    assert raised.value.code == "MEASUREMENT_INPUT_INVALID"
    assert "private" not in str(raised.value)


def test_top_level_response_context_does_not_merge_with_nested_document_context():
    body, definition, schema = measurement_fixture()
    body["Custom"] = {"CertificateAdult": True}
    body["Usage"] = {"Regional_Option": "usa", "Email": "top@example.invalid"}
    body["Organization"] = {"org_name": "Top organization"}
    body["hstm_document"] = {"Custom": {"CertificateAdult": False}, "Usage": {"Email": "nested@example.invalid"},
                             "Organization": {"org_name": "Nested organization"}}
    value = project_input(body, definition, schema).payload
    assert value["response_context"]["Custom"]["CertificateAdult"] is True
    assert value["document_context"]["document"]["Custom"]["CertificateAdult"] is False
    assert value["response_context"]["Organization"]["org_name"] == "Top organization"
    assert value["document_context"]["document"]["Organization"]["org_name"] == "Nested organization"


def test_truthy_nested_document_remains_selected_after_credentials_removed():
    body, definition, schema = measurement_fixture()
    body["Usage"] = {"Email": "must-not-fallback@example.invalid", "validNum": 1}
    body["hstm_document"] = {"access_token": "private"}
    projected = project_input(body, definition, schema)
    context = projected.payload["document_context"]
    assert context == {"document_source": "nested", "document": {}}
    working = {**projected.payload["calculation_input"], **projected.payload["response_context"]}
    finalize_legacy_response(working, {"cpr_score": {"total_score": {"overall": None}}},
                             document_selection=DocumentSelection(context["document_source"], context["document"]))
    assert "Usage" not in working["hstm_document"]


@pytest.mark.parametrize("as_pairs", [False, True])
def test_cycle_input_numbers_are_discarded_but_selected_container_is_preserved(as_pairs):
    body, definition, schema = measurement_fixture()
    document = {"ResultByCycle": {"CompressionDepth": {"Overall": 777, "ByCycle": [888]}}, "Usage": {"validNum": 1}}
    body["hstm_document"] = [[key, value] for key, value in document.items()] if as_pairs else document
    projected = project_input(body, definition, schema)
    selected = projected.payload["document_context"]["document"]
    assert type(selected) is (list if as_pairs else dict)
    assert dict(selected)["ResultByCycle"] == {}
    assert "777" not in json.dumps(projected.payload) and "888" not in json.dumps(projected.payload)


def test_missing_or_mismatched_schema_never_enables_projection():
    body, definition, schema = measurement_fixture()
    for wrong in (None, ProjectionSchema("wrong-version", {})):
        with pytest.raises(JourneyError) as raised:
            project_input(body, definition, wrong)
        assert raised.value.code == "CALCULATOR_CONTRACT_MISMATCH"
    body["condition"]["is_2rescuers"] = 0
    assert definition["condition"]["is_2rescuers"] is False
    with pytest.raises(JourneyError) as raised:
        project_input(body, definition, schema)
    assert raised.value.code == "PROFILE_MISMATCH"


def test_changed_bytes_and_changed_profile_have_distinct_input_identity():
    body, definition, schema = measurement_fixture()
    before = typed_identity(project_input(body, definition, schema))
    body["cpr_b64_data"] += b"more"
    assert typed_identity(project_input(body, definition, schema)) != before
    body["cpr_b64_data"] = b"actual-collected-measurement"
    body["Open_Skill"] = {"Passing_Score": 80.0}
    with_float = typed_identity(project_input(body, definition, schema))
    body["Open_Skill"] = {"Passing_Score": 80}
    assert typed_identity(project_input(body, definition, schema)) != with_float

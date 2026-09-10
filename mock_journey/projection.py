"""Versioned consumed-field projection for durable jobs, not a legacy parser."""

from copy import deepcopy
from dataclasses import dataclass
import hashlib

from mock_journey.errors import JourneyError
from mock_journey import typed


SCALAR = "scalar"
_CONDITION = {key: SCALAR for key in ("mode", "target", "training_type", "guideline", "cpr_cycle_type", "is_2rescuers")}
_VP = {key: SCALAR for key in ("event", "timestamp", "last_timestamp")}
_CUSTOM = {key: SCALAR for key in ("CertificateAdult", "CertificateChild", "CertificateInfant", "CertificateBaby", "PassThreshold", "PassThresholdChild")}
_CUSTOM["TrainCourse"] = {"Certification": SCALAR, "StopCondition": {
    key: SCALAR for key in ("finish_cycle", "finish_compression", "finish_ventilation")
}}
_USAGE = {key: SCALAR for key in ("Type", "Regional_Option", "Email", "Comment", "validNum", "hstreamId")}
_ORGANIZATION = {key: SCALAR for key in ("org_id", "org_name", "First_name", "Last_name")}
_DOCUMENT = {
    "Custom": _CUSTOM, "Open_Skill": {"Passing_Score": SCALAR}, "Usage": _USAGE,
    "Organization": _ORGANIZATION, "Dummy": {"DeviceID": SCALAR, "Hardware": SCALAR},
    "ResultSummary": {key: SCALAR for key in (
        "HstreamId", "hstreamId", "ManikinDeviceId", "OrgId", "hStreamScore", "JudgResult", "hStreamResult", "hStreamReason",
    )},
    "DeviceInfo": {}, "Institution": {}, "CalculationService": {},
    "Certification": {"Target": SCALAR},
}
_METRICS = frozenset(("CompressionDepth", "CompressionRate", "Recoil", "HandPosition", "CompressionNo",
                      "VentilationVolume", "VentilationRate", "VentilationSpeed", "ScoreOfCCF", "CompressionRelease"))
_CREDENTIALS = frozenset((
    "access_token", "refresh_token", "client_id", "client_secret", "access_token_url", "send_result_url",
    "source_endpoint", "token_expired", "hstm_access_token", "hstm_refresh_token", "hstm_client_id",
    "hstm_client_secret", "hstm_access_token_url", "hstm_send_result_url", "hstm_source_endpoint", "hstm_token_expired",
))


@dataclass(frozen=True)
class ProjectionSchema:
    version: str
    metric_fields: dict

    def __post_init__(self):
        if type(self.version) is not str or not self.version or type(self.metric_fields) is not dict:
            raise ValueError("A verified projection schema is required.")
        if not set(self.metric_fields) <= _METRICS:
            raise ValueError("Unsupported legacy metric name.")


@dataclass(frozen=True)
class ProjectedInput:
    cpr_bytes: bytes
    aed_bytes: bytes
    payload: dict


@dataclass(frozen=True)
class LoadedInput:
    projected: ProjectedInput
    raw_base: str


def _project(value, schema):
    if schema == SCALAR:
        if value is not None and type(value) not in (str, int, float, bool):
            raise ValueError("A scalar field has a container value.")
        typed.canonical_bytes(value)
        return value
    # Preserve falsy legacy container values and null instead of normalizing
    # them to {}. Existing validators decide whether consumed input is valid.
    if value is None or (type(value) in (bool, int, float, str, list) and not value):
        typed.canonical_bytes(value)
        return deepcopy(value)
    if type(schema) is list and len(schema) == 1:
        if type(value) is not list:
            raise ValueError("An array field has a different type.")
        return [_project(item, schema[0]) for item in value]
    if type(schema) is not dict:
        raise ValueError("Invalid projection schema.")
    if type(value) is dict:
        items = list(value.items())
        as_pairs = False
    elif type(value) is list and all(type(pair) is list and len(pair) == 2 and type(pair[0]) is str for pair in value):
        items = value
        as_pairs = True
    else:
        raise ValueError("An object field cannot be reproduced.")
    result = []
    for key, nested in items:
        if key in _CREDENTIALS:
            continue
        if key not in schema:
            raise ValueError("Unknown projection field.")
        result.append([key, _project(nested, schema[key])])
    return result if as_pairs else dict(result)


def project_input(body, definition, schema):
    if type(definition) is not dict or not isinstance(schema, ProjectionSchema) or schema.version != definition.get("projection_version"):
        raise JourneyError("CALCULATOR_CONTRACT_MISMATCH")
    try:
        if type(body) is not dict or type(definition) is not dict:
            raise ValueError("Invalid projection input.")
        document_schema = {**_DOCUMENT, "ResultByCriteria": schema.metric_fields,
                           "ResultByCycle": {key: {"Overall": SCALAR, "ByCycle": [SCALAR]} for key in _METRICS}}
        allowed = set(document_schema) | {"cpr_b64_data", "aed_b64_data", "condition", "vp_event_list", "hstm_document"} | _CREDENTIALS
        if set(body) - allowed:
            raise ValueError("Unknown top-level field.")
        cpr, aed = body.get("cpr_b64_data", b""), body.get("aed_b64_data", b"")
        if type(cpr) is not bytes or not cpr or type(aed) is not bytes:
            raise ValueError("Invalid measurement bytes.")
        condition = _project(body.get("condition"), _CONDITION)
        if typed.canonical_bytes(condition) != typed.canonical_bytes(definition["condition"]):
            raise JourneyError("PROFILE_MISMATCH")
        vp = body.get("vp_event_list", [])
        if type(vp) is not list or any(type(item) is not dict for item in vp):
            raise ValueError("Invalid VP sequence.")
        calculation = {"condition": condition, "vp_event_list": [_project(item, _VP) for item in vp]}
        response = {key: _project(body[key], document_schema[key]) for key in ("Custom", "Open_Skill", "Usage", "Organization") if key in body}
        if body.get("hstm_document"):
            source = "nested"
            document = _project(body["hstm_document"], document_schema)
        else:
            selected = {key: body[key] for key in document_schema if body.get(key) is not None}
            source = "top_level" if selected else "none"
            document = _project(selected, document_schema) if selected else None
        # ResultByCycle is regenerated from the complete calculator result. Do
        # not retain caller values as an alternative score source.
        if type(document) is dict and "ResultByCycle" in document:
            document["ResultByCycle"] = {} if document["ResultByCycle"] is not None else None
        elif type(document) is list:
            document = [[key, ({} if value is not None else None) if key == "ResultByCycle" else value]
                        for key, value in document]
        definition_schema = {
            "condition": _CONDITION,
            "calculation_profile": {key: document_schema[key] for key in ("Custom", "Open_Skill", "Usage", "Organization")},
            "goal": {"kind": SCALAR, "required": SCALAR},
            **{key: SCALAR for key in ("catalog_version", "profile_version", "adapter_version", "projection_version")},
        }
        safe_definition = _project(definition, definition_schema)
        payload = {"calculation_input": calculation, "response_context": response,
                   "document_context": {"document_source": source, "document": document},
                   "definition": safe_definition}
        typed.canonical_bytes(payload)
        return ProjectedInput(cpr, aed, payload)
    except JourneyError:
        raise
    except (KeyError, ValueError, TypeError, UnicodeError, RecursionError):
        raise JourneyError("MEASUREMENT_INPUT_INVALID") from None


def typed_identity(projected):
    if not isinstance(projected, ProjectedInput) or type(projected.cpr_bytes) is not bytes or type(projected.aed_bytes) is not bytes:
        raise ValueError("Invalid projected input.")
    return typed.digest({
        "payload": projected.payload,
        "cpr": {"sha256": hashlib.sha256(projected.cpr_bytes).hexdigest(), "size": len(projected.cpr_bytes)},
        "aed": {"sha256": hashlib.sha256(projected.aed_bytes).hexdigest(), "size": len(projected.aed_bytes)},
    })

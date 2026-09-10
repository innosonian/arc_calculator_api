"""Shared legacy response assembly; no calculation or external submission.

The HTTP handler retains its parser and scoring path. Journey workers may use
this boundary only after obtaining a validated, complete calculator result.
"""

from copy import deepcopy
from dataclasses import dataclass
from typing import Literal

from services.legacy_document import (
    _apply_calculated_fields,
    _build_certification,
    _build_hstm_document,
)


@dataclass(frozen=True)
class DocumentSelection:
    """A document branch selected before a durable projection removes fields.

    ``None`` at the finalizer call means ordinary legacy selection. An explicit
    selection instead carries the already selected nested or top-level document,
    including an empty projection; it must not be selected by truthiness again.
    ``none`` records that the original input had no selected document.
    """

    source: Literal["none", "nested", "top_level"]
    document: dict | list | None = None

    def __post_init__(self) -> None:
        if self.source not in ("none", "nested", "top_level"):
            raise ValueError("Unsupported document selection.")
        if self.source == "none" and self.document is not None:
            raise ValueError("An absent document cannot have a document value.")
        if self.source != "none" and self.document is None:
            raise ValueError("A selected document requires a document value.")


def finalize_legacy_response(
    body: dict,
    core_result: dict,
    *,
    document_selection: DocumentSelection | None = None,
) -> dict:
    """Preserve legacy assembly while protecting the supplied core result.

    The generated document is assigned to ``body`` as in the existing handler;
    it is neither returned as part of the result nor submitted. Callers using a
    stored manifest supply a working body and an explicit document selection.
    """
    condition = body.get("condition") or {}
    result = _convert_result_to_legacy(deepcopy(core_result), condition)
    # Submission belongs to the app response boundary, never to the immutable
    # calculated snapshot (including any stale fields in a supplied result).
    result.pop("submit_hstm", None)
    result.pop("submit_arc", None)
    result["certification"] = _build_certification(
        result, condition, body.get("Custom"), body.get("Open_Skill"), None
    )
    if document_selection is None:
        if body.get("hstm_document"):
            body["hstm_document"] = _apply_calculated_fields(body["hstm_document"], result, condition)
        else:
            body["hstm_document"] = _build_hstm_document(body, result, condition)
    elif document_selection.source == "none":
        body["hstm_document"] = None
    else:
        # Both branches carry the already selected document. An empty projected
        # document still follows that branch, not the body's fallback fields.
        body["hstm_document"] = _apply_calculated_fields(
            deepcopy(document_selection.document), result, condition
        )
    return result


def _convert_result_to_legacy(result: dict, condition: dict) -> dict:
    training_type = (condition or {}).get("training_type")
    target = (condition or {}).get("target")

    if training_type == "ventilation_only" and target == "infant":
        legacy_template = "sample_baby_ventilation.json"
    elif training_type == "ventilation_only":
        legacy_template = "sample_ventilation_only.json"
    elif training_type == "compression_only":
        legacy_template = "sample_compression_only.json"
    else:
        legacy_template = "sample_cpr.json"

    _ = legacy_template

    legacy_result = dict(result)

    total_score = (legacy_result.get("cpr_score") or {}).get("total_score")
    if isinstance(total_score, dict):
        # TODO 변환시 확인 필요: cpr_score.total_score.score_rescue_vent 값(0 vs None)
        total_score["score_rescue_vent"] = 0

    metrics = legacy_result.get("metrics")
    if isinstance(metrics, dict) and training_type == "cpr" and target != "infant":
        metrics["VentilationSpeed"] = None

    return legacy_result

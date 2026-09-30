"""Pure app response composition while the ARC submission contract is pending."""

import math


def disabled_submission_status() -> dict:
    """Return a fresh, trusted status; no configuration can enable submission."""
    return {"status": "disabled", "ok": False, "error": "arc_contract_pending"}


def compose_calculation_response(calculation: dict) -> dict:
    """Keep calculated fields independent of the app's submission status.

    Callers must authenticate and verify stored-result integrity before this
    boundary. Neither an old result nor client data can supply a success state.
    """
    if type(calculation) is not dict:
        raise ValueError("Invalid calculation result.")
    try:
        result = _copy_json(calculation)
    except (ValueError, TypeError, RecursionError, OverflowError):
        raise ValueError("Invalid calculation result.") from None
    result.pop("submit_hstm", None)
    result["submit_arc"] = disabled_submission_status()
    return result


def _copy_json(value):
    """Copy JSON values without invoking custom objects or coercing key types."""
    kind = type(value)
    if value is None or kind in (str, bool, int):
        return value
    if kind is float:
        if not math.isfinite(value):
            raise ValueError("Invalid calculation result.")
        return value
    if kind is list:
        return [_copy_json(item) for item in value]
    if kind is dict:
        if any(type(key) is not str for key in value):
            raise ValueError("Invalid calculation result.")
        return {key: _copy_json(item) for key, item in value.items()}
    raise ValueError("Invalid calculation result.")

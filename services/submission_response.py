"""Pure app response composition while the ARC submission contract is pending."""

import json
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


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Invalid calculation snapshot.")
        result[key] = value
    return result


def _reject_constant(_value):
    raise ValueError("Invalid calculation snapshot.")


def compose_calculation_snapshot(snapshot: bytes) -> bytes:
    """Overlay only the HTTP response; never rewrite the committed snapshot."""
    if type(snapshot) is not bytes:
        raise ValueError("Invalid calculation snapshot.")
    try:
        calculation = json.loads(
            snapshot.decode("utf-8"), object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
        result = compose_calculation_response(calculation)
        # allow_nan also rejects a valid JSON exponent that overflowed a float.
        return json.dumps(result, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise ValueError("Invalid calculation snapshot.") from None

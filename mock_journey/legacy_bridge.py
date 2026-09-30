"""The only Mock import bridge to existing wire parsers and validators.

The parsers live in the neutral ``services.http.legacy_request`` module, which
``lambda_handler`` re-exports as the same objects. Importing them from there
keeps the API/Worker/Relay assembly from loading the Lambda entry module and
``main`` (and, through them, boto3-backed legacy uploads and Sentry). Tests replace ``ClientError`` and the
validator names on this module, so they stay bound here.
"""

from services.http.legacy_request import (
    ClientError,
    _get_content_type,
    _parse_multipart_body,
    _validate_legacy_document,
    _validate_request,
)
from services.http.service import parse_body
from mock_journey.errors import JourneyError


class MeasurementInputError(JourneyError):
    """One fixed MEASUREMENT_INPUT_INVALID error; no parser message is kept or echoed."""

    def __init__(self):
        super().__init__("MEASUREMENT_INPUT_INVALID")


def parse_measurement(event):
    """Call only after API authentication. Never store/log the event or form."""
    try:
        headers = event.get("headers") or {}
        content_type = _get_content_type(headers)
        if content_type and "multipart/form-data" in content_type.lower():
            body = _parse_multipart_body(event.get("body") or "", headers, event.get("isBase64Encoded", False))
        else:
            body = parse_body(event.get("body") or "")
        _validate_request(body)
        _validate_legacy_document(body)
        # This existing parser guard used to fail synchronously as HTTP 400.
        # Check the same rule before durable acceptance, without decoding the
        # measurements twice or tightening the legacy trailing-byte policy.
        from data_handlers.data_parser import DataParser
        if not DataParser()._validate_data_length(body["cpr_b64_data"]):
            raise MeasurementInputError()
        return body
    except ClientError:
        raise MeasurementInputError() from None
    except (ValueError, TypeError, UnicodeError, RecursionError, AttributeError):
        raise MeasurementInputError() from None

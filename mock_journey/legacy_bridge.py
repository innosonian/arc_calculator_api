"""The only Mock import bridge to existing wire parsers and validators."""

from lambda_handler import (
    ClientError,
    _MSG_CPR_FILE_REQUIRED,
    _MSG_SANITIZED_CLIENT_ERROR,
    _MSG_UNSUPPORTED_GUIDELINE,
    _MSG_UNSUPPORTED_TARGET,
    _MSG_UNSUPPORTED_TRAINING_TYPE,
    _get_content_type,
    _parse_multipart_body,
    _validate_legacy_document,
    _validate_request,
)
from services.http.service import parse_body
from mock_journey.errors import JourneyError


_SAFE_MESSAGES = frozenset((
    _MSG_CPR_FILE_REQUIRED, _MSG_SANITIZED_CLIENT_ERROR,
    _MSG_UNSUPPORTED_GUIDELINE, _MSG_UNSUPPORTED_TARGET,
    _MSG_UNSUPPORTED_TRAINING_TYPE, "Content-Type must be multipart/form-data", "Empty body",
))


class MeasurementInputError(JourneyError):
    """One internal error with the existing route-specific safe representations."""

    def __init__(self, legacy_message=_MSG_SANITIZED_CLIENT_ERROR):
        super().__init__("MEASUREMENT_INPUT_INVALID")
        self.legacy_message = (legacy_message if type(legacy_message) is str
                               and legacy_message in _SAFE_MESSAGES
                               else _MSG_SANITIZED_CLIENT_ERROR)


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
    except ClientError as error:
        raise MeasurementInputError(str(error)) from None
    except (ValueError, TypeError, UnicodeError, RecursionError, AttributeError):
        raise MeasurementInputError() from None

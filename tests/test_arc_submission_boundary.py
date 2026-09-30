"""Adversarial tests for the disabled submission boundary, not an ARC contract.

compose_calculation_response is the trusted lambda_handler result overlay. The
v1 GET snapshot overlay (compose_calculation_snapshot) was removed with
/mock/v1 (D103); /api/v2 returns the stored final as it is.
"""

from copy import deepcopy
import json
import math
from pathlib import Path
import subprocess
import sys

import pytest

import submit_arc
import services.submission_response as submission_response
from services.submission_response import (
    compose_calculation_response,
    disabled_submission_status,
)


DISABLED = {"status": "disabled", "ok": False, "error": "arc_contract_pending"}
ROOT = Path(__file__).resolve().parents[1]


def _calculation():
    return {
        "cpr_score": {"total_score": {"overall": None, "compression": 90}},
        "metrics": {"legacy_values": [None, False, True, 0, 1, 0.0, -0.0, 1.0, "1"]},
        "action_count": {"comp": 90, "vent": 6},
        "training_stats": {"cycle_count": 3, "elapsed_seconds": 12.5},
        "guide_prompts": ["기존 코칭", "existing coaching"],
        "chart_dataset_url": None,
        "preserved": {"submit_arc": "nested calculation field"},
    }


def _assert_same_json_values_and_types(left, right):
    assert type(left) is type(right)
    if type(left) is dict:
        assert left.keys() == right.keys()
        for key in left:
            _assert_same_json_values_and_types(left[key], right[key])
    elif type(left) is list:
        assert len(left) == len(right)
        for original, actual in zip(left, right):
            _assert_same_json_values_and_types(original, actual)
    else:
        assert left == right
        if type(left) is float and left == 0:
            assert math.copysign(1, left) == math.copysign(1, right)


def test_stale_success_is_not_submission_authority_and_values_keep_their_types():
    original = _calculation()
    original["submit_hstm"] = {"ok": True, "receipt": "untrusted-legacy-receipt"}
    original["submit_arc"] = {"status": "succeeded", "ok": True, "error": None}
    before = deepcopy(original)
    actual = compose_calculation_response(original)
    assert original == before
    assert "submit_hstm" not in actual
    assert actual.pop("submit_arc") == DISABLED
    expected = {key: value for key, value in original.items()
                if key not in ("submit_hstm", "submit_arc")}
    _assert_same_json_values_and_types(expected, actual)


def test_composition_detaches_nested_results_and_does_not_mutate_input():
    original = _calculation()
    before = deepcopy(original)
    first = compose_calculation_response(original)
    first["metrics"]["legacy_values"].append("changed")
    first["guide_prompts"][0] = "changed"
    first["submit_arc"]["ok"] = True
    _assert_same_json_values_and_types(before, original)
    second = compose_calculation_response(original)
    assert second["submit_arc"] == DISABLED
    second.pop("submit_arc")
    _assert_same_json_values_and_types(before, second)


@pytest.mark.parametrize("factory", [
    disabled_submission_status,
    submit_arc.submit_arc,
    lambda: submit_arc.run({"submit_arc": {"ok": True}}, None),
])
def test_status_results_are_fresh_and_cannot_poison_future_calls(factory):
    first = factory()
    assert first == DISABLED
    first["status"], first["ok"] = "succeeded", True
    assert factory() == DISABLED


def test_v1_snapshot_overlay_is_removed():
    assert not hasattr(submission_response, "compose_calculation_snapshot")
    assert {name for name in vars(submission_response) if name.startswith("compose_")} == {
        "compose_calculation_response"}


@pytest.mark.parametrize("value", [
    None, [], "{}", 1, True,
    {"n": float("nan")}, {"n": float("inf")}, {"nested": [float("-inf")]},
    {"nested": {"not_json": {1, 2}}}, {"nested": b"bytes"},
    {1: "coerced-key"}, {"nested": {None: "coerced-key"}},
])
def test_non_json_calculation_cannot_be_silently_coerced(value):
    with pytest.raises(ValueError) as raised:
        compose_calculation_response(value)
    assert str(raised.value) == "Invalid calculation result."


def test_mapping_path_keeps_valid_scalar_types_through_json():
    original = _calculation()
    original["large_integer"] = 2**80 + 1
    response = compose_calculation_response(original)
    decoded = json.loads(json.dumps(response, allow_nan=False))
    _assert_same_json_values_and_types(response, decoded)
    assert type(response["large_integer"]) is int and response["large_integer"] == 2**80 + 1


def test_response_reads_do_not_invoke_the_submission_module(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("A read-only response composition invoked submission.")

    monkeypatch.setattr(submit_arc, "submit_arc", forbidden)
    monkeypatch.setattr(submit_arc, "run", forbidden)
    assert compose_calculation_response(_calculation())["submit_arc"] == DISABLED


def test_import_direct_lambda_and_read_paths_have_no_network_sdk_or_secret_output():
    # Fresh-process import inspection avoids pytest's previously imported SDKs.
    # No endpoint is contacted: forbidden imports/socket operations stop first.
    code = r'''
import builtins
import json
import os
import sys

secret = "P1_SECRET_SENTINEL_do_not_log"
os.environ.update({
    "ARC_SUBMIT_ENABLED": "true", "ARC_SUBMISSION_MODE": "external",
    "ARC_SUBMIT_LAMBDA_NAME": secret,
    "ARC_SEND_RESULT_URL": "https://invalid.example/" + secret,
    "ARC_ACCESS_TOKEN": secret, "ARC_CLIENT_SECRET": secret,
    "HSTM_SUBMIT_LAMBDA_NAME": secret, "AWS_PROFILE": secret,
    "SENTRY_DSN": secret, "HTTP_PROXY": "http://invalid.example/" + secret,
})

class BoundaryViolation(BaseException):
    pass

original_import = builtins.__import__
forbidden = {"boto3", "botocore", "requests", "httpx", "aiohttp", "urllib3", "sentry_sdk"}
def guarded_import(name, *args, **kwargs):
    if name.split(".")[0] in forbidden:
        raise BoundaryViolation("Disabled submission imported a network SDK.")
    return original_import(name, *args, **kwargs)
def audit(event, args):
    if event in {"socket.connect", "socket.bind", "socket.sendto", "socket.getaddrinfo"}:
        raise BoundaryViolation("Disabled submission attempted network access.")
builtins.__import__ = guarded_import
sys.addaudithook(audit)

import submit_arc
from services.submission_response import compose_calculation_response
expected = {"status": "disabled", "ok": False, "error": "arc_contract_pending"}
assert submit_arc.submit_arc() == expected
event = {
    "access_token": secret, "refresh_token": secret, "client_secret": secret,
    "send_result_url": "https://invalid.example/" + secret,
    "body": json.dumps({"secret": secret}),
    "submit_arc": {"status": "succeeded", "ok": True},
}
assert submit_arc.run(event, None) == expected
assert compose_calculation_response({"value": None})["submit_arc"] == expected
try:
    compose_calculation_response({secret: float("nan")})
except ValueError as error:
    assert str(error) == "Invalid calculation result."
else:
    raise AssertionError("A non-JSON calculation was accepted.")
assert not forbidden.intersection(sys.modules)
'''
    completed = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT,
        capture_output=True, text=True, timeout=15, check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == ""
    assert completed.stderr == ""

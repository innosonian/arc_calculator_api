"""Adversarial tests for the disabled submission boundary, not an ARC contract."""

from copy import deepcopy
import json
import math
from pathlib import Path
import subprocess
import sys

import pytest

import submit_arc
from services.submission_response import (
    compose_calculation_response,
    compose_calculation_snapshot,
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


@pytest.mark.parametrize("wire", ["mapping", "snapshot"])
def test_stale_success_is_not_submission_authority_and_values_keep_their_types(wire):
    original = _calculation()
    original["submit_hstm"] = {"ok": True, "receipt": "untrusted-legacy-receipt"}
    original["submit_arc"] = {"status": "succeeded", "ok": True, "error": None}
    before = deepcopy(original)
    if wire == "mapping":
        actual = compose_calculation_response(original)
    else:
        encoded = json.dumps(original, ensure_ascii=False).encode("utf-8")
        output = compose_calculation_snapshot(encoded)
        assert type(output) is bytes
        actual = json.loads(output)
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


@pytest.mark.parametrize("snapshot", [
    b"", b"{", b"{} trailing", b"\xff", b'{"text":"\xff"}',
    b"null", b"true", b"123", b'"object-looking string"', b"[]",
    b'{"metric":1,"metric":2}',
    b'{"nested":{"metric":1,"metric":2}}',
    b'{"submit_arc":{"ok":true},"submit_arc":{"ok":false}}',
    b'{"n":NaN}', b'{"n":Infinity}', b'{"n":-Infinity}',
    b'{"n":1e400}', b'{"nested":[-1e400]}',
    "{}", bytearray(b"{}"), memoryview(b"{}"), None, {},
])
def test_malformed_or_ambiguous_snapshot_is_rejected_without_details(snapshot):
    with pytest.raises(ValueError) as raised:
        compose_calculation_snapshot(snapshot)
    assert str(raised.value) == "Invalid calculation snapshot."


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


def test_snapshot_and_mapping_paths_agree_without_normalizing_valid_scalar_types():
    original = _calculation()
    original["large_integer"] = 2**80 + 1
    response = compose_calculation_response(original)
    decoded = json.loads(compose_calculation_snapshot(json.dumps(original).encode()))
    _assert_same_json_values_and_types(response, decoded)


def test_response_reads_do_not_invoke_the_submission_module(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("A read-only response composition invoked submission.")

    monkeypatch.setattr(submit_arc, "submit_arc", forbidden)
    monkeypatch.setattr(submit_arc, "run", forbidden)
    assert compose_calculation_response(_calculation())["submit_arc"] == DISABLED
    encoded = compose_calculation_snapshot(json.dumps(_calculation()).encode())
    assert json.loads(encoded)["submit_arc"] == DISABLED


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
from services.submission_response import compose_calculation_response, compose_calculation_snapshot
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
assert json.loads(compose_calculation_snapshot(b'{"value":null}'))["submit_arc"] == expected
try:
    compose_calculation_snapshot(('{"' + secret + '":').encode())
except ValueError as error:
    assert str(error) == "Invalid calculation snapshot."
else:
    raise AssertionError("Malformed snapshot was accepted.")
assert not forbidden.intersection(sys.modules)
'''
    completed = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT,
        capture_output=True, text=True, timeout=15, check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == ""
    assert completed.stderr == ""

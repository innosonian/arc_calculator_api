"""A role that validates alone can still strand every accepted calculation."""

from copy import deepcopy
from dataclasses import asdict
import json

import pytest

from mock_journey.course_settings import fixture_course_settings
from scripts.validate_aws_dev_bundle import BundleError, main, validate_bundle
from tests.test_aws_runtime import configuration


def documents():
    result = {role: configuration(role) for role in ("api", "worker", "relay")}
    for role in ("api", "worker"):
        result[role]["course"] = {"mode": "course_v2_dummy", "catalog_version": "arc-dummy-dev-v1",
                                  "settings": asdict(fixture_course_settings())}
    return result


def check(configs, **timing):
    return validate_bundle({role: json.dumps(value) for role, value in configs.items()},
        **{"api_timeout": 30, "worker_timeout": 60, "relay_timeout": 30,
           "queue_visibility": 360, "batch_window": 0, **timing})


def test_valid_bundle_does_not_claim_aws_verification():
    result = check(documents())
    assert result["status"] == "dummy_dev_bundle_valid"
    assert result["aws_resources_verified"] is False
    assert result["secrets_verified"] is False
    assert result["deployment_performed"] is False


@pytest.mark.parametrize("role,path,value,code", [
    ("worker", ("environment",), "other-dev", "ROLE_SCOPE_MISMATCH"),
    ("relay", ("state", "table_name"), "other-table", "STATE_TABLE_MISMATCH"),
    ("worker", ("storage", "bucket"), "other-private-bucket", "STORAGE_MISMATCH"),
    ("worker", ("storage", "input_bytes"), 999999, "STORAGE_MISMATCH"),
    ("worker", ("execution", "retained_adapter_versions"), [], "EXECUTION_VERSION_MISMATCH"),
    ("worker", ("course", "settings", "max_course_items"), 32, "DUMMY_COURSE_CONFIGURATION_MISMATCH"),
])
def test_individually_valid_roles_must_use_same_state_and_contract(role, path, value, code):
    configs = documents()
    target = configs[role]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(BundleError, match=code):
        check(configs)


@pytest.mark.parametrize("role", ["api", "worker"])
def test_legacy_role_does_not_silently_join_course_release(role):
    configs = documents()
    configs[role].pop("course")
    with pytest.raises(BundleError, match="DUMMY_COURSE_CONFIGURATION_MISMATCH"):
        check(configs)


@pytest.mark.parametrize("timing,code", [
    ({"worker_timeout": True}, "LAMBDA_TIMEOUT_INVALID"),
    ({"relay_timeout": 901}, "LAMBDA_TIMEOUT_INVALID"),
    ({"queue_visibility": 359}, "QUEUE_VISIBILITY_TOO_SHORT"),
    ({"batch_window": 1}, "QUEUE_VISIBILITY_TOO_SHORT"),
    ({"batch_window": -1}, "QUEUE_TIMING_INVALID"),
    ({"queue_visibility": 43201}, "QUEUE_TIMING_INVALID"),
])
def test_queue_budget_checks_real_values_instead_of_inventing_them(timing, code):
    with pytest.raises(BundleError, match=code):
        check(documents(), **timing)


def test_invocation_cannot_be_shorter_than_reserved_work_and_log_time():
    configs = documents()
    configs["worker"]["worker"]["processing_reserve_ms"] = 1000
    with pytest.raises(BundleError, match="INVOCATION_BUDGET_TOO_SHORT"):
        check(configs, worker_timeout=1)


def test_relay_needs_time_to_acquire_and_process_at_least_one_work_item():
    with pytest.raises(BundleError, match="INVOCATION_BUDGET_TOO_SHORT"):
        check(documents(), relay_timeout=1)


def test_cli_sanitizes_invalid_configuration_contents(tmp_path, capsys):
    paths = []
    for role, value in documents().items():
        path = tmp_path / f"{role}.json"
        value = deepcopy(value)
        value["PRIVATE_MARKER"] = "PRIVATE_MARKER"
        path.write_text(json.dumps(value))
        paths.extend([f"--{role}-config", str(path), f"--{role}-timeout", "30"])
    assert main(paths + ["--queue-visibility", "180", "--batch-window", "0"]) == 2
    output = capsys.readouterr().out
    assert "PRIVATE_MARKER" not in output
    assert json.loads(output)["code"] == "ROLE_CONFIGURATION_INVALID"

"""Offline initial item preparation; all resource values are synthetic."""

from copy import deepcopy
import json
import os
import stat

import pytest

from mock_journey.relay_init import initialization_request, main
from mock_journey.state import _decode
from tests.test_aws_runtime import configuration


def test_generated_request_is_create_only_and_contains_no_due_or_ttl_fields():
    config = configuration("relay")
    request = initialization_request(json.dumps(config))
    assert set(request) == {"TableName", "Item", "ConditionExpression"}
    assert request["TableName"] == config["state"]["table_name"]
    assert request["ConditionExpression"] == "attribute_not_exists(PK)"
    row = _decode(request["Item"])
    assert row["PK"].startswith("RELAY_SCAN#") and row["SK"] == "PROGRESS#v1"
    assert row["schema"] == 1 and row["next_kind"] == "OUTBOX"
    assert row["owner"] is None and row["lease_until"] == row["revision"] == row["fence"] == 0
    assert row["scans"] == {kind: {"cutoff": None, "cursor": None} for kind in ("OUTBOX", "JOB")}
    assert not set(row) & {"GSI1PK", "GSI1SK", "next_due_at", "ttl", "expires_at"}


def test_init_prepares_private_file_without_aws_and_refuses_overwrite(tmp_path, capsys):
    config_path, output = tmp_path / "private.json", tmp_path / "request.json"
    config_path.write_text(json.dumps(configuration("relay")))
    args = ["--config", str(config_path), "--output", str(output)]
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out) == {
        "status": "initialization_request_prepared", "aws_access_checked": False, "aws_write_performed": False}
    first = output.read_bytes()
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert json.loads(first) == initialization_request(config_path.read_text())
    assert main(args) == 2
    assert output.read_bytes() == first and "PRIVATE" not in capsys.readouterr().out


def test_init_refuses_existing_symlink_without_touching_target(tmp_path, capsys):
    config_path, output, target = tmp_path / "private.json", tmp_path / "request.json", tmp_path / "owned.json"
    config_path.write_text(json.dumps(configuration("relay")))
    target.write_bytes(b"PRIVATE-ORIGINAL")
    output.symlink_to(target)
    assert main(["--config", str(config_path), "--output", str(output)]) == 2
    assert target.read_bytes() == b"PRIVATE-ORIGINAL" and output.is_symlink()
    assert "PRIVATE" not in capsys.readouterr().out


@pytest.mark.parametrize("failure", ["missing", "malformed", "wrong_role", "invalid_budget", "io"])
def test_init_error_is_fixed_and_does_not_create_output(tmp_path, capsys, monkeypatch, failure):
    config_path, output = tmp_path / "PRIVATE-MARKER.json", tmp_path / "PRIVATE-OUT.json"
    config = configuration("api" if failure == "wrong_role" else "relay")
    if failure == "invalid_budget":
        config["relay"]["lease_seconds"] = 1
    if failure != "missing":
        config_path.write_text('{"PRIVATE-MARKER":NaN}' if failure == "malformed" else json.dumps(config))
    if failure == "io":
        monkeypatch.setattr(os, "open", lambda *args: (_ for _ in ()).throw(OSError("PRIVATE-MARKER")))
    assert main(["--config", str(config_path), "--output", str(output)]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result == {"status": "initialization_request_not_prepared", "aws_access_checked": False,
                      "aws_write_performed": False}
    assert not output.exists()


@pytest.mark.parametrize("changed", ["partition", "account_id", "region", "environment", "table", "queue"])
def test_initial_binding_changes_for_every_resource_scope_component(changed):
    original = configuration("relay")
    other = deepcopy(original)
    if changed == "partition":
        other.update(partition="aws-cn", region="cn-north-1")
        other["relay"]["queue_url"] = "https://sqs.cn-north-1.amazonaws.com.cn/123456789012/synthetic-jobs"
    elif changed == "region":
        other["region"] = "us-west-2"
        other["relay"]["queue_url"] = "https://sqs.us-west-2.amazonaws.com/123456789012/synthetic-jobs"
    elif changed == "account_id":
        other["account_id"] = "111111111111"
        other["relay"]["queue_url"] = "https://sqs.us-east-1.amazonaws.com/111111111111/synthetic-jobs"
    elif changed == "environment":
        other[changed] = "synthetic-other"
    elif changed == "table":
        other["state"]["table_name"] = "synthetic-other"
    else:
        other["relay"]["queue_url"] += "-other"
    before = _decode(initialization_request(json.dumps(original))["Item"])
    after = _decode(initialization_request(json.dumps(other))["Item"])
    assert before["binding_sha256"] != after["binding_sha256"]
    assert (before["PK"] != after["PK"]) is (changed == "environment")

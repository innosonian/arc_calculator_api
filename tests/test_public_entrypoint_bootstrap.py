"""Cold imports select bundled dependencies before calculator imports them."""

import os
from pathlib import Path
import subprocess
import sys

import pytest

from scripts.build_mock_artifact import _source_files


@pytest.mark.parametrize("entrypoint", ["lambda_handler", "mock_journey.handler"])
def test_cold_public_entrypoint_prefers_bundle_to_ambient_sdk(tmp_path, entrypoint):
    # Actual repository source selection, with two intentionally different SDK
    # interface markers. These markers do not claim real SDK functionality.
    source = Path(__file__).resolve().parents[1]
    artifact = tmp_path / "artifact"
    for name, data in _source_files(source).items():
        selected = artifact / name
        selected.parent.mkdir(parents=True, exist_ok=True)
        selected.write_bytes(data)
    ambient = tmp_path / "ambient-runtime"
    for root, identity in ((artifact / "packages", "bundled"), (ambient, "ambient")):
        module = root / "boto3" / "__init__.py"
        module.parent.mkdir(parents=True)
        module.write_text(
            f"identity = {identity!r}\n"
            "def client(*args, **kwargs):\n"
            "    raise AssertionError('SDK client construction is forbidden')\n"
        )
    program = """
import importlib, pathlib, socket, sys
artifact, ambient, entrypoint = sys.argv[1:]
sys.path[:0] = [artifact, ambient]
def forbidden(*args, **kwargs):
    raise AssertionError('Network access is forbidden')
socket.socket = forbidden
socket.create_connection = forbidden
socket.getaddrinfo = forbidden
module = importlib.import_module(entrypoint)
import boto3
assert boto3.identity == 'bundled', 'Ambient SDK was imported before bootstrap'
assert pathlib.Path(boto3.__file__).is_relative_to(pathlib.Path(artifact) / 'packages')
"""
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("AWS_", "BOTO_", "ARC_", "SENTRY_", "PYTHON"))}
    completed = subprocess.run(
        [sys.executable, "-I", "-S", "-c", program, str(artifact), str(ambient), entrypoint],
        env=environment, capture_output=True, text=True, timeout=20, check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == ""

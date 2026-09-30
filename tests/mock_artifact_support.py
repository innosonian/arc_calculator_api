"""Synthetic artifact source/package trees shared by artifact and preflight tests.

Moved unchanged from tests/test_mock_artifact.py so other test modules no longer
import a test module. A test module uses the ``roots`` fixture by importing it.
"""

import base64
import csv
import hashlib
import io

import pytest

from scripts import build_mock_artifact as builder


VERSIONS = {"boto3": "1.34.140", "botocore": "1.34.162", "jmespath": "1.1.0", "s3transfer": "0.10.4",
            "sentry-sdk": "2.22.0", "urllib3": "2.7.0", "certifi": "2026.7.22", "python-dateutil": "2.9.0.post0", "six": "1.17.0"}


def write(root, name, body):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body if type(body) is bytes else body.encode())


def distribution(root, name, *, extra=None, version=None, tag="py3-none-any"):
    prefix, version = builder.DEPENDENCIES[name], version or VERSIONS[name]
    metadata_dir = name.replace("-", "_") + "-" + version + ".dist-info"
    body = {
        prefix if prefix.endswith(".py") else prefix + "/__init__.py": b"# test distribution fixture\n",
        metadata_dir + "/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n".encode(),
        metadata_dir + "/WHEEL": f"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: {tag}\n".encode(),
        **(extra or {}),
    }
    record = metadata_dir + "/RECORD"
    stream = io.StringIO()
    writer = csv.writer(stream)
    for relative, data in body.items():
        write(root, relative, data)
        checksum = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("=")
        writer.writerow((relative, "sha256=" + checksum, len(data)))
    writer.writerow((record, "", ""))
    write(root, record, stream.getvalue())
    return metadata_dir


@pytest.fixture
def roots(tmp_path):
    source, packages = tmp_path / "source", tmp_path / "packages"
    source.mkdir()
    packages.mkdir()
    write(source, "requirements.txt", "boto3==1.34.140\nsentry-sdk==2.22.0\n")
    for name in builder.SOURCE_PACKAGES:
        write(source, name + "/__init__.py", "")
    for entry in builder.ENTRYPOINTS:
        write(source, entry.rsplit(".", 1)[0].replace(".", "/") + ".py", "def run(*args): return None\n")
    write(source, "main.py", "# source fixture\n")
    write(source, "services/guide_prompts.py", "_TABLE_OF_PROMPT = {'language_guideline': {'g': {'default': 'required.json'}}}\n")
    write(source, "resources/prompt_books/required.json", '{"coaching":"기존 문구"}')
    for name in builder.DEPENDENCIES:
        extra = {name + "/cacert.pem": b"PUBLIC CA BUNDLE"} if name in ("certifi", "botocore") else None
        distribution(packages, name, extra=extra)
    return source, packages, tmp_path / "output"

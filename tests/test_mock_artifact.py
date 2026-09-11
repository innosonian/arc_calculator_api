"""Offline packaging boundaries; fixture packages are not SDK implementations."""

import base64
import csv
import hashlib
import io
import json
from pathlib import Path
import zipfile

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


def reject(roots, code):
    with pytest.raises(builder.ArtifactError) as raised:
        builder.build_artifact(*roots)
    assert raised.value.code == code
    assert "PRIVATE" not in str(raised.value)


def test_identical_inputs_produce_identical_zip_and_file_provenance(roots):
    source, packages, output = roots
    first = builder.build_artifact(*roots)
    another = output.with_name("another-output")
    second = builder.build_artifact(source, packages, another)
    for name in ("mock-lambda.zip", "artifact-manifest.json"):
        assert (output / name).read_bytes() == (another / name).read_bytes()
    assert first == second
    assert "submit_arc.run" in first["entrypoints"]
    assert first["zip"]["sha256"] == hashlib.sha256((output / "mock-lambda.zip").read_bytes()).hexdigest()
    with zipfile.ZipFile(output / "mock-lambda.zip") as archive:
        assert archive.namelist() == sorted(first["files"])
        for entry in archive.infolist():
            assert entry.compress_type == zipfile.ZIP_DEFLATED
            data = archive.read(entry)
            assert first["files"][entry.filename] == {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            assert entry.date_time == (1980, 1, 1, 0, 0, 0)
        assert archive.read("resources/prompt_books/required.json").decode() == '{"coaching":"기존 문구"}'
        assert archive.read("packages/certifi/cacert.pem") == b"PUBLIC CA BUNDLE"
        assert archive.read("packages/botocore/cacert.pem") == b"PUBLIC CA BUNDLE"
        assert archive.read("submit_arc.py") == (source / "submit_arc.py").read_bytes()
    assert set(first["dependencies"]) == set(VERSIONS)
    assert "linux-lambda-execution" in first["not_verified"]


def test_archive_hashing_does_not_read_the_entire_zip_into_memory(roots, monkeypatch):
    original = Path.read_bytes
    def bounded_read(path):
        assert path.suffix != ".zip", "ZIP hashing must stream the completed artifact."
        return original(path)
    monkeypatch.setattr(Path, "read_bytes", bounded_read)
    builder.build_artifact(*roots)


@pytest.mark.parametrize("limit,code", [
    ("MAX_ZIP_BYTES", "DIRECT_UPLOAD_ARTIFACT_TOO_LARGE"),
    ("MAX_UNZIPPED_BYTES", "UNZIPPED_ARTIFACT_TOO_LARGE"),
])
def test_oversized_artifact_is_not_published(roots, monkeypatch, limit, code):
    monkeypatch.setattr(builder, limit, 1)
    reject(roots, code)
    assert not (roots[2] / "mock-lambda.zip").exists()
    assert not (roots[2] / "artifact-manifest.json").exists()


def test_repository_secrets_tests_and_unreferenced_resources_are_never_read(roots, monkeypatch):
    source, _, output = roots
    forbidden = (".env", ".env.dev", ".git/config", "tests/private.py", "integration_tests/private.py",
                 "docs/private.md", "scripts/private.py", "services/__pycache__/private.pyc",
                 "services/.env/private.py", "resources/prompt_books/unreferenced.json")
    for name in forbidden:
        write(source, name, "PRIVATE")
    original = Path.open
    targets = {source / name for name in forbidden}
    def checked_open(path, *args, **kwargs):
        assert path not in targets, "Excluded file was opened."
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", checked_open)
    result = builder.build_artifact(*roots)
    assert not targets.intersection(source / name for name in result["files"])
    with zipfile.ZipFile(output / "mock-lambda.zip") as archive:
        assert all(b"PRIVATE" not in archive.read(name) for name in archive.namelist())


def test_sdk_runtime_docs_are_kept_but_sdk_tests_are_not_read_or_packaged(roots, monkeypatch):
    _, packages, _ = roots
    distribution(packages, "boto3", extra={"boto3/docs/module.py": b"# runtime module", "boto3/tests/test_private.py": b"PRIVATE"})
    original = Path.open
    def checked_open(path, *args, **kwargs):
        assert path != packages / "boto3/tests/test_private.py"
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", checked_open)
    files = builder.build_artifact(*roots)["files"]
    assert "packages/boto3/docs/module.py" in files
    assert "packages/boto3/tests/test_private.py" not in files


@pytest.mark.parametrize("relative", ["inject.pth", "inject.egg-link", "boto3/private.key", "boto3/private.pem", ".env"])
def test_dependency_secret_or_startup_hook_is_rejected_without_opening_it(roots, relative, monkeypatch):
    _, packages, _ = roots
    write(packages, relative, "PRIVATE")
    original = Path.open
    def checked_open(path, *args, **kwargs):
        assert path != packages / relative
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", checked_open)
    reject(roots, "FORBIDDEN_DEPENDENCY_FILE")


@pytest.mark.parametrize("relative", ["boto3/core.so", "boto3/core.dylib", "boto3/core.pyd"])
def test_native_payload_requires_a_different_platform_verification_scope(roots, relative):
    write(roots[1], relative, b"not a portable module")
    reject(roots, "NATIVE_DEPENDENCY_NOT_SUPPORTED")


def test_native_wheel_metadata_is_not_accepted_as_portable_python(roots):
    distribution(roots[1], "boto3", tag="cp312-cp312-macosx_11_0_arm64")
    reject(roots, "NATIVE_DEPENDENCY_NOT_SUPPORTED")


@pytest.mark.parametrize("where", ["source", "package", "resource", "source_root"])
def test_symlinks_cannot_pull_files_from_outside_the_selected_roots(roots, where):
    source, packages, output = roots
    external = source.parent / "private-file"
    external.write_text("PRIVATE")
    if where == "source_root":
        link = source.parent / "linked-source"
        link.symlink_to(source, target_is_directory=True)
        reject((link, packages, output), "SYMLINK_NOT_ALLOWED")
        return
    target = {"source": source / "services/injected.py", "package": packages / "boto3/injected.py",
              "resource": source / "resources/prompt_books/required.json"}[where]
    if target.exists():
        target.unlink()
    target.symlink_to(external)
    reject(roots, "SYMLINK_NOT_ALLOWED")


@pytest.mark.parametrize("selected", ["source", "packages", "same"])
def test_input_and_output_roots_must_not_overlap(roots, selected):
    source, packages, _ = roots
    output = source / "output" if selected == "source" else packages / "output"
    reject((source, source if selected == "same" else packages, output), "OVERLAPPING_DIRECTORIES")


def test_existing_output_is_never_overwritten(roots):
    output = roots[2]
    write(output, "mock-lambda.zip", "keep existing")
    reject(roots, "OUTPUT_CONFLICT")
    assert (output / "mock-lambda.zip").read_text() == "keep existing"
    assert not (output / "artifact-manifest.json").exists()


def test_record_checksum_tampering_and_unowned_files_are_rejected(roots):
    write(roots[1], "boto3/__init__.py", "PRIVATE TAMPER")
    reject(roots, "DEPENDENCY_CHECKSUM_MISMATCH")


def test_extra_distribution_or_module_cannot_be_copied_from_a_whole_venv(roots):
    write(roots[1], "pytest/__init__.py", "PRIVATE")
    reject(roots, "DEPENDENCY_SET_INVALID")


def test_metadata_without_the_actual_runtime_package_is_rejected(roots):
    packages = roots[1]
    module = "boto3/__init__.py"
    (packages / module).unlink()
    record = packages / "boto3-1.34.140.dist-info/RECORD"
    rows = list(csv.reader(io.StringIO(record.read_text())))
    output = io.StringIO()
    csv.writer(output).writerows(row for row in rows if row[0] != module)
    record.write_text(output.getvalue())
    reject(roots, "DEPENDENCY_RECORD_INVALID")


def test_competing_manifest_creation_does_not_overwrite_foreign_output(roots, monkeypatch):
    output = roots[2]
    original = builder.os.link
    def link(source, destination):
        if destination.name == "artifact-manifest.json":
            destination.write_text("concurrent owner")
            raise FileExistsError()
        return original(source, destination)
    monkeypatch.setattr(builder.os, "link", link)
    reject(roots, "OUTPUT_CONFLICT")
    assert (output / "mock-lambda.zip").exists()
    assert (output / "artifact-manifest.json").read_text() == "concurrent owner"


def test_rollback_cannot_delete_a_concurrently_replaced_zip(roots, monkeypatch):
    output = roots[2]
    original = builder.os.link
    def link(source, destination):
        if destination.name == "artifact-manifest.json":
            zip_path = output / "mock-lambda.zip"
            zip_path.unlink()
            zip_path.write_bytes(b"different owner zip")
            destination.write_bytes(b"different owner manifest")
            raise FileExistsError()
        return original(source, destination)
    monkeypatch.setattr(builder.os, "link", link)
    reject(roots, "OUTPUT_CONFLICT")
    assert (output / "mock-lambda.zip").read_bytes() == b"different owner zip"
    assert (output / "artifact-manifest.json").read_bytes() == b"different owner manifest"
    assert not [path for path in output.iterdir() if path.name.startswith(".artifact-")]


def test_direct_dependency_pins_and_declared_resources_are_required(roots):
    write(roots[0], "requirements.txt", "boto3==0.0.0\nsentry-sdk==2.22.0\n")
    reject(roots, "REQUIREMENTS_NOT_SUPPORTED")


def test_missing_prompt_book_cannot_silently_remove_coaching(roots):
    (roots[0] / "resources/prompt_books/required.json").unlink()
    reject(roots, "REQUIRED_FILE_MISSING")


def test_prompt_table_cannot_select_an_environment_file(roots):
    write(roots[0], "services/guide_prompts.py", "_TABLE_OF_PROMPT={'language_guideline':{'g':{'default':'.env.json'}}}")
    write(roots[0], "resources/prompt_books/.env.json", "PRIVATE")
    reject(roots, "PROMPT_RESOURCE_CONTRACT_INVALID")


def test_cli_failure_does_not_print_private_path_or_traceback(roots, capsys):
    private = roots[0].parent / "PRIVATE-MISSING-SOURCE"
    result = builder.main(["--source-root", str(private), "--packages-dir", str(roots[1]), "--outdir", str(roots[2])])
    captured = capsys.readouterr()
    assert result == 1 and captured.err == "DIRECTORY_REQUIRED\n" and captured.out == ""

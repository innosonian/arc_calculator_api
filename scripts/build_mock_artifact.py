"""Build a local, deterministic runtime ZIP from explicit source/dependency roots.

No installer, environment discovery, application import, AWS, or network call.
The result is not proof of a Linux Lambda deployment or a verified remote API.
"""

import argparse
import ast
import base64
import csv
from email.parser import BytesParser
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tempfile
import zipfile


SOURCE_PACKAGES = ("calculators", "config", "data_handlers", "mock_journey", "models", "services", "transformers", "util")
DEPENDENCIES = {
    "boto3": "boto3", "botocore": "botocore", "jmespath": "jmespath", "s3transfer": "s3transfer",
    "sentry-sdk": "sentry_sdk", "urllib3": "urllib3", "certifi": "certifi", "python-dateutil": "dateutil", "six": "six.py",
}
DIRECT_PINS = {"boto3": "1.34.140", "sentry-sdk": "2.22.0"}
ENTRYPOINTS = ("lambda_handler.run", "submit_arc.run", "mock_journey.handler.run", "mock_journey.worker.run", "mock_journey.dispatch.run")
EXCLUDED_PARTS = {"tests", "integration_tests", "docs", "scripts", "__pycache__", ".git", ".venv"}
CA_FILES = {"certifi/cacert.pem", "botocore/cacert.pem"}
# Standard ZIP Lambda limits, including the existing direct --zip-file path.
# https://docs.aws.amazon.com/lambda/latest/dg/gettingstarted-limits.html
MAX_ZIP_BYTES = 50 * 1024 * 1024
MAX_UNZIPPED_BYTES = 250 * 1024 * 1024
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9.!+_-]*\Z")


class ArtifactError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _fail(code):
    raise ArtifactError(code)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _name(value):
    return re.sub(r"[-_.]+", "-", value).lower()


def _path(value, *, existing):
    path = Path(value).absolute()
    if ".." in path.parts:
        _fail("UNSAFE_PATH")
    for component in (path, *path.parents):
        if component.is_symlink():
            _fail("SYMLINK_NOT_ALLOWED")
    if existing and not path.is_dir():
        _fail("DIRECTORY_REQUIRED")
    if path.exists() and not path.is_dir():
        _fail("DIRECTORY_REQUIRED")
    return path


def _relative(name):
    path = PurePosixPath(name)
    if (not name or "\\" in name or path.is_absolute() or ".." in path.parts
            or str(path) != name or any(not part for part in path.parts)):
        _fail("UNSAFE_FILE_NAME")
    return path


def _forbidden(name, *, dependency=False):
    path = _relative(name)
    excluded = EXCLUDED_PARTS - {"docs", "scripts"} if dependency else EXCLUDED_PARTS
    if any(part in excluded or part.startswith(".env") or part.startswith(".git") for part in path.parts):
        return True
    leaf = path.name.lower()
    return (leaf.endswith((".pyc", ".pyo", ".pth", ".egg-link", ".key", ".p12", ".pfx"))
            or leaf in {"credentials", "id_rsa", "id_ed25519", "env.json"}
            or (leaf.endswith((".pem", ".crt")) and name not in CA_FILES))


def _read_file(root, relative):
    _relative(relative)
    path = root / relative
    for component in (path, *path.parents):
        if component == root.parent:
            break
        if component.is_symlink():
            _fail("SYMLINK_NOT_ALLOWED")
    if not path.is_file():
        _fail("REQUIRED_FILE_MISSING")
    with path.open("rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            _fail("REGULAR_FILE_REQUIRED")
        return stream.read()


def _walk(root):
    for directory, folders, files in os.walk(root, followlinks=False):
        for name in folders + files:
            path = Path(directory) / name
            if path.is_symlink():
                _fail("SYMLINK_NOT_ALLOWED")
        folders[:] = sorted(folders)
        for name in sorted(files):
            yield (Path(directory) / name).relative_to(root).as_posix()


def _source_files(root):
    files = {name: _read_file(root, name) for name in ("lambda_handler.py", "main.py", "submit_arc.py")}
    for package in SOURCE_PACKAGES:
        directory = root / package
        if not directory.is_dir() or directory.is_symlink():
            _fail("SOURCE_PACKAGE_INVALID")
        for local in _walk(directory):
            relative = f"{package}/{local}"
            if relative.endswith(".py") and not _forbidden(relative):
                files[relative] = _read_file(root, relative)
    if any(entry.rsplit(".", 1)[0].replace(".", "/") + ".py" not in files for entry in ENTRYPOINTS):
        _fail("ENTRYPOINT_MISSING")
    # Read the literal lookup, without importing source or traversing unrelated resources.
    try:
        tree = ast.parse(files["services/guide_prompts.py"].decode("utf-8"))
        tables = [ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign)
                  and any(isinstance(target, ast.Name) and target.id == "_TABLE_OF_PROMPT" for target in node.targets)]
        if len(tables) != 1:
            _fail("PROMPT_RESOURCE_CONTRACT_INVALID")
        names = {filename for regions in tables[0]["language_guideline"].values() for filename in regions.values()}
        if not names or any(type(name) is not str or PurePosixPath(name).name != name or not name.endswith(".json") for name in names):
            _fail("PROMPT_RESOURCE_CONTRACT_INVALID")
    except (ValueError, SyntaxError, KeyError, TypeError, AttributeError, UnicodeError):
        _fail("PROMPT_RESOURCE_CONTRACT_INVALID")
    for name in sorted(names):
        relative = "resources/prompt_books/" + name
        if _forbidden(relative):
            _fail("PROMPT_RESOURCE_CONTRACT_INVALID")
        files[relative] = _read_file(root, relative)
    return files


def _pins(body):
    try:
        rows = [line.strip() for line in body.decode("utf-8").splitlines() if line.strip() and not line.lstrip().startswith("#")]
        values = {}
        for row in rows:
            name, version = row.split("==")
            normalized = _name(name)
            if normalized in values or not _VERSION.fullmatch(version):
                _fail("REQUIREMENTS_NOT_SUPPORTED")
            values[normalized] = version
        if values != DIRECT_PINS:
            _fail("REQUIREMENTS_NOT_SUPPORTED")
        return values
    except (UnicodeError, ValueError):
        _fail("REQUIREMENTS_NOT_SUPPORTED")


def _metadata_value(message, key):
    values = message.get_all(key) or []
    if len(values) != 1:
        _fail("DEPENDENCY_METADATA_INVALID")
    return values[0]


def _dependency_files(root, pins):
    names = {name for name in _walk(root)
             if not {"tests", "integration_tests"}.intersection(PurePosixPath(name).parts)
             and PurePosixPath(name).parts[0] != "bin"}
    if any(_forbidden(name, dependency=True) for name in names):
        _fail("FORBIDDEN_DEPENDENCY_FILE")
    if any(PurePosixPath(name).suffix.lower() in {".so", ".dylib", ".dll", ".pyd", ".exe"} for name in names):
        _fail("NATIVE_DEPENDENCY_NOT_SUPPORTED")
    metadata_paths = sorted(name for name in names if re.fullmatch(r"[^/]+\.dist-info/METADATA", name))
    found, owners, files = {}, {}, {}
    for metadata_path in metadata_paths:
        directory = str(PurePosixPath(metadata_path).parent)
        metadata_body = _read_file(root, metadata_path)
        metadata = BytesParser().parsebytes(metadata_body)
        name, version = _name(_metadata_value(metadata, "Name")), _metadata_value(metadata, "Version")
        if name not in DEPENDENCIES or name in found or not _VERSION.fullmatch(version):
            _fail("DEPENDENCY_SET_INVALID")
        if name in pins and pins[name] != version:
            _fail("DEPENDENCY_PIN_MISMATCH")
        wheel_body = _read_file(root, directory + "/WHEEL")
        wheel = BytesParser().parsebytes(wheel_body)
        tags = wheel.get_all("Tag") or []
        if (_metadata_value(wheel, "Root-Is-Purelib") != "true" or not tags
                or any(not re.fullmatch(r"py[23](?:\.py[23])?-none-any", tag) for tag in tags)
                or not any("py3" in tag.split("-", 1)[0].split(".") for tag in tags)):
            _fail("NATIVE_DEPENDENCY_NOT_SUPPORTED")
        record_path = directory + "/RECORD"
        try:
            rows = list(csv.reader(io.StringIO(_read_file(root, record_path).decode("utf-8"))))
        except (ValueError, UnicodeError, csv.Error):
            _fail("DEPENDENCY_RECORD_INVALID")
        declared = set()
        for row in rows:
            if len(row) != 3:
                _fail("DEPENDENCY_RECORD_INVALID")
            relative, checksum, size = row
            # Installed RECORD also names generated pyc and external CLI files.
            if ({"__pycache__", "tests", "integration_tests"}.intersection(PurePosixPath(relative).parts)
                    or relative.endswith((".pyc", ".pyo"))):
                continue
            if relative.startswith("../"):
                if re.fullmatch(r"(?:\.\./)+bin/[^/]+", relative):
                    continue
                _fail("DEPENDENCY_RECORD_INVALID")
            _relative(relative)
            prefix = DEPENDENCIES[name]
            if not (relative == prefix or relative.startswith(prefix + "/") or relative.startswith(directory + "/")):
                _fail("DEPENDENCY_OWNERSHIP_INVALID")
            if relative in declared or relative in owners:
                _fail("DEPENDENCY_OWNERSHIP_INVALID")
            declared.add(relative)
            data = _read_file(root, relative)
            if size and (not size.isdecimal() or int(size) != len(data)):
                _fail("DEPENDENCY_CHECKSUM_MISMATCH")
            if checksum:
                actual = "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode("ascii").rstrip("=")
                if checksum != actual:
                    _fail("DEPENDENCY_CHECKSUM_MISMATCH")
            elif relative != record_path and not relative.startswith(directory + "/"):
                _fail("DEPENDENCY_CHECKSUM_REQUIRED")
            files["packages/" + relative] = data
            owners[relative] = name
        module = DEPENDENCIES[name]
        module = module if module.endswith(".py") else module + "/__init__.py"
        if not {metadata_path, directory + "/WHEEL", record_path, module} <= declared:
            _fail("DEPENDENCY_RECORD_INVALID")
        found[name] = {"version": version, "metadata_sha256": _sha(metadata_body), "wheel_sha256": _sha(wheel_body),
                       "wheel_tags": tags, "requires_dist": metadata.get_all("Requires-Dist") or []}
    if set(found) != set(DEPENDENCIES) or names != set(owners):
        _fail("DEPENDENCY_SET_INVALID")
    return files, found


def build_artifact(source_root, packages_dir, outdir):
    source, packages, output = [_path(value, existing=index < 2) for index, value in enumerate((source_root, packages_dir, outdir))]
    roots = (source, packages, output)
    if any(a == b or a in b.parents or b in a.parents for index, a in enumerate(roots) for b in roots[index + 1:]):
        _fail("OVERLAPPING_DIRECTORIES")
    destinations = (output / "mock-lambda.zip", output / "artifact-manifest.json")
    if any(path.exists() or path.is_symlink() for path in destinations):
        _fail("OUTPUT_CONFLICT")
    requirements = _read_file(source, "requirements.txt")
    pins = _pins(requirements)
    entries = _source_files(source)
    dependencies, provenance = _dependency_files(packages, pins)
    entries.update(dependencies)
    if sum(len(data) for data in entries.values()) > MAX_UNZIPPED_BYTES:
        _fail("UNZIPPED_ARTIFACT_TOO_LARGE")
    manifest = {
        "schema": "arc-mock-local-artifact-v1", "entrypoints": list(ENTRYPOINTS),
        "requirements": {"sha256": _sha(requirements), "direct_pins": pins},
        "dependencies": provenance,
        "files": {name: {"sha256": _sha(data), "size": len(data)} for name, data in sorted(entries.items())},
        "build_python": list(sys.version_info[:3]),
        "verification_scope": "local-source-and-installed-pure-python-files-only",
        "not_verified": ["independent-wheel-origin", "dependency-resolution", "linux-lambda-execution", "remote-contract", "deployment"],
    }
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".artifact-", dir=output) as temporary:
        staged_zip = Path(temporary) / "runtime.zip"
        with zipfile.ZipFile(staged_zip, "x") as archive:
            for name, data in sorted(entries.items()):
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system, info.external_attr = 3, 0o100644 << 16
                archive.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=6)
        zip_size = staged_zip.stat().st_size
        if zip_size > MAX_ZIP_BYTES:
            _fail("DIRECT_UPLOAD_ARTIFACT_TOO_LARGE")
        # Hash the finished ZIP without keeping a second archive-sized copy.
        with staged_zip.open("rb") as stream:
            zip_sha256 = hashlib.file_digest(stream, "sha256").hexdigest()
        manifest["zip"] = {"sha256": zip_sha256, "size": zip_size}
        staged_manifest = Path(temporary) / "manifest.json"
        staged_manifest.write_bytes((json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode("utf-8"))
        try:
            os.link(staged_zip, destinations[0])
            os.link(staged_manifest, destinations[1])
        except OSError as error:
            # Another builder/operator may replace a published path. Never
            # unlink destination files during rollback, even after an inode
            # check (which would itself race). Only our private temp is cleaned.
            _fail("OUTPUT_CONFLICT" if isinstance(error, FileExistsError) else "ARTIFACT_WRITE_FAILED")
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source-root", "packages-dir", "outdir"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args(argv)
    try:
        manifest = build_artifact(args.source_root, args.packages_dir, args.outdir)
    except ArtifactError as error:
        print(error.code, file=sys.stderr)
        return 1
    except (OSError, ValueError, TypeError, zipfile.BadZipFile):
        print("ARTIFACT_BUILD_FAILED", file=sys.stderr)
        return 1
    print(json.dumps({"artifact_sha256": manifest["zip"]["sha256"], "file_count": len(manifest["files"])}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

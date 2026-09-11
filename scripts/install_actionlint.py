"""Install one checksum-pinned official actionlint binary into a temporary directory.

No archive member is read or executable written before the archive hash matches.
The installer never extracts archive paths and never overwrites an existing file.
"""

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tarfile
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


VERSION = "1.7.12"
RELEASE_URL = f"https://github.com/rhysd/actionlint/releases/download/v{VERSION}"
# Official actionlint_1.7.12_checksums.txt, read during the 2026-09-11 review.
CHECKSUMS = {
    "linux_amd64": "8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8",
    "darwin_arm64": "aba9ced2dee8d27fecca3dc7feb1a7f9a52caefa1eb46f3271ea66b6e0e6953f",
    "darwin_amd64": "5b44c3bc2255115c9b69e30efc0fecdf498fdb63c5d58e17084fd5f16324c644",
}
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_BINARY_BYTES = 32 * 1024 * 1024
HTTP_TIMEOUT_SECONDS = 30


class InstallError(ValueError):
    """A fixed diagnostic without response bodies or redirect URLs."""


class HTTPSRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(newurl).scheme != "https":
            raise InstallError("ACTIONLINT_REDIRECT_NOT_HTTPS")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def platform_key():
    machine = {"x86_64": "amd64", "amd64": "amd64", "arm64": "arm64", "aarch64": "arm64"}.get(
        platform.machine().lower(), "unsupported"
    )
    key = f"{platform.system().lower()}_{machine}"
    if key not in CHECKSUMS:
        raise InstallError("ACTIONLINT_PLATFORM_UNSUPPORTED")
    return key


def download_archive(asset):
    if asset not in {f"actionlint_{VERSION}_{key}.tar.gz" for key in CHECKSUMS}:
        raise InstallError("ACTIONLINT_ASSET_UNSUPPORTED")
    request = Request(f"{RELEASE_URL}/{asset}", headers={"User-Agent": "arc-actions-validation"})
    with build_opener(HTTPSRedirectHandler()).open(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
        if urlsplit(response.url).scheme != "https":
            raise InstallError("ACTIONLINT_RESPONSE_NOT_HTTPS")
        archive = response.read(MAX_ARCHIVE_BYTES + 1)
    if len(archive) > MAX_ARCHIVE_BYTES:
        raise InstallError("ACTIONLINT_ARCHIVE_TOO_LARGE")
    return archive


def verified_binary(archive, expected_sha256):
    if len(archive) > MAX_ARCHIVE_BYTES:
        raise InstallError("ACTIONLINT_ARCHIVE_TOO_LARGE")
    if hashlib.sha256(archive).hexdigest() != expected_sha256:
        raise InstallError("ACTIONLINT_CHECKSUM_MISMATCH")
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as package:
        matches = []
        for index, member in enumerate(package):
            if index >= 128:
                raise InstallError("ACTIONLINT_ARCHIVE_TOO_MANY_MEMBERS")
            if member.name == "actionlint":
                matches.append(member)
        if len(matches) != 1 or not matches[0].isfile():
            raise InstallError("ACTIONLINT_BINARY_MEMBER_INVALID")
        member = matches[0]
        if not 0 < member.size <= MAX_BINARY_BYTES:
            raise InstallError("ACTIONLINT_BINARY_SIZE_INVALID")
        with package.extractfile(member) as stream:
            binary = stream.read(MAX_BINARY_BYTES + 1)
        if len(binary) != member.size:
            raise InstallError("ACTIONLINT_BINARY_SIZE_INVALID")
        return binary


def install(output_dir):
    key = platform_key()
    asset = f"actionlint_{VERSION}_{key}.tar.gz"
    binary = verified_binary(download_archive(asset), CHECKSUMS[key])
    directory = Path(output_dir).absolute()
    if directory.is_symlink():
        raise InstallError("ACTIONLINT_OUTPUT_DIRECTORY_INVALID")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = directory / "actionlint"
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(binary)
    result = subprocess.run([str(target), "-version"], check=True, capture_output=True, text=True, timeout=10)
    if result.stdout.splitlines()[:1] != [VERSION]:
        raise InstallError("ACTIONLINT_VERSION_MISMATCH")
    return {"actionlint_version": VERSION, "asset": asset, "archive_sha256": CHECKSUMS[key],
            "binary_sha256": hashlib.sha256(binary).hexdigest(), "binary_path": str(target)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args(argv)
    try:
        evidence = install(args.output_dir)
    except InstallError as error:
        print(str(error), file=sys.stderr)
        return 1
    except (OSError, ValueError, tarfile.TarError, subprocess.SubprocessError):
        print("ACTIONLINT_INSTALL_FAILED", file=sys.stderr)
        return 1
    print(json.dumps(evidence, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

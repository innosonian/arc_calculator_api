"""Decision Q15=A (2026-09-28): the parity tool requires an explicit output folder.

Running it without --output-dir used to overwrite the checked-in golden fixtures.
These checks stop at argument parsing, so the external reference is never read.
"""

import hashlib
import os
from pathlib import Path
import subprocess
import sys

import pytest

from scripts import verify_reference_parity as parity


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/verify_reference_parity.py"
FIXTURES = ROOT / "tests/fixtures/reference_parity"


def _fixture_digest():
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(FIXTURES.iterdir())}


@pytest.mark.parametrize("extra", [[], ["--guard-check-only"], ["--case", "adult"]])
def test_missing_output_dir_fails_without_touching_golden_fixtures(extra, tmp_path):
    before = _fixture_digest()
    completed = subprocess.run([sys.executable, "-B", str(SCRIPT), "--reference-dir", str(tmp_path), *extra],
                               capture_output=True, text=True, encoding="utf-8", cwd=ROOT, timeout=60)
    assert completed.returncode == 2
    assert "--output-dir" in completed.stderr
    assert "required" in completed.stderr
    assert completed.stdout == ""
    assert _fixture_digest() == before


def test_main_rejects_missing_output_dir_in_process(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--reference-dir", str(tmp_path)])
    with pytest.raises(SystemExit) as raised:
        parity.main()
    assert raised.value.code == 2


def test_help_documents_separate_output_folder():
    env = dict(os.environ, COLUMNS="400")
    completed = subprocess.run([sys.executable, "-B", str(SCRIPT), "--help"], env=env,
                               capture_output=True, text=True, encoding="utf-8", cwd=ROOT, timeout=60)
    assert completed.returncode == 0
    assert "--output-dir" in completed.stdout
    assert "골든 fixture 를 덮어쓰지 않도록 별도 폴더 지정" in completed.stdout

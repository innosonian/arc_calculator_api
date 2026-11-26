"""Decision Q15=A (2026-09-28): the parity tool requires an explicit output folder.

Running it without --output-dir used to overwrite the checked-in golden fixtures.
These checks stop at argument parsing, so the external reference is never read.
"""

import hashlib
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

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


@pytest.mark.parametrize("destination", ["reference", "fixtures", "fixtures/nested"])
def test_output_cannot_mutate_reference_or_golden_fixtures(destination, tmp_path):
    before = _fixture_digest()
    output = tmp_path if destination == "reference" else ROOT / "tests" / destination
    completed = subprocess.run(
        [sys.executable, "-B", str(SCRIPT), "--reference-dir", str(tmp_path), "--output-dir", str(output)],
        capture_output=True, text=True, encoding="utf-8", cwd=ROOT, timeout=60,
    )
    assert completed.returncode == 2
    assert "Output must not" in completed.stderr
    assert completed.stdout == ""
    assert _fixture_digest() == before
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("status", [" M calculators/waveform.py\n", "M  calculators/waveform.py\n",
                                  " D scoring/context.py\n", "?? services/untracked.py\n"])
def test_reference_commit_rejects_changes_at_the_approved_head(monkeypatch, tmp_path, status):
    def git(command, **kwargs):
        assert command[:4] == ["git", "--no-optional-locks", "-C", str(tmp_path)]
        if command[4:] == ["rev-parse", "HEAD"]:
            return SimpleNamespace(stdout=parity.REFERENCE_COMMIT + "\n")
        assert command[4:] == ["status", "--porcelain", "--untracked-files=all"]
        return SimpleNamespace(stdout=status)

    monkeypatch.setattr(parity.subprocess, "run", git)
    with pytest.raises(ValueError, match="working tree must be clean"):
        parity._reference_commit(tmp_path)


def test_reference_commit_accepts_only_the_clean_approved_head(monkeypatch, tmp_path):
    outputs = iter((parity.REFERENCE_COMMIT + "\n", "", "wrong-commit\n"))
    monkeypatch.setattr(parity.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout=next(outputs)))
    assert parity._reference_commit(tmp_path) == parity.REFERENCE_COMMIT
    with pytest.raises(ValueError, match="HEAD does not match"):
        parity._reference_commit(tmp_path)


@pytest.mark.parametrize("dirty_after_workers", [False, True])
def test_dirty_reference_never_writes_authority_reports(monkeypatch, tmp_path, dirty_after_workers):
    output = tmp_path / "reports"
    calls = []

    def check_reference(repo):
        calls.append(repo)
        if not dirty_after_workers or len(calls) == 2:
            raise ValueError("Reference working tree must be clean at the approved commit.")
        return parity.REFERENCE_COMMIT

    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--reference-dir", str(tmp_path / "reference"),
                                     "--current-dir", str(ROOT), "--output-dir", str(output)])
    monkeypatch.setattr(parity, "_reference_commit", check_reference)
    monkeypatch.setattr(parity, "_source_manifest", lambda repo: {})
    monkeypatch.setattr(parity, "_build_cases", lambda repo: [])
    monkeypatch.setattr(parity, "_run_worker", lambda *args, **kwargs: {})
    with pytest.raises(SystemExit) as raised:
        parity.main()
    assert raised.value.code == 2
    assert len(calls) == (2 if dirty_after_workers else 1)
    assert not output.exists()

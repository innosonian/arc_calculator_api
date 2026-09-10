"""Load the existing ZIP's bundled dependencies before importing SDK consumers."""

from pathlib import Path
import sys


def configure_imports():
    path = str(Path(__file__).resolve().parent.parent / "packages")
    if Path(path).is_dir() and path not in sys.path:
        sys.path.insert(0, path)

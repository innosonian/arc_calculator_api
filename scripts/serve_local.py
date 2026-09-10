"""Start the isolated local server; no automatic downloads or cloud bootstrap."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from local_server.cli import main

if __name__ == "__main__":
    raise SystemExit(main())

"""Run fixed local suites against a new, owned loopback DynamoDB process.

No existing database or listening service is adopted. The selected distribution
is verified by the same launcher as the local server. Temporary test data and
the child process are removed on exit; no AWS/ARC request is configured.
"""

import argparse
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.test_suites import LOCAL_SUITES, SUITE_TIMEOUT_SECONDS  # noqa: E402  (standard library only)

TIMEOUT_EXIT_CODE = 3


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dynamodb-home", required=True, type=Path)
    parser.add_argument("--suite", choices=tuple(LOCAL_SUITES), required=True)
    parser.add_argument("--timeout-seconds", type=int, default=None,
                        help="override the suite's budget from scripts/test_suites.py (positive seconds)")
    args = parser.parse_args(argv)
    timeout = SUITE_TIMEOUT_SECONDS[args.suite] if args.timeout_seconds is None else args.timeout_seconds
    if timeout <= 0:
        parser.error("--timeout-seconds must be a positive number of seconds")
    from local_server.cli import isolated_environment, verify_distribution, start_database, stop_database

    isolated_environment()
    home = verify_distribution(args.dynamodb_home.absolute())
    with tempfile.TemporaryDirectory(prefix="arc-internal-validation-") as directory:
        temporary = Path(directory)
        os.chmod(temporary, 0o700)
        database = temporary / "database"
        database.mkdir(mode=0o700)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        child = start_database(home, database, port, temporary)
        try:
            env = dict(os.environ, ARC_TEST_DYNAMODB_ENDPOINT=f"http://127.0.0.1:{port}",
                       ARC_TEST_DYNAMODB_HOME=str(home),
                       ARC_LOCAL_TEST_PYTHON=sys.executable,
                       ARC_LOCAL_TEST_DYNAMODB_HOME=str(home),
                       ARC_TEST_HTTPS_HOST="127.0.0.1", ARC_TEST_HTTPS_PORT="0")
            paths = LOCAL_SUITES[args.suite]
            try:
                result = subprocess.run([sys.executable, "-m", "pytest", "-q", *paths],
                                        cwd=ROOT, env=env, timeout=timeout, check=False)
            except subprocess.TimeoutExpired:
                # subprocess.run has already killed the pytest child.
                print(f"Local validation suite '{args.suite}' exceeded its {timeout}-second budget "
                      "and was stopped.", file=sys.stderr, flush=True)
                return TIMEOUT_EXIT_CODE
            return result.returncode
        finally:
            stop_database(child)


if __name__ == "__main__":
    raise SystemExit(main())

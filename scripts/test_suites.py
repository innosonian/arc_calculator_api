"""Fixed pytest selections shared by the Actions and local validation runners.

Standard library only: run_actions_regression.py imports this after its network
audit hook and before any SDK or application module. Selections are repository
file or directory paths, never pytest options.
"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
# Every tests/test_*.py joins the socket-free unit suite unless it is listed here
# with a reason. The suite runs with only requirements.txt + requirements-ci.txt
# (no waitress) and under run_actions_regression.py's socket/AWS audit guards.
# A new file that needs a socket or local-only dependency must be added here
# instead of weakening those guards; the DynamoDB Local `integration` suite
# below still runs it.
OFFLINE_UNIT_EXCLUDED = {}
# The PR CI runs `integration` and `boundary` as two parallel DynamoDB Local jobs
# of the same names, next to the offline unit suite. The two suites are
# disjoint and, with the offline suite, cover `all`; tests/ is not repeated
# against DynamoDB Local except for the files excluded above.
LOCAL_SUITES = {
    # Real DynamoDB Local state/journeys and the in-process HTTP pipeline, plus
    # the loopback TLS transport tests (explicit TLS host/port, no database):
    # they are few and short, so they join the suite without a job of their own.
    "integration": ("integration_tests", "http_pipeline_tests", "transport_integration_tests",
                    *sorted(OFFLINE_UNIT_EXCLUDED)),
    # Local server CLI/HTTP/live-journey boundaries with an owned DB child.
    "boundary": ("local_server_tests",),
    # scripts/ holds no tests since the auxiliary Lambdas were removed (Q14=C);
    # collecting it would import this module as a test file.
    "all": ("tests", "local_server_tests", "integration_tests", "http_pipeline_tests",
            "transport_integration_tests"),
}
# Per-suite budget of scripts/validate_local_integration.py (seconds), one per
# LOCAL_SUITES entry. The two PR CI suites get at least three times their
# measured duration (integration 193 s, boundary 174 s on 2026-09-30) and stay
# below their CI job limits, so a hung suite ends with the runner's fixed
# message instead of the job limit. `all` is a local-only run (about 12 min).
SUITE_TIMEOUT_SECONDS = {"integration": 600, "boundary": 600, "all": 1800}


def offline_unit_tests(root=ROOT):
    """Return the sorted socket-free unit test files as repository-relative paths."""
    root = Path(root)
    selected = (path.relative_to(root).as_posix() for path in (root / "tests").glob("test_*.py"))
    return tuple(sorted(path for path in selected if path not in OFFLINE_UNIT_EXCLUDED))

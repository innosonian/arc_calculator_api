"""Fixed suite selections shared by the PR CI jobs and the local runner."""

from pathlib import Path

from scripts import test_suites as suites
from scripts import validate_actions as validation
from scripts import validate_local_integration as local_runner


ROOT = Path(__file__).resolve().parents[1]
# The deployment gate ran exactly these files before the suite was widened.
# Keep them in the default selection so the deploy job never regresses.
DEPLOYMENT_GATE = (
    "tests/test_deployment_preflight.py",
    "tests/test_deployment_preflight_security.py",
    "tests/test_mock_artifact.py",
    "tests/test_aws_runtime.py",
    "tests/test_aws_dev_course.py",
    "tests/test_aws_dev_bundle.py",
    "tests/test_vcc_http_ingress_security.py",
    "tests/test_aws_lease.py",
    "tests/test_aws_storage.py",
    "tests/test_aws_logs.py",
    "tests/test_aws_relay_entrypoint.py",
    "tests/test_aws_relay_budget.py",
    "tests/test_validate_actions.py",
    "tests/test_actions_regression.py",
)


def test_offline_suite_is_every_unit_file_except_documented_exclusions():
    selected = suites.offline_unit_tests(ROOT)
    every = {path.relative_to(ROOT).as_posix() for path in (ROOT / "tests").glob("test_*.py")}
    assert list(selected) == sorted(selected) and len(set(selected)) == len(selected)
    assert set(selected) == every - set(suites.OFFLINE_UNIT_EXCLUDED)
    assert all((ROOT / path).is_file() for path in selected)
    assert set(DEPLOYMENT_GATE) <= set(selected)
    assert "tests/test_ci_suites.py" in selected


def test_every_exclusion_names_a_unit_file_and_a_reason():
    for path, reason in suites.OFFLINE_UNIT_EXCLUDED.items():
        assert path.startswith("tests/test_") and path.endswith(".py") and "/" not in path[len("tests/"):]
        assert isinstance(reason, str) and reason.strip()
    assert not set(DEPLOYMENT_GATE) & set(suites.OFFLINE_UNIT_EXCLUDED)


def test_offline_suite_ignores_helpers_and_other_directories(tmp_path):
    (tmp_path / "tests").mkdir()
    for name in ("test_b.py", "test_a.py", "conftest.py", "vcc_support.py", "test_data.json"):
        (tmp_path / "tests" / name).write_text("", encoding="utf-8")
    (tmp_path / "integration_tests").mkdir()
    (tmp_path / "integration_tests" / "test_live.py").write_text("", encoding="utf-8")
    assert suites.offline_unit_tests(tmp_path) == ("tests/test_a.py", "tests/test_b.py")


def test_local_runner_uses_the_shared_directory_suites():
    assert local_runner.LOCAL_SUITES is suites.LOCAL_SUITES
    assert set(suites.LOCAL_SUITES) == {"integration", "boundary", "all"}
    every = set(suites.LOCAL_SUITES["all"])
    assert set(suites.LOCAL_SUITES["integration"]) | set(suites.LOCAL_SUITES["boundary"]) | {"tests"} <= every
    assert all((ROOT / path).is_dir() for path in every)
    # The local `all` still names every top-level directory that holds tests
    # (scripts/test_suites.py is this selection module, not a test file).
    holders = {path.name for path in ROOT.iterdir()
               if path.is_dir() and not path.name.startswith(".") and path.name != "scripts"
               and any(path.glob("test_*.py"))}
    assert holders == every


def selected_files(selection):
    """Expand repository-relative directories and files to their test files."""
    result = set()
    for name in selection:
        path = ROOT / name
        if path.is_dir():
            result |= {found.relative_to(ROOT).as_posix() for found in path.rglob("test_*.py")}
        else:
            assert path.is_file(), name
            result.add(name)
    return result


def test_parallel_ci_jobs_cover_the_local_all_suite_without_repeating_unit_tests():
    # `validate` runs the offline unit suite; one DynamoDB Local job runs each
    # listed suite. Together they select exactly the files of the local `all`.
    jobs = tuple(validation.DYNAMODB_JOBS)
    assert set(jobs) == {"integration", "boundary"} and set(jobs) < set(suites.LOCAL_SUITES)
    dynamodb = [name for job in jobs for name in suites.LOCAL_SUITES[job]]
    assert len(dynamodb) == len(set(dynamodb)), "the two DynamoDB Local suites must be disjoint"
    offline, database = set(suites.offline_unit_tests(ROOT)), selected_files(dynamodb)
    assert not offline & database, "tests/ already ran offline in the validate job"
    assert offline | database == selected_files(suites.LOCAL_SUITES["all"])
    # A unit file excluded from the offline suite still runs against DynamoDB Local.
    assert set(suites.OFFLINE_UNIT_EXCLUDED) <= set(suites.LOCAL_SUITES["integration"])
    assert "transport_integration_tests" in suites.LOCAL_SUITES["integration"]


# Local measurements of the two PR CI suites (2026-09-30 review): the runner
# budget must give each at least three times its measured duration.
MEASURED_SECONDS = {"integration": 193, "boundary": 174}


def test_each_suite_has_a_budget_of_at_least_three_times_its_measured_duration():
    assert set(suites.SUITE_TIMEOUT_SECONDS) == set(suites.LOCAL_SUITES) == {"integration", "boundary", "all"}
    assert all(type(seconds) is int and seconds > 0 for seconds in suites.SUITE_TIMEOUT_SECONDS.values())
    for name, measured in MEASURED_SECONDS.items():
        assert suites.SUITE_TIMEOUT_SECONDS[name] >= 3 * measured, name
        # The CI job limit (validator upper bound) is the outer bound; the
        # runner's own budget ends a hung suite with a fixed message first.
        assert suites.SUITE_TIMEOUT_SECONDS[name] < validation.MAX_INTEGRATION_MINUTES * 60
    assert suites.SUITE_TIMEOUT_SECONDS["all"] >= max(suites.SUITE_TIMEOUT_SECONDS.values())
    assert local_runner.SUITE_TIMEOUT_SECONDS is suites.SUITE_TIMEOUT_SECONDS


def test_each_dynamodb_local_job_command_names_its_own_suite():
    for job, step_ids in validation.DYNAMODB_JOBS.items():
        body = validation.SUITE_RUNS[step_ids[-1]][0]
        assert body.split()[-2:] == ["--suite", job]
        assert "scripts/validate_local_integration.py" in body
    assert set(validation.SUITE_RUNS) == {ids[-1] for ids in validation.DYNAMODB_JOBS.values()}

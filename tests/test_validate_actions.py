"""Offline counterexamples for the PR action validator; no action or AWS runs."""

import argparse
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import re
import sys
import tarfile

import pytest

from scripts import validate_actions as validation
from scripts import install_actionlint as installer


ROOT = Path(__file__).resolve().parents[1]
CHECKOUT = "actions/checkout"
PYTHON = "actions/setup-python"
AWS = "aws-actions/configure-aws-credentials"
# Frozen baseline fixtures, independent of which PR source runs the unit tests.
OLD_REFS = {
    CHECKOUT: CHECKOUT + "@11d5960a326750d5838078e36cf38b85af677262",
    PYTHON: PYTHON + "@a26af69be951a213d495a4c3e4e4022e16d87065",
    AWS: AWS + "@7474bc4690e29a8392af63c5b98e7449536d5c3a",
}


def candidate_refs():
    # Smoke candidates follow the actual uses steps when Dependabot updates CI.
    ci = validation.load_yaml((ROOT / ".github/workflows/validate_actions.yml").read_text())
    refs = [step["uses"] for job in ci["jobs"].values() for step in job["steps"] if "uses" in step]
    manifest = json.loads((ROOT / "scripts/actions_contracts.json").read_text())
    refs.extend(manifest["contract_candidates"])
    return {ref.split("@", 1)[0]: ref for ref in refs}


CANDIDATES = candidate_refs()


@pytest.fixture
def documents():
    deployment = validation.load_yaml(
        (ROOT / ".github/workflows/deploy_arc_lambdas.yml").read_text()
    )
    for action, ref in OLD_REFS.items():
        replace_action(deployment, action, ref)
    ci = validation.load_yaml((ROOT / ".github/workflows/validate_actions.yml").read_text())
    return deepcopy(deployment), deepcopy(deployment), deepcopy(ci)


def action_steps(workflow, action):
    return [
        step
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if step.get("uses", "").startswith(action + "@")
    ]


def replace_action(workflow, action, ref):
    steps = action_steps(workflow, action)
    assert steps, f"fixture has no {action} step"
    for step in steps:
        step["uses"] = ref


DYNAMODB_JOBS = tuple(validation.DYNAMODB_JOBS)
# Every (DynamoDB Local job, step id) pair, so each counterexample runs per job.
DYNAMODB_STEPS = [(job_id, step_id) for job_id, step_ids in validation.DYNAMODB_JOBS.items()
                  for step_id in step_ids]


def smoke_job(ci):
    assert set(ci["jobs"]) == {"validate", "integration", "boundary"}, "smoke job plus two DynamoDB Local jobs"
    return ci["jobs"]["validate"]


def integration_job(ci, job_id="integration"):
    return ci["jobs"][job_id]


def integration_step(ci, step_id, job_id="integration"):
    return next(step for step in integration_job(ci, job_id)["steps"] if step.get("id") == step_id)


def assert_rejected(code, operation, *args):
    with pytest.raises(validation.ValidationError, match=f"^{code}$"):
        operation(*args)


@pytest.mark.parametrize("changed", [(), (CHECKOUT,), (PYTHON,), (AWS,), tuple(CANDIDATES)])
def test_ci_first_individual_prs_and_combination_are_distinguished(documents, changed):
    base, deployment, ci = documents
    for action in changed:
        replace_action(deployment, action, CANDIDATES[action])
    result = validation.validate_workflows(base, deployment, ci)
    introduced = {ref for refs in result["introduced"].values() for ref in refs}
    assert introduced == {CANDIDATES[action] for action in changed}
    assert CANDIDATES[CHECKOUT] in result["smoke_refs"]
    assert CANDIDATES[PYTHON] in result["smoke_refs"]
    assert {step["uses"] for step in action_steps(deployment, AWS)} <= set(result["contract_refs"])
    assert CANDIDATES[AWS] not in result["smoke_refs"]


@pytest.mark.parametrize("action", [CHECKOUT, PYTHON])
def test_only_new_unexercised_deployment_ref_fails(documents, action):
    base, deployment, ci = documents
    replace_action(deployment, action, action + "@" + "0" * 40)
    assert_rejected("SMOKE_REF_UNCOVERED", validation.validate_workflows, base, deployment, ci)


@pytest.mark.parametrize("scope", ["job", "checkout", "python"])
@pytest.mark.parametrize("escape", ["if", "continue-on-error"])
def test_a_declared_but_skipped_or_failure_ignored_smoke_is_not_coverage(documents, scope, escape):
    base, deployment, ci = documents
    target = smoke_job(ci) if scope == "job" else action_steps(
        ci, CHECKOUT if scope == "checkout" else PYTHON
    )[0]
    target[escape] = False if escape == "if" else True
    assert_rejected("SMOKE_STRUCTURE_INVALID", validation.validate_workflows, base, deployment, ci)


@pytest.mark.parametrize("field,value", [
    ("needs", "a_skipped_job"),
    ("strategy", {"matrix": {"unused": [1]}}),
])
def test_smoke_cannot_depend_on_an_unexecuted_job_or_become_a_matrix(documents, field, value):
    base, deployment, ci = documents
    smoke_job(ci)[field] = value
    assert_rejected("SMOKE_STRUCTURE_INVALID", validation.validate_workflows, base, deployment, ci)


def test_candidate_in_another_skipped_job_does_not_cover_the_smoke(documents):
    base, deployment, ci = documents
    replace_action(deployment, CHECKOUT, CANDIDATES[CHECKOUT])
    candidate_step = deepcopy(action_steps(ci, CHECKOUT)[0])
    replace_action(ci, CHECKOUT, action_steps(base, CHECKOUT)[0]["uses"])
    ci["jobs"]["unused"] = {"if": False, "runs-on": "ubuntu-24.04", "steps": [candidate_step]}
    assert_rejected("SMOKE_STRUCTURE_INVALID", validation.validate_workflows, base, deployment, ci)


@pytest.mark.parametrize("step_id", ["actionlint", "regression"])
@pytest.mark.parametrize("change", ["remove", "echo_only"])
def test_required_checks_must_still_execute(documents, step_id, change):
    base, deployment, ci = documents
    steps = smoke_job(ci)["steps"]
    selected = next(step for step in steps if step.get("id") == step_id)
    if change == "remove":
        steps.remove(selected)
    else:
        selected["run"] = "echo '" + selected["run"] + "'"
    assert_rejected("SMOKE_STRUCTURE_INVALID", validation.validate_workflows, base, deployment, ci)


def test_reference_guard_cannot_compare_the_pr_head_to_itself(documents):
    base, deployment, ci = documents
    contracts = next(step for step in smoke_job(ci)["steps"] if step.get("id") == "contracts")
    contracts["env"]["PR_BASE_SHA"] = "${{ github.event.pull_request.head.sha }}"
    assert_rejected("CI_BOUNDARY_INVALID", validation.validate_workflows, base, deployment, ci)


@pytest.mark.parametrize("ref", [
    "actions/checkout@v6",
    "actions/checkout@${{ github.sha }}",
    "actions/checkout@" + "a" * 39,
    "actions/checkout@" + "g" * 40,
    "unreviewed-owner/checkout@" + "a" * 40,
])
def test_deployment_action_ref_must_be_an_official_full_sha(documents, ref):
    base, deployment, ci = documents
    replace_action(deployment, CHECKOUT, ref)
    assert_rejected("ACTION_REF_INVALID", validation.validate_workflows, base, deployment, ci)


@pytest.mark.parametrize("change", [
    "target_event", "closed_only", "write_contents", "id_token", "job_permission", "secret", "secret_index",
])
def test_ci_privilege_or_secret_expansion_is_rejected(documents, change):
    base, deployment, ci = documents
    if change == "target_event":
        ci["on"] = {"pull_request_target": {}}
    elif change == "closed_only":
        ci["on"]["pull_request"] = {"types": ["closed"]}
    elif change == "write_contents":
        ci["permissions"]["contents"] = "write"
    elif change == "id_token":
        ci["permissions"]["id-token"] = "write"
    elif change == "job_permission":
        smoke_job(ci)["permissions"] = {"contents": "write"}
    elif change == "secret_index":
        ci.setdefault("env", {})["UNSAFE_SETTING"] = "${{ secrets['SYNTHETIC_PROBE'] }}"
    else:
        ci.setdefault("env", {})["UNSAFE_SETTING"] = "${{ secrets.SYNTHETIC_ONLY }}"
    assert_rejected("CI_BOUNDARY_INVALID", validation.validate_workflows, base, deployment, ci)


@pytest.mark.parametrize("job_id", ["deploy-dev", "deploy-prod"])
@pytest.mark.parametrize("change", ["oidc_if", "key_if", "role", "session", "region", "preflight_order"])
def test_preserved_authentication_wiring_cannot_drift(documents, job_id, change):
    base, deployment, ci = documents
    steps = deployment["jobs"][job_id]["steps"]
    aws_steps = [step for step in steps if step.get("uses", "").startswith(AWS + "@")]
    oidc = next(step for step in aws_steps if "role-to-assume" in step["with"])
    keys = next(step for step in aws_steps if "aws-access-key-id" in step["with"])
    if change == "oidc_if":
        oidc["if"] = "env.AWS_ROLE_ARN == ''"
    elif change == "key_if":
        keys["if"] = "env.AWS_ROLE_ARN != ''"
    elif change == "role":
        oidc["with"]["role-to-assume"] = "arbitrary-fixture-role"
    elif change == "session":
        oidc["with"]["role-session-name"] = "arbitrary-fixture-session"
    elif change == "region":
        keys["with"]["aws-region"] = "arbitrary-fixture-region"
    else:
        preflight = next(step for step in steps if step.get("id") == "preflight")
        steps.remove(preflight)
        steps.append(preflight)
    assert_rejected("DEPLOYMENT_AUTH_INVALID", validation.validate_workflows, base, deployment, ci)


@pytest.mark.parametrize("change", ["permissions", "environment", "self_hosted", "container"])
def test_deployment_job_keeps_its_effective_identity_and_execution_context(documents, change):
    base, deployment, ci = documents
    job = deployment["jobs"]["deploy-dev"]
    if change == "permissions":
        job["permissions"] = {"contents": "read"}
    elif change == "environment":
        job["environment"] = "unreviewed-fixture-environment"
    elif change == "self_hosted":
        job["runs-on"] = "self-hosted"
    else:
        job["container"] = "unreviewed-fixture-image"
    assert_rejected("DEPLOYMENT_AUTH_INVALID", validation.validate_workflows, base, deployment, ci)


@pytest.mark.parametrize("scope", ["workflow", "job", "oidc"])
def test_environment_alias_cannot_silently_change_aws_action_inputs(documents, scope):
    base, deployment, ci = documents
    job = deployment["jobs"]["deploy-dev"]
    if scope == "workflow":
        target = deployment
    elif scope == "job":
        target = job
    else:
        target = next(step for step in job["steps"] if "role-to-assume" in step.get("with", {}))
    target.setdefault("env", {})["ROLE_CHAINING"] = "true"
    assert_rejected("DEPLOYMENT_AUTH_INVALID", validation.validate_workflows, base, deployment, ci)


@pytest.mark.parametrize("change", ["echo_only", "binding_source"])
def test_preflight_must_keep_its_executed_command_and_inputs(documents, change):
    base, deployment, ci = documents
    preflight = next(step for step in deployment["jobs"]["deploy-dev"]["steps"]
                     if step.get("id") == "preflight")
    if change == "echo_only":
        preflight["run"] = "echo '" + preflight["run"] + "'"
    else:
        preflight["env"]["ARC_DEPLOYMENT_BINDINGS"] = "different-fixture-binding.json"
    assert_rejected("DEPLOYMENT_AUTH_INVALID", validation.validate_workflows, base, deployment, ci)


@pytest.mark.parametrize("text", [
    "name: first\nname: second\n",
    "jobs:\n  smoke:\n    permissions:\n      contents: read\n      contents: write\n",
    "on:\n  pull_request: {}\n  pull_request: {}\n",
])
def test_duplicate_yaml_keys_fail_instead_of_silently_overwriting(text):
    assert_rejected("YAML_DUPLICATE_KEY", validation.load_yaml, text)


def test_workflow_on_key_is_not_yaml_11_boolean():
    document = validation.load_yaml("on:\n  pull_request: {}\nflag: false\nword: on\n")
    assert document == {"on": {"pull_request": {}}, "flag": False, "word": "on"}


@pytest.mark.parametrize("jobs", [None, []])
def test_malformed_jobs_has_a_structured_error_instead_of_a_traceback(documents, jobs):
    base, deployment, ci = documents
    deployment["jobs"] = jobs
    assert_rejected("YAML_STRUCTURE_INVALID", validation.validate_workflows, base, deployment, ci)


def metadata_fixture(runtime="node24"):
    raw = (
        "name: Fixture action\n"
        "description: Offline metadata only\n"
        "inputs:\n"
        "  aws-region:\n"
        "    description: Fixture region\n"
        "    required: true\n"
        "runs:\n"
        f"  using: {runtime}\n"
        "  main: dist/index.js\n"
    ).encode()
    return raw, {"sha256": hashlib.sha256(raw).hexdigest(), "runtime": runtime, "release": "v0.0.0"}


@pytest.mark.parametrize("runtime", ["node20", "node24"])
def test_metadata_contract_is_verified_without_executing_the_action(runtime):
    raw, record = metadata_fixture(runtime)
    metadata = validation.validate_metadata(CANDIDATES[AWS], raw, record)
    validation.validate_inputs({"with": {"aws-region": "fixture-region-1"}}, metadata)


@pytest.mark.parametrize("change", ["bytes", "hash", "runtime", "unsupported_runtime"])
def test_metadata_tampering_or_unreviewed_runtime_fails(change):
    raw, record = metadata_fixture()
    if change == "bytes":
        raw += b"# unexpected bytes\n"
    elif change == "hash":
        record["sha256"] = "0" * 64
    elif change == "runtime":
        record["runtime"] = "node20"
    else:
        raw, record = metadata_fixture("node99")
    assert_rejected("ACTION_METADATA_INVALID", validation.validate_metadata, CANDIDATES[AWS], raw, record)


def test_unknown_action_input_is_rejected_even_when_metadata_is_authentic():
    raw, record = metadata_fixture()
    metadata = validation.validate_metadata(CANDIDATES[AWS], raw, record)
    assert_rejected("ACTION_INPUT_INVALID", validation.validate_inputs,
                    {"with": {"aws-region": "fixture-region-1", "unknown-input": "fixture"}}, metadata)


def test_required_action_input_cannot_be_omitted():
    raw, record = metadata_fixture()
    metadata = validation.validate_metadata(CANDIDATES[AWS], raw, record)
    assert_rejected("ACTION_INPUT_INVALID", validation.validate_inputs, {"with": {}}, metadata)


def synthetic_archive(kind="regular"):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for _ in range(2 if kind == "duplicate" else 1):
            member = tarfile.TarInfo("../actionlint" if kind == "traversal" else "actionlint")
            if kind == "symlink":
                member.type = tarfile.SYMTYPE
                member.linkname = "elsewhere"
                archive.addfile(member)
            else:
                body = b"non-executable fixture bytes"
                member.size = len(body)
                archive.addfile(member, io.BytesIO(body))
    return output.getvalue()


def test_installer_rejects_wrong_checksum_before_parsing_non_archive_bytes():
    with pytest.raises(installer.InstallError, match="^ACTIONLINT_CHECKSUM_MISMATCH$"):
        installer.verified_binary(b"not a tar archive", "0" * 64)


@pytest.mark.parametrize("kind", ["symlink", "duplicate", "traversal"])
def test_installer_requires_one_regular_binary_at_the_exact_archive_path(kind):
    raw = synthetic_archive(kind)
    with pytest.raises(installer.InstallError, match="^ACTIONLINT_BINARY_MEMBER_INVALID$"):
        installer.verified_binary(raw, hashlib.sha256(raw).hexdigest())


def test_installer_preserves_existing_target_without_executing_any_binary(tmp_path, monkeypatch):
    target = tmp_path / "actionlint"
    original = b"existing user-owned file"
    target.write_bytes(original)
    raw = synthetic_archive()
    monkeypatch.setattr(installer, "platform_key", lambda: "linux_amd64")
    monkeypatch.setattr(installer, "download_archive", lambda _asset: raw)
    monkeypatch.setitem(installer.CHECKSUMS, "linux_amd64", hashlib.sha256(raw).hexdigest())

    def forbidden_execution(*_args, **_kwargs):
        pytest.fail("an existing target must be rejected before binary execution")

    monkeypatch.setattr(installer.subprocess, "run", forbidden_execution)
    with pytest.raises(FileExistsError):
        installer.install(tmp_path)
    assert target.read_bytes() == original


@pytest.mark.parametrize("change", ["push", "branch", "skip_test", "test_failure_ignored", "echo_only", "after_preflight", "overlap"])
def test_deployment_requires_manual_restricted_revision_and_passing_regression(documents, change):
    base, deployment, ci = documents
    job = deployment["jobs"]["deploy-dev"]
    regression = next(step for step in job["steps"] if step.get("id") == "deployment_regression")
    if change == "push":
        deployment["on"]["push"] = {"branches": ["develop"]}
    elif change == "branch":
        job["if"] = "github.event_name == 'workflow_dispatch' && inputs.stage == 'development'"
    elif change == "skip_test":
        regression["if"] = "false"
    elif change == "test_failure_ignored":
        regression["continue-on-error"] = True
    elif change == "echo_only":
        regression["run"] = "echo '" + regression["run"] + "'"
    elif change == "overlap":
        job["concurrency"]["cancel-in-progress"] = True
    else:
        job["steps"].remove(regression)
        job["steps"].append(regression)
    assert_rejected("DEPLOYMENT_GATE_INVALID", validation.validate_workflows, base, deployment, ci)


@pytest.mark.parametrize("job_id", DYNAMODB_JOBS)
def test_current_ci_has_the_offline_smoke_and_dynamodb_local_jobs(documents, job_id):
    base, deployment, ci = documents
    validation.validate_workflows(base, deployment, ci)
    assert tuple(step["id"] for step in integration_job(ci, job_id)["steps"]) == validation.DYNAMODB_JOBS[job_id]
    archive = integration_step(ci, "dynamodb_download", job_id)["env"]
    assert archive == validation.DYNAMODB_ARCHIVE
    assert archive["DYNAMODB_LOCAL_URL"].startswith("https://d1ni2b6xgvw0s0.cloudfront.net/")
    assert "latest" not in archive["DYNAMODB_LOCAL_URL"]
    assert len(archive["DYNAMODB_LOCAL_SHA256"]) == 64 and int(archive["DYNAMODB_LOCAL_SHA256"], 16) >= 0
    suite = integration_step(ci, validation.DYNAMODB_JOBS[job_id][-1], job_id)["run"].split()
    assert suite[-2:] == ["--suite", job_id]


def test_both_dynamodb_local_jobs_share_the_same_setup_commands(documents):
    # Only the checkout/Python step ids and the suite name differ between jobs.
    _, _, ci = documents
    for step_id in validation.INTEGRATION_RUNS:
        first, second = (integration_step(ci, step_id, job_id) for job_id in DYNAMODB_JOBS)
        assert {k: v for k, v in first.items() if k != "name"} == {k: v for k, v in second.items() if k != "name"}
    first, second = (integration_job(ci, job_id)["steps"][:2] for job_id in DYNAMODB_JOBS)
    assert [(s["uses"], s["with"]) for s in first] == [(s["uses"], s["with"]) for s in second]


@pytest.mark.parametrize("job_id", DYNAMODB_JOBS)
def test_integration_job_is_required(documents, job_id):
    base, deployment, ci = documents
    del ci["jobs"][job_id]
    assert_rejected("SMOKE_STRUCTURE_INVALID", validation.validate_workflows, base, deployment, ci)


def test_an_unreviewed_extra_dynamodb_local_job_is_rejected(documents):
    base, deployment, ci = documents
    ci["jobs"]["all"] = deepcopy(integration_job(ci))
    assert_rejected("SMOKE_STRUCTURE_INVALID", validation.validate_workflows, base, deployment, ci)


@pytest.mark.parametrize("job_id,suite", [
    ("integration", "all"),
    ("integration", "boundary"),
    ("boundary", "all"),
    ("boundary", "integration"),
])
def test_each_dynamodb_local_job_runs_only_its_own_fixed_suite(documents, job_id, suite):
    # A job silently running another suite would drop (or duplicate) coverage.
    base, deployment, ci = documents
    step = integration_step(ci, validation.DYNAMODB_JOBS[job_id][-1], job_id)
    step["run"] = step["run"].rsplit("--suite ", 1)[0] + "--suite " + suite
    assert_rejected("SMOKE_STRUCTURE_INVALID", validation.validate_workflows, base, deployment, ci)


def test_dynamodb_local_jobs_cannot_swap_their_step_ids(documents):
    base, deployment, ci = documents
    ci["jobs"]["integration"], ci["jobs"]["boundary"] = ci["jobs"]["boundary"], ci["jobs"]["integration"]
    assert_rejected("SMOKE_STRUCTURE_INVALID", validation.validate_workflows, base, deployment, ci)


@pytest.mark.parametrize("job_id", DYNAMODB_JOBS)
@pytest.mark.parametrize("field,value", [
    ("if", False),
    ("continue-on-error", True),
    ("needs", "validate"),
    ("strategy", {"matrix": {"unused": [1]}}),
    ("container", "unreviewed-fixture-image"),
    ("services", {"db": {"image": "unreviewed-fixture-image"}}),
    ("environment", "unreviewed-fixture-environment"),
    ("env", {"PYTEST_ADDOPTS": "-k nothing"}),
])
def test_integration_job_cannot_be_skipped_ignored_or_rewired(documents, job_id, field, value):
    base, deployment, ci = documents
    integration_job(ci, job_id)[field] = value
    assert_rejected("SMOKE_STRUCTURE_INVALID", validation.validate_workflows, base, deployment, ci)


@pytest.mark.parametrize("job_id", DYNAMODB_JOBS)
def test_one_dynamodb_local_job_cannot_wait_for_the_other(documents, job_id):
    base, deployment, ci = documents
    other = next(name for name in DYNAMODB_JOBS if name != job_id)
    integration_job(ci, job_id)["needs"] = other
    assert_rejected("SMOKE_STRUCTURE_INVALID", validation.validate_workflows, base, deployment, ci)


@pytest.mark.parametrize("job_id", DYNAMODB_JOBS)
@pytest.mark.parametrize("change", ["job_permission", "self_hosted", "timeout", "timeout_zero", "timeout_text"])
def test_integration_job_keeps_the_ci_boundary(documents, job_id, change):
    base, deployment, ci = documents
    job = integration_job(ci, job_id)
    if change == "job_permission":
        job["permissions"] = {"contents": "write"}
    elif change == "self_hosted":
        job["runs-on"] = "self-hosted"
    elif change == "timeout":
        job["timeout-minutes"] = validation.MAX_INTEGRATION_MINUTES + 1
    elif change == "timeout_zero":
        job["timeout-minutes"] = 0
    else:
        job["timeout-minutes"] = str(job["timeout-minutes"])
    assert_rejected("CI_BOUNDARY_INVALID", validation.validate_workflows, base, deployment, ci)


@pytest.mark.parametrize("job_id,step_id", DYNAMODB_STEPS)
@pytest.mark.parametrize("change", ["remove", "skip", "failure_ignored", "replaced"])
def test_every_integration_step_must_still_execute(documents, job_id, step_id, change):
    base, deployment, ci = documents
    steps = integration_job(ci, job_id)["steps"]
    selected = integration_step(ci, step_id, job_id)
    if change == "remove":
        steps.remove(selected)
    elif change == "skip":
        selected["if"] = "false"
    elif change == "failure_ignored":
        selected["continue-on-error"] = True
    elif "run" in selected:
        selected["run"] = "echo '" + selected["run"] + "'"
    else:
        selected["uses"] = selected["uses"].split("@")[0] + "@" + "0" * 40
    assert_rejected("SMOKE_STRUCTURE_INVALID", validation.validate_workflows, base, deployment, ci)


@pytest.mark.parametrize("job_id", DYNAMODB_JOBS)
def test_fingerprint_check_cannot_move_after_the_suites(documents, job_id):
    base, deployment, ci = documents
    steps = integration_job(ci, job_id)["steps"]
    verify = integration_step(ci, "dynamodb_verify", job_id)
    steps.remove(verify)
    steps.append(verify)
    assert_rejected("SMOKE_STRUCTURE_INVALID", validation.validate_workflows, base, deployment, ci)


@pytest.mark.parametrize("job_id", DYNAMODB_JOBS)
@pytest.mark.parametrize("key,value", [
    ("DYNAMODB_LOCAL_URL", "http://d1ni2b6xgvw0s0.cloudfront.net/v2.x/dynamodb_local_2026-07-31.tar.gz"),
    ("DYNAMODB_LOCAL_URL", "https://unreviewed.example.invalid/dynamodb_local_2026-07-31.tar.gz"),
    ("DYNAMODB_LOCAL_URL", "https://d1ni2b6xgvw0s0.cloudfront.net/v2.x/dynamodb_local_latest.tar.gz"),
    ("DYNAMODB_LOCAL_SHA256", "0" * 64),
    ("DYNAMODB_LOCAL_SHA256", ""),
])
def test_dynamodb_local_archive_source_and_digest_are_pinned(documents, job_id, key, value):
    base, deployment, ci = documents
    integration_step(ci, "dynamodb_download", job_id)["env"][key] = value
    assert_rejected("CI_BOUNDARY_INVALID", validation.validate_workflows, base, deployment, ci)


@pytest.mark.parametrize("job_id", DYNAMODB_JOBS)
def test_dynamodb_local_download_keeps_the_pipefail_shell(documents, job_id):
    base, deployment, ci = documents
    del integration_step(ci, "dynamodb_download", job_id)["shell"]
    assert_rejected("SMOKE_STRUCTURE_INVALID", validation.validate_workflows, base, deployment, ci)


@pytest.mark.parametrize("job_id,step_id,env,code", [
    (job_id, step_id, env, code)
    for job_id, suite_step in (("integration", "local_integration"), ("boundary", "local_boundary"))
    for step_id, env, code in (
        (suite_step, {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_ADDOPTS": "-k nothing"}, "CI_BOUNDARY_INVALID"),
        (suite_step, {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "0"}, "CI_BOUNDARY_INVALID"),
        ("dynamodb_verify", {"PYTHONPATH": "unreviewed-fixture-path"}, "SMOKE_STRUCTURE_INVALID"),
    )
])
def test_integration_step_environment_cannot_be_extended(documents, job_id, step_id, env, code):
    base, deployment, ci = documents
    integration_step(ci, step_id, job_id)["env"] = env
    assert_rejected(code, validation.validate_workflows, base, deployment, ci)


@pytest.mark.parametrize("job_id", DYNAMODB_JOBS)
@pytest.mark.parametrize("position,with_", [
    (0, {"persist-credentials": True, "ref": "${{ github.sha }}"}),
    (0, {"persist-credentials": False, "ref": "${{ github.event.pull_request.head.sha }}"}),
    (0, {"ref": "${{ github.sha }}"}),
    (1, {"python-version": "3.13"}),
])
def test_integration_checkout_and_python_inputs_are_fixed(documents, job_id, position, with_):
    base, deployment, ci = documents
    integration_step(ci, validation.DYNAMODB_JOBS[job_id][position], job_id)["with"] = with_
    assert_rejected("SMOKE_STRUCTURE_INVALID", validation.validate_workflows, base, deployment, ci)


@pytest.mark.parametrize("job_id", DYNAMODB_JOBS)
@pytest.mark.parametrize("action", [CHECKOUT, PYTHON])
def test_integration_job_cannot_use_a_ref_outside_the_smoke(documents, job_id, action):
    # A valid official ref used only by a DynamoDB Local job is not smoke coverage.
    base, deployment, ci = documents
    step_id = validation.DYNAMODB_JOBS[job_id][0 if action == CHECKOUT else 1]
    integration_step(ci, step_id, job_id)["uses"] = OLD_REFS[action]
    assert_rejected("SMOKE_STRUCTURE_INVALID", validation.validate_workflows, base, deployment, ci)


@pytest.mark.parametrize("job_id", DYNAMODB_JOBS)
def test_integration_secret_reference_is_rejected(documents, job_id):
    base, deployment, ci = documents
    integration_step(ci, "dynamodb_download", job_id)["env"]["DYNAMODB_LOCAL_URL"] = "${{ secrets.SYNTHETIC_ONLY }}"
    assert_rejected("CI_BOUNDARY_INVALID", validation.validate_workflows, base, deployment, ci)


# --- DynamoDB Local archive digest chain (manifest <-> validator <-> workflow) ---

DISTRIBUTION_MANIFEST = ROOT / "docs/local_server/DYNAMODB_DISTRIBUTION_MANIFEST.json"
# Stated here on purpose, not read from the validator: replacing the archive is
# a reviewed change of the yml, the validator, the manifest and this literal.
PINNED_ARCHIVE_URL = "https://d1ni2b6xgvw0s0.cloudfront.net/v2.x/dynamodb_local_2026-07-31.tar.gz"
PINNED_ARCHIVE_SHA256 = "f80bcec477f85f57e2c77f8d54aa6b672a8403fceff0c450560aee1cf6c21163"


def test_distribution_manifest_pins_the_archive_ci_downloads(documents):
    manifest = json.loads(DISTRIBUTION_MANIFEST.read_text())
    assert list(manifest) == ["version", "source", "archive", "files"]
    assert manifest["version"] == "3.3.1" and len(manifest["files"]) == 127
    assert all(re.fullmatch(r"[0-9a-f]{64}", digest) for digest in manifest["files"].values())
    archive = manifest["archive"]
    assert archive == {"url": PINNED_ARCHIVE_URL, "sha256": PINNED_ARCHIVE_SHA256}
    assert validation.DYNAMODB_ARCHIVE == {"DYNAMODB_LOCAL_URL": PINNED_ARCHIVE_URL,
                                           "DYNAMODB_LOCAL_SHA256": PINNED_ARCHIVE_SHA256}
    _, _, ci = documents
    for job_id in DYNAMODB_JOBS:
        assert integration_step(ci, "dynamodb_download", job_id)["env"] == {
            "DYNAMODB_LOCAL_URL": PINNED_ARCHIVE_URL, "DYNAMODB_LOCAL_SHA256": PINNED_ARCHIVE_SHA256}
    validation.validate_distribution_manifest(ROOT)


def manifest_copy(tmp_path, mutate):
    """A repository-shaped copy of the manifest with one change; the checked-in file is never written."""
    manifest = json.loads(DISTRIBUTION_MANIFEST.read_text())
    mutate(manifest)
    target = tmp_path / "docs/local_server/DYNAMODB_DISTRIBUTION_MANIFEST.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return target


def set_archive(key, value):
    def mutate(manifest):
        manifest["archive"][key] = value
    return mutate


@pytest.mark.parametrize("change,code", [
    (set_archive("sha256", "0" * 64), "DISTRIBUTION_ARCHIVE_MISMATCH"),
    (set_archive("sha256", PINNED_ARCHIVE_SHA256.upper()), "DISTRIBUTION_ARCHIVE_MISMATCH"),
    (set_archive("url", PINNED_ARCHIVE_URL.replace("2026-07-31", "latest")), "DISTRIBUTION_ARCHIVE_MISMATCH"),
    (set_archive("url", PINNED_ARCHIVE_URL.replace("https://", "http://")), "DISTRIBUTION_ARCHIVE_MISMATCH"),
    (set_archive("mirror", "https://unreviewed.example.invalid/dynamodb_local.tar.gz"), "DISTRIBUTION_MANIFEST_INVALID"),
    (lambda manifest: manifest.pop("archive"), "DISTRIBUTION_MANIFEST_INVALID"),
    (lambda manifest: manifest.__setitem__("archive", PINNED_ARCHIVE_URL), "DISTRIBUTION_MANIFEST_INVALID"),
    (lambda manifest: manifest.__setitem__("files", {}), "DISTRIBUTION_MANIFEST_INVALID"),
], ids=["digest", "digest_case", "url_latest", "url_http", "extra_key", "missing", "not_object", "no_files"])
def test_distribution_manifest_archive_must_match_the_validator(tmp_path, change, code):
    manifest_copy(tmp_path, change)
    assert_rejected(code, validation.validate_distribution_manifest, tmp_path)


def test_distribution_manifest_copy_without_changes_is_accepted(tmp_path):
    manifest_copy(tmp_path, lambda manifest: None)
    validation.validate_distribution_manifest(tmp_path)


def offline_check_arguments(monkeypatch, tmp_path):
    """`check` against the repository with git and action metadata replaced by offline stand-ins."""
    deployment_text = (ROOT / validation.DEPLOYMENT).read_text()

    def git_text(_root, *args):
        return deployment_text if args[0] == "show" else "c" * 40

    ci = validation.load_yaml((ROOT / validation.VALIDATION).read_text())
    deployment = validation.load_yaml(deployment_text)
    inputs = {name: {} for workflow in (ci, deployment) for job in workflow["jobs"].values()
              for step in job["steps"] for name in step.get("with", {})}

    def read_metadata(_ref, record, _directory, _offline):
        return {"inputs": inputs, "runs": {"using": record["runtime"]}}

    monkeypatch.setattr(validation, "git_text", git_text)
    monkeypatch.setattr(validation, "read_metadata", read_metadata)
    return argparse.Namespace(root=str(ROOT), base_sha="b" * 40, metadata_dir=str(tmp_path / "metadata"), offline=True)


def test_check_reports_the_distribution_manifest_in_its_source_hashes(monkeypatch, tmp_path, capsys):
    args = offline_check_arguments(monkeypatch, tmp_path)
    validation.check(args)
    report = json.loads(capsys.readouterr().out)
    assert report["checks"] == "static_contracts_and_syntax_passed" and report["aws_action_executed"] is False
    assert report["source_hashes"]["docs/local_server/DYNAMODB_DISTRIBUTION_MANIFEST.json"] == \
        hashlib.sha256(DISTRIBUTION_MANIFEST.read_bytes()).hexdigest()


def test_check_fails_when_the_manifest_archive_differs_from_the_workflow(monkeypatch, tmp_path):
    args = offline_check_arguments(monkeypatch, tmp_path)
    changed = manifest_copy(tmp_path, set_archive("sha256", "0" * 64))
    # An absolute path joined onto the root selects the changed copy, not the checked-in file.
    monkeypatch.setattr(validation, "DISTRIBUTION_MANIFEST", str(changed))
    assert_rejected("DISTRIBUTION_ARCHIVE_MISMATCH", validation.check, args)


# --- Per-job derivation of step ids, suite commands, timeouts and the smoke ref count ---

def test_dynamodb_job_step_ids_and_suite_commands_are_derived_per_job():
    assert validation.DYNAMODB_JOBS == {
        "integration": ("integration_checkout", "integration_python", "local_dependencies", "jdk",
                        "dynamodb_download", "dynamodb_verify", "local_integration"),
        "boundary": ("boundary_checkout", "boundary_python", "local_dependencies", "jdk",
                     "dynamodb_download", "dynamodb_verify", "local_boundary"),
    }
    assert validation.SUITE_RUNS == {
        "local_integration": (
            '"$RUNNER_TEMP/integration-venv/bin/python" scripts/validate_local_integration.py \\\n'
            '  --dynamodb-home "$RUNNER_TEMP/dynamodb-local" --suite integration',
            {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}, None),
        "local_boundary": (
            '"$RUNNER_TEMP/integration-venv/bin/python" scripts/validate_local_integration.py \\\n'
            '  --dynamodb-home "$RUNNER_TEMP/dynamodb-local" --suite boundary',
            {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}, None),
    }
    assert set(validation.INTEGRATION_RUNS) == {"local_dependencies", "jdk", "dynamodb_download", "dynamodb_verify"}


@pytest.mark.parametrize("job_id,limit", [("validate", 12), ("integration", 13), ("boundary", 12)])
def test_job_timeouts_are_bounded_by_their_measured_budget(documents, job_id, limit):
    # 2026-09-28 measurement x3 + 3 min, rounded up; the yml may not exceed it.
    base, deployment, ci = documents
    job = ci["jobs"][job_id]
    assert 1 <= job["timeout-minutes"] <= limit
    job["timeout-minutes"] = limit
    validation.validate_workflows(base, deployment, ci)
    job["timeout-minutes"] = limit + 1
    assert_rejected("CI_BOUNDARY_INVALID", validation.validate_workflows, base, deployment, ci)
    assert validation.MAX_VALIDATE_MINUTES == 12 and validation.MAX_INTEGRATION_MINUTES == 13


def smoke_arguments(monkeypatch, tmp_path, workflow_text):
    """Run `smoke` in a temporary checkout copy without git, a runner or a second interpreter."""
    monkeypatch.chdir(tmp_path)
    target = tmp_path / validation.VALIDATION
    target.parent.mkdir(parents=True)
    target.write_text(workflow_text, encoding="utf-8")
    monkeypatch.setattr(validation, "git_text", lambda _root, *args: "a" * 40)
    monkeypatch.setattr(sys, "version_info", (3, 12, 0, "final", 0))
    monkeypatch.setattr(validation.shutil, "which", lambda _name: sys.executable)
    for name in ("GITHUB_EVENT_PATH", "GITHUB_RUN_ID", "GITHUB_REPOSITORY"):
        monkeypatch.delenv(name, raising=False)
    return argparse.Namespace(expected_sha="a" * 40, python_path=sys.executable)


def test_smoke_counts_two_refs_for_the_smoke_job_and_each_dynamodb_local_job(monkeypatch, tmp_path, capsys):
    args = smoke_arguments(monkeypatch, tmp_path, (ROOT / validation.VALIDATION).read_text())
    validation.smoke(args)
    evidence = json.loads(capsys.readouterr().out)
    assert len(evidence["action_refs"]) == 6 and evidence["aws_action_executed"] is False
    assert {ref.split("@", 1)[0] for ref in evidence["action_refs"]} == {CHECKOUT, PYTHON}


@pytest.mark.parametrize("change", ["extra_job", "missing_step", "extra_ref"])
def test_smoke_rejects_a_job_set_that_differs_from_the_validator(monkeypatch, tmp_path, change):
    text = (ROOT / validation.VALIDATION).read_text()
    uses = re.findall(r"^\s*uses:\s*(\S+)", text, re.M)
    checkout, python = uses[0], uses[1]
    # Same layout as the yml: `- name:` first, `uses:` on its own line.
    if change == "extra_job":
        text += ("\n  extra:\n    runs-on: ubuntu-latest\n    steps:\n"
                 f"      - name: Extra checkout\n        uses: {checkout}\n"
                 f"      - name: Extra Python\n        uses: {python}\n")
    elif change == "missing_step":
        text = text[:text.rindex("uses: " + python)] + "run: echo replaced\n"
    else:
        text += ("\n  extra:\n    runs-on: ubuntu-latest\n    steps:\n"
                 f"      - name: Extra checkout\n        uses: {OLD_REFS[CHECKOUT]}\n")
    assert len(re.findall(r"^\s*uses:\s*(\S+)", text, re.M)) in (5, 7, 8), "the fixture must change the ref count"
    args = smoke_arguments(monkeypatch, tmp_path, text)
    assert_rejected("SMOKE_STRUCTURE_INVALID", validation.smoke, args)

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
UPLOAD = "actions/upload-artifact"
DOWNLOAD = "actions/download-artifact"
# Frozen baseline fixtures (the refs an older base revision used), independent
# of which PR source runs the unit tests.
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
    """(base, deployment, ci): the base is the deployment workflow with the older action refs."""
    deployment = validation.load_yaml((ROOT / validation.DEPLOYMENT).read_text())
    base = deepcopy(deployment)
    for action, ref in OLD_REFS.items():
        replace_action(base, action, ref)
    ci = validation.load_yaml((ROOT / validation.VALIDATION).read_text())
    return base, deepcopy(deployment), deepcopy(ci)


def deploy_job(deployment):
    assert set(deployment["jobs"]) == {"deploy-dev"}
    return deployment["jobs"]["deploy-dev"]


def deploy_step(deployment, step_id):
    return next(step for step in deploy_job(deployment)["steps"] if step.get("id") == step_id)


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
    for action in (CHECKOUT, PYTHON, AWS):
        replace_action(deployment, action, CANDIDATES[action] if action in changed else OLD_REFS[action])
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


@pytest.mark.parametrize("change", ["role", "session", "region", "extra_input", "if", "env", "continue_on_error"])
def test_the_single_oidc_credentials_step_is_fixed(documents, change):
    base, deployment, ci = documents
    oidc = deploy_step(deployment, "credentials")
    if change == "role":
        oidc["with"]["role-to-assume"] = "arbitrary-fixture-role"
    elif change == "session":
        oidc["with"]["role-session-name"] = "arbitrary-fixture-session"
    elif change == "region":
        oidc["with"]["aws-region"] = "arbitrary-fixture-region"
    elif change == "extra_input":
        oidc["with"]["role-duration-seconds"] = 3600
    elif change == "if":
        oidc["if"] = "env.AWS_ROLE_ARN != ''"
    elif change == "env":
        oidc["env"] = {"ROLE_CHAINING": "true"}
    else:
        oidc["continue-on-error"] = True
    # Without a base the step rules themselves reject it; with the base, the
    # comparison of the OIDC step rejects it first (test_base_comparison_...).
    assert_rejected("DEPLOYMENT_GATE_INVALID" if change in ("if", "continue_on_error") else "DEPLOYMENT_AUTH_INVALID",
                    validation.validate_workflows, None, deployment, ci)
    assert_rejected("DEPLOYMENT_AUTH_INVALID", validation.validate_workflows, base, deployment, ci)


@pytest.mark.parametrize("placement", ["credentials_with", "job_name", "step_name", "job_env", "second_step"])
def test_an_access_key_path_is_rejected_wherever_it_appears(documents, placement):
    base, deployment, ci = documents
    job = deploy_job(deployment)
    if placement == "credentials_with":
        deploy_step(deployment, "credentials")["with"] = {
            "aws-access-key-id": "${{ secrets.DEV_AWS_ACCESS_KEY_ID }}",
            "aws-secret-access-key": "${{ secrets.DEV_AWS_SECRET_ACCESS_KEY }}", "aws-region": "${{ env.AWS_REGION }}"}
    elif placement == "job_name":
        job["name"] = "deploy with aws-access-key-id"
    elif placement == "step_name":
        deploy_step(deployment, "deploy")["name"] = "uses AWS_SECRET_ACCESS_KEY"
    elif placement == "job_env":
        job["env"]["AWS_ACCESS_KEY_ID"] = "${{ secrets.DEV_AWS_ACCESS_KEY_ID }}"
    else:
        fallback = deepcopy(deploy_step(deployment, "credentials"))
        fallback.update(id="credentials_keys", **{"if": "env.AWS_ROLE_ARN == ''"})
        fallback["with"] = {"aws-access-key-id": "${{ secrets.DEV_AWS_ACCESS_KEY_ID }}",
                            "aws-secret-access-key": "${{ secrets.DEV_AWS_SECRET_ACCESS_KEY }}",
                            "aws-region": "${{ env.AWS_REGION }}"}
        job["steps"].insert(job["steps"].index(deploy_step(deployment, "credentials")) + 1, fallback)
    assert_rejected("DEPLOYMENT_AUTH_INVALID", validation.validate_workflows, base, deployment, ci)
    assert_rejected("DEPLOYMENT_AUTH_INVALID" if placement != "second_step" else "DEPLOYMENT_GATE_INVALID",
                    validation.validate_workflows, None, deployment, ci)


@pytest.mark.parametrize("reference", ["${{ secrets.SYNTHETIC_ONLY }}", "${{ secrets['DEV_AWS_ROLE_ARN'] }}",
                                       "${{ vars.SYNTHETIC_ONLY }}", "${{ vars['DEV_API_BASE_URL'] }}"])
def test_only_the_reviewed_secret_and_variable_may_be_referenced(documents, reference):
    base, deployment, ci = documents
    deploy_step(deployment, "smoke")["name"] = "smoke " + reference
    assert_rejected("DEPLOYMENT_AUTH_INVALID", validation.validate_workflows, base, deployment, ci)


@pytest.mark.parametrize("change", ["permissions", "environment", "self_hosted", "container", "needs", "if",
                                    "concurrency", "strategy", "second_job"])
def test_deployment_job_keeps_its_effective_identity_and_execution_context(documents, change):
    base, deployment, ci = documents
    job = deploy_job(deployment)
    if change == "permissions":
        job["permissions"] = {"contents": "read"}
    elif change == "environment":
        job["environment"] = "unreviewed-fixture-environment"
    elif change == "self_hosted":
        job["runs-on"] = "self-hosted"
    elif change == "container":
        job["container"] = "unreviewed-fixture-image"
    elif change == "needs":
        job["needs"] = "a_skipped_job"
    elif change == "if":
        job["if"] = "github.actor == 'fixture'"
    elif change == "concurrency":
        job["concurrency"] = {"group": "deploy-development", "cancel-in-progress": False}
    elif change == "strategy":
        job["strategy"] = {"matrix": {"unused": [1]}}
    else:
        deployment["jobs"]["deploy-prod"] = deepcopy(job)
    assert_rejected("DEPLOYMENT_AUTH_INVALID", validation.validate_workflows, base, deployment, ci)


@pytest.mark.parametrize("change", ["region", "function_name", "extra_key", "missing_key", "role_secret", "url_secret"])
def test_deployment_job_env_bindings_are_fixed(documents, change):
    base, deployment, ci = documents
    env = deploy_job(deployment)["env"]
    if change == "region":
        env["AWS_REGION"] = "us-east-1"
    elif change == "function_name":
        env["ARC_DEV_FUNCTIONS_API"] = "arc-calc-prod-api"
    elif change == "extra_key":
        env["ROLE_CHAINING"] = "true"
    elif change == "missing_key":
        del env["DEV_API_BASE_URL"]
    elif change == "role_secret":
        env["AWS_ROLE_ARN"] = "${{ secrets.PROD_AWS_ROLE_ARN }}"
    else:
        env["DEV_API_BASE_URL"] = "https://fixture.example.invalid/dev"
    assert_rejected("DEPLOYMENT_AUTH_INVALID", validation.validate_workflows, base, deployment, ci)


@pytest.mark.parametrize("scope", ["workflow", "job", "oidc"])
def test_environment_alias_cannot_silently_change_aws_action_inputs(documents, scope):
    base, deployment, ci = documents
    if scope == "workflow":
        target = deployment
    elif scope == "job":
        target = deploy_job(deployment)
    else:
        target = deploy_step(deployment, "credentials")
    target.setdefault("env", {})["ROLE_CHAINING"] = "true"
    assert_rejected("DEPLOYMENT_AUTH_INVALID", validation.validate_workflows, base, deployment, ci)


@pytest.mark.parametrize("change,code", [
    ("echo_only", "DEPLOYMENT_AUTH_INVALID"), ("worker_only", "DEPLOYMENT_AUTH_INVALID"),
    ("registry", "DEPLOYMENT_AUTH_INVALID"),
    ("if", "DEPLOYMENT_GATE_INVALID"), ("continue_on_error", "DEPLOYMENT_GATE_INVALID"),
    ("env", "DEPLOYMENT_AUTH_INVALID"), ("after_deploy", "DEPLOYMENT_GATE_INVALID"), ("removed", "DEPLOYMENT_GATE_INVALID"),
])
def test_check_config_must_run_unconditionally_with_its_command_before_the_deploy(documents, change, code):
    base, deployment, ci = documents
    steps = deploy_job(deployment)["steps"]
    check = deploy_step(deployment, "check_config")
    if change == "echo_only":
        check["run"] = "echo '" + check["run"] + "'"
    elif change == "worker_only":
        check["run"] = check["run"].replace(' --function api="$ARC_DEV_FUNCTIONS_API"', "")
    elif change == "registry":
        # The D140 body: a configuration registry argument is no longer accepted (D141).
        check["run"] = validation.CHECK_CONFIG_PREDECESSOR_RUNS[0] + "\n"
    elif change == "if":
        check["if"] = "false"
    elif change == "continue_on_error":
        check["continue-on-error"] = True
    elif change == "env":
        check["env"] = {"AWS_REGION": "us-east-1"}
    elif change == "after_deploy":
        steps.remove(check)
        steps.insert(steps.index(deploy_step(deployment, "deploy")) + 1, check)
    else:
        steps.remove(check)
    assert_rejected(code, validation.validate_workflows, None, deployment, ci)
    # With the base, an unchanged step body compares equal and the order rule fires instead.
    assert_rejected("DEPLOYMENT_GATE_INVALID" if change == "after_deploy" else "DEPLOYMENT_AUTH_INVALID",
                    validation.validate_workflows, base, deployment, ci)


REGISTRY_ARGUMENT = ' \\\n  --registry "$RUNNER_TEMP/artifact/execution-registry.json"'
REGISTRY_LINE = ('\n"$RUNNER_TEMP/actions-venv/bin/python" scripts/deploy_dev_lambdas.py registry \\\n'
                 '  --output "$RUNNER_TEMP/artifact/execution-registry.json"')


@pytest.mark.parametrize("step_id,change", [("deploy", "argument"), ("check_config", "argument"), ("build", "line"),
                                            ("upload", "path")])
def test_the_deployment_carries_no_configuration_registry(documents, monkeypatch, step_id, change):
    # D141: the D140 registry file and `--registry` arguments are gone; even when the
    # yml and the pinned body are changed together they are rejected.
    base, deployment, ci = documents
    step = deploy_step(deployment, step_id)
    if change == "path":
        step["with"]["path"] += "${{ runner.temp }}/artifact/execution-registry.json\n"
        monkeypatch.setitem(validation.DEPLOY_UPLOAD_WITH, "path", step["with"]["path"])
    else:
        step["run"] = step["run"].rstrip("\n") + (REGISTRY_ARGUMENT if change == "argument" else REGISTRY_LINE) + "\n"
        body, env, shell = validation.DEPLOY_RUNS[step_id]
        monkeypatch.setitem(validation.DEPLOY_RUNS, step_id, (step["run"].strip(), env, shell))
        if step_id == "check_config":
            monkeypatch.setattr(validation, "CHECK_CONFIG_PREDECESSOR_RUNS", ())
    assert_rejected("DEPLOYMENT_GATE_INVALID", validation.validate_workflows, None, deployment, ci)


@pytest.mark.parametrize("step_id", ["deploy", "smoke"])
@pytest.mark.parametrize("change", ["echo_only", "if", "continue_on_error", "removed", "env"])
def test_deploy_and_smoke_must_run_unconditionally_with_their_commands(documents, step_id, change):
    base, deployment, ci = documents
    steps = deploy_job(deployment)["steps"]
    step = deploy_step(deployment, step_id)
    if change == "echo_only":
        step["run"] = "echo '" + step["run"] + "'"
    elif change == "if":
        step["if"] = "success()"
    elif change == "continue_on_error":
        step["continue-on-error"] = True
    elif change == "env":
        step["env"] = {"DEV_API_BASE_URL": "https://fixture.example.invalid/dev"}
    else:
        steps.remove(step)
    assert_rejected("DEPLOYMENT_GATE_INVALID", validation.validate_workflows, base, deployment, ci)


@pytest.mark.parametrize("order", [("api", "worker", "relay"), ("worker", "api", "relay"), ("relay", "worker", "api"),
                                   ("worker", "relay")])
def test_deploy_order_is_worker_relay_api(documents, order):
    base, deployment, ci = documents
    step = deploy_step(deployment, "deploy")
    names = {"worker": "$ARC_DEV_FUNCTIONS_WORKER", "relay": "$ARC_DEV_FUNCTIONS_RELAY", "api": "$ARC_DEV_FUNCTIONS_API"}
    fixed = " ".join(f'--function {role}="{names[role]}"' for role in ("worker", "relay", "api"))
    changed = " ".join(f'--function {role}="{names[role]}"' for role in order)
    assert fixed in step["run"]
    step["run"] = step["run"].replace(fixed, changed)
    assert_rejected("DEPLOYMENT_GATE_INVALID", validation.validate_workflows, base, deployment, ci)
    assert validation.DEPLOY_FUNCTION_ORDER == ("worker", "relay", "api")


@pytest.mark.parametrize("change", ["smoke_after_deploy_missing", "swap_deploy_smoke", "swap_check_deploy"])
def test_check_config_deploy_smoke_order_is_fixed(documents, change):
    base, deployment, ci = documents
    steps = deploy_job(deployment)["steps"]
    first, second = {"smoke_after_deploy_missing": ("deploy", None), "swap_deploy_smoke": ("deploy", "smoke"),
                     "swap_check_deploy": ("check_config", "deploy")}[change]
    if second is None:
        steps.remove(deploy_step(deployment, first))
    else:
        i, j = steps.index(deploy_step(deployment, first)), steps.index(deploy_step(deployment, second))
        steps[i], steps[j] = steps[j], steps[i]
    assert_rejected("DEPLOYMENT_GATE_INVALID", validation.validate_workflows, base, deployment, ci)


@pytest.mark.parametrize("step_id,change", [
    ("build", "rollback_condition"), ("build", "no_condition"), ("build", "echo_only"), ("build", "arm64"),
    ("upload", "rollback_condition"), ("upload", "no_condition"), ("upload", "retention"), ("upload", "name"),
    ("upload", "missing_manifest"), ("upload", "warn_on_missing"),
    ("download", "build_condition"), ("download", "no_condition"), ("download", "fixed_run_id"),
    ("download", "other_repository"), ("download", "no_merge"), ("download", "name_pattern"),
])
def test_artifact_steps_keep_their_conditions_and_inputs(documents, step_id, change):
    base, deployment, ci = documents
    step = deploy_step(deployment, step_id)
    if change == "rollback_condition":
        step["if"] = "inputs.rollback_run_id != ''"
    elif change == "build_condition":
        step["if"] = "inputs.rollback_run_id == ''"
    elif change == "no_condition":
        del step["if"]
    elif change == "echo_only":
        step["run"] = "echo '" + step["run"] + "'"
    elif change == "arm64":
        step["run"] = step["run"].replace("manylinux2014_x86_64", "manylinux2014_aarch64")
    elif change == "retention":
        step["with"]["retention-days"] = 90
    elif change == "name":
        step["with"]["name"] = "mock-lambda"
    elif change == "missing_manifest":
        step["with"]["path"] = "${{ runner.temp }}/artifact/mock-lambda.zip\n"
    elif change == "warn_on_missing":
        step["with"]["if-no-files-found"] = "warn"
    elif change == "fixed_run_id":
        step["with"]["run-id"] = "1"
    elif change == "other_repository":
        step["with"]["repository"] = "fixture/other"
    elif change == "no_merge":
        del step["with"]["merge-multiple"]
    else:
        del step["with"]["pattern"]
        step["with"]["name"] = "mock-lambda-${{ github.sha }}"
    assert_rejected("DEPLOYMENT_GATE_INVALID", validation.validate_workflows, base, deployment, ci)


def test_download_token_must_be_the_run_token_not_a_secret(documents):
    base, deployment, ci = documents
    deploy_step(deployment, "download")["with"]["github-token"] = "${{ secrets.ARTIFACT_PAT }}"
    assert_rejected("DEPLOYMENT_GATE_INVALID", validation.validate_workflows, base, deployment, ci)


@pytest.mark.parametrize("change", ["no_if", "success_only", "echo_only"])
def test_summary_always_runs_its_fixed_command(documents, change):
    base, deployment, ci = documents
    step = deploy_step(deployment, "summary")
    if change == "no_if":
        del step["if"]
    elif change == "success_only":
        step["if"] = "success()"
    else:
        step["run"] = "echo '" + step["run"] + "'"
    assert_rejected("DEPLOYMENT_GATE_INVALID", validation.validate_workflows, base, deployment, ci)


@pytest.mark.parametrize("change", ["persist", "head_ref", "no_ref", "python_313", "extra_step", "swapped"])
def test_deployment_checkout_and_python_match_the_ci_smoke(documents, change):
    base, deployment, ci = documents
    steps = deploy_job(deployment)["steps"]
    if change == "persist":
        deploy_step(deployment, "checkout")["with"]["persist-credentials"] = True
    elif change == "head_ref":
        deploy_step(deployment, "checkout")["with"]["ref"] = "${{ github.event.pull_request.head.sha }}"
    elif change == "no_ref":
        del deploy_step(deployment, "checkout")["with"]["ref"]
    elif change == "python_313":
        deploy_step(deployment, "python")["with"]["python-version"] = "3.13"
    elif change == "extra_step":
        steps.insert(2, {"id": "extra", "run": "echo extra"})
    else:
        steps[0], steps[1] = steps[1], steps[0]
    assert_rejected("DEPLOYMENT_GATE_INVALID", validation.validate_workflows, base, deployment, ci)


def test_deployment_timeout_is_bounded_by_the_measured_budget(documents):
    base, deployment, ci = documents
    job = deploy_job(deployment)
    assert 1 <= job["timeout-minutes"] <= validation.MAX_DEPLOY_MINUTES == 15
    job["timeout-minutes"] = validation.MAX_DEPLOY_MINUTES
    validation.validate_workflows(base, deployment, ci)
    for value in (validation.MAX_DEPLOY_MINUTES + 1, 0, str(validation.MAX_DEPLOY_MINUTES)):
        job["timeout-minutes"] = value
        assert_rejected("DEPLOYMENT_GATE_INVALID", validation.validate_workflows, base, deployment, ci)


# --- PR base comparison (env, OIDC step, check-config step) ---

def test_introduction_pr_without_a_base_deployment_is_accepted_and_reported(documents):
    _, deployment, ci = documents
    result = validation.validate_workflows(None, deployment, ci)
    assert result["base_deployment"] == "absent"
    introduced = {ref for refs in result["introduced"].values() for ref in refs}
    assert introduced == {step["uses"] for job in deployment["jobs"].values() for step in job["steps"] if "uses" in step}
    assert {ref.split("@", 1)[0] for ref in introduced} == {CHECKOUT, PYTHON, AWS, UPLOAD, DOWNLOAD}


def test_introduction_pr_still_requires_the_ci_smoke_refs(documents):
    _, deployment, ci = documents
    replace_action(deployment, CHECKOUT, OLD_REFS[CHECKOUT])
    assert_rejected("SMOKE_REF_UNCOVERED", validation.validate_workflows, None, deployment, ci)


def test_unchanged_deployment_against_its_base_is_compared(documents):
    base, deployment, ci = documents
    assert validation.validate_workflows(base, deployment, ci)["base_deployment"] == "compared"


@pytest.mark.parametrize("change", ["job_env", "oidc_env", "oidc_with", "check_config_run", "check_config_env",
                                    "workflow_env", "missing_job"])
def test_base_comparison_rejects_a_pr_that_rewires_env_oidc_or_check_config(documents, change):
    # The current file alone passes validate_deployment; only the base differs.
    base, deployment, ci = documents
    if change == "job_env":
        deploy_job(base)["env"]["AWS_ROLE_ARN"] = "${{ secrets.OTHER_ROLE_ARN }}"
    elif change == "oidc_env":
        deploy_step(base, "credentials")["env"] = {"ROLE_CHAINING": "true"}
    elif change == "oidc_with":
        deploy_step(base, "credentials")["with"]["role-session-name"] = "arbitrary-fixture-session"
    elif change == "check_config_run":
        deploy_step(base, "check_config")["run"] = "echo skipped"
    elif change == "check_config_env":
        deploy_step(base, "check_config")["env"] = {"AWS_REGION": "us-east-1"}
    elif change == "workflow_env":
        base["env"] = {"ROLE_CHAINING": "true"}
    else:
        base["jobs"] = {}
    assert_rejected("DEPLOYMENT_AUTH_INVALID", validation.validate_workflows, base, deployment, ci)


def d140_base(base):
    """The base as it is at the revision before D141: check-config with `--registry`."""
    step = deploy_step(base, "check_config")
    assert step["run"].strip() == validation.DEPLOY_RUNS["check_config"][0]
    step["run"] = validation.CHECK_CONFIG_PREDECESSOR_RUNS[0] + "\n"
    return base


def test_the_d140_check_config_body_is_the_only_reviewed_predecessor():
    current = validation.DEPLOY_RUNS["check_config"][0]
    assert current == (
        '"$RUNNER_TEMP/actions-venv/bin/python" scripts/deploy_dev_lambdas.py check-config \\\n'
        '  --region "$AWS_REGION" --function api="$ARC_DEV_FUNCTIONS_API" --function worker="$ARC_DEV_FUNCTIONS_WORKER"')
    assert validation.CHECK_CONFIG_PREDECESSOR_RUNS == (
        current + ' \\\n  --registry "$RUNNER_TEMP/artifact/execution-registry.json"',)
    assert current not in validation.CHECK_CONFIG_PREDECESSOR_RUNS


def test_base_comparison_accepts_the_reviewed_check_config_change_of_d141(documents):
    # The PR that removes `--registry`: the base still has the D140 body, the head has the fixed D141 body.
    base, deployment, ci = documents
    assert validation.validate_workflows(d140_base(base), deployment, ci)["base_deployment"] == "compared"


@pytest.mark.parametrize("change", ["head_other_body", "head_env", "head_shell", "head_keeps_predecessor",
                                    "base_other_body", "base_env", "base_if", "base_two_steps", "reverse"])
def test_base_comparison_accepts_no_other_check_config_change(documents, change):
    base, deployment, ci = documents
    d140_base(base)
    old, new = deploy_step(base, "check_config"), deploy_step(deployment, "check_config")
    if change == "head_other_body":
        new["run"] = new["run"].replace(' --function api="$ARC_DEV_FUNCTIONS_API"', "")
    elif change == "head_env":
        new["env"] = {"AWS_REGION": "us-east-1"}
    elif change == "head_shell":
        new["shell"] = "bash"
    elif change == "head_keeps_predecessor":
        # Not a base difference at all, so the fixed-body rule of the current file decides.
        new["run"] = old["run"]
    elif change == "base_other_body":
        old["run"] = "echo skipped"
    elif change == "base_env":
        old["env"] = {"AWS_REGION": "us-east-1"}
    elif change == "base_if":
        old["if"] = "false"
    elif change == "base_two_steps":
        deploy_job(base)["steps"].append(deepcopy(old))
    else:
        # The predecessor (D140, with `--registry`) is not a body the current file may go back to.
        old["run"], new["run"] = new["run"], old["run"]
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


@pytest.mark.parametrize("change", [
    "pull_request", "main_branch", "two_branches", "no_dispatch", "input_type", "input_required", "input_default",
    "extra_input", "skip_test", "test_failure_ignored", "echo_only", "regression_env", "after_credentials",
    "secret_before_regression", "variable_before_regression", "overlap", "other_group", "no_concurrency",
])
def test_deployment_requires_the_develop_push_and_the_passing_regression_before_any_secret(documents, change):
    base, deployment, ci = documents
    job = deploy_job(deployment)
    regression = deploy_step(deployment, "regression")
    if change == "pull_request":
        deployment["on"]["pull_request"] = {"branches": ["develop"]}
    elif change == "main_branch":
        deployment["on"]["push"] = {"branches": ["main"]}
    elif change == "two_branches":
        deployment["on"]["push"] = {"branches": ["develop", "main"]}
    elif change == "no_dispatch":
        del deployment["on"]["workflow_dispatch"]
    elif change == "input_type":
        deployment["on"]["workflow_dispatch"]["inputs"]["rollback_run_id"]["type"] = "number"
    elif change == "input_required":
        deployment["on"]["workflow_dispatch"]["inputs"]["rollback_run_id"]["required"] = True
    elif change == "input_default":
        deployment["on"]["workflow_dispatch"]["inputs"]["rollback_run_id"]["default"] = "1"
    elif change == "extra_input":
        deployment["on"]["workflow_dispatch"]["inputs"]["skip_tests"] = {"type": "boolean", "default": False}
    elif change == "skip_test":
        regression["if"] = "false"
    elif change == "test_failure_ignored":
        regression["continue-on-error"] = True
    elif change == "echo_only":
        regression["run"] = "echo '" + regression["run"] + "'"
    elif change == "regression_env":
        regression["env"]["PYTEST_ADDOPTS"] = "-k nothing"
    elif change == "after_credentials":
        steps = job["steps"]
        steps.remove(regression)
        steps.insert(steps.index(deploy_step(deployment, "credentials")) + 1, regression)
    elif change == "secret_before_regression":
        deploy_step(deployment, "dependencies")["name"] = "install ${{ secrets.DEV_AWS_ROLE_ARN }}"
    elif change == "variable_before_regression":
        deploy_step(deployment, "checkout")["name"] = "checkout ${{ vars.DEV_API_BASE_URL }}"
    elif change == "overlap":
        deployment["concurrency"]["cancel-in-progress"] = True
    elif change == "other_group":
        deployment["concurrency"]["group"] = "deploy-${{ github.ref }}"
    else:
        del deployment["concurrency"]
    assert_rejected("DEPLOYMENT_GATE_INVALID", validation.validate_workflows, base, deployment, ci)


@pytest.mark.parametrize("change", ["contents_write", "no_actions_read", "extra_scope"])
def test_deployment_permissions_are_minimal(documents, change):
    base, deployment, ci = documents
    if change == "contents_write":
        deployment["permissions"]["contents"] = "write"
    elif change == "no_actions_read":
        del deployment["permissions"]["actions"]
    else:
        deployment["permissions"]["deployments"] = "write"
    assert_rejected("DEPLOYMENT_AUTH_INVALID", validation.validate_workflows, base, deployment, ci)


def test_current_deployment_workflow_matches_the_validator_constants(documents):
    _, deployment, _ = documents
    assert deployment["on"]["push"] == validation.DEPLOY_TRIGGERS["push"]
    assert deployment["permissions"] == validation.DEPLOY_PERMISSIONS
    assert deployment["concurrency"] == validation.DEPLOY_CONCURRENCY
    job = deploy_job(deployment)
    assert job["environment"] == "development" and job["env"] == validation.DEPLOY_ENV
    assert tuple(step["id"] for step in job["steps"]) == validation.DEPLOY_STEP_IDS
    assert deploy_step(deployment, "credentials")["with"] == validation.DEPLOY_OIDC_WITH
    assert deploy_step(deployment, "regression")["run"] == validation.CRITICAL_RUNS["regression"]
    assert deploy_step(deployment, "dependencies")["run"].strip() == validation.DEPENDENCIES_RUN
    ci_dependencies = next(step for step in validation.load_yaml((ROOT / validation.VALIDATION).read_text())
                           ["jobs"]["validate"]["steps"] if step.get("id") == "dependencies")
    assert ci_dependencies["run"].strip() == validation.DEPENDENCIES_RUN
    assert "aws-access-key" not in (ROOT / validation.DEPLOYMENT).read_text()


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


def offline_check_arguments(monkeypatch, tmp_path, *, base_present=True):
    """`check` against the repository with git and action metadata replaced by offline stand-ins."""
    deployment_text = (ROOT / validation.DEPLOYMENT).read_text()

    def git_text(_root, *args):
        if args[0] == "ls-tree":
            return validation.DEPLOYMENT if base_present else ""
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


@pytest.mark.parametrize("base_present", [True, False])
def test_check_reports_the_distribution_manifest_in_its_source_hashes(monkeypatch, tmp_path, capsys, base_present):
    args = offline_check_arguments(monkeypatch, tmp_path, base_present=base_present)
    validation.check(args)
    report = json.loads(capsys.readouterr().out)
    assert report["checks"] == "static_contracts_and_syntax_passed" and report["aws_action_executed"] is False
    assert report["base_deployment"] == ("compared" if base_present else "absent")
    assert set(report["metadata"]) == set(report["contract_refs"]) | set(
        json.loads((ROOT / "scripts/actions_contracts.json").read_text())["contract_candidates"])
    assert {ref.split("@", 1)[0] for ref in report["contract_refs"]} == {CHECKOUT, PYTHON, AWS, UPLOAD, DOWNLOAD}
    assert report["source_hashes"]["docs/local_server/DYNAMODB_DISTRIBUTION_MANIFEST.json"] == \
        hashlib.sha256(DISTRIBUTION_MANIFEST.read_bytes()).hexdigest()


def test_check_rejects_a_base_listing_that_names_another_file(monkeypatch, tmp_path):
    args = offline_check_arguments(monkeypatch, tmp_path)
    monkeypatch.setattr(validation, "git_text", lambda _root, *args: "other/file.yml" if args[0] == "ls-tree" else "c" * 40)
    assert_rejected("SOURCE_REVISION_UNAVAILABLE", validation.check, args)


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

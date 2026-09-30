"""PR smoke evidence and static action contracts. Never runs AWS actions.

The coverage guard compares newly introduced deployment refs with unconditional
smoke steps, so CI-only introduction and individual dependency PRs remain valid.
This self-check is review support, not a sandbox against a PR changing the guard.

Where to change together when the CI changes (the yml is pinned here as text,
so a reviewed CI change touches every copy at once):

* DynamoDB Local archive (URL or digest): both `dynamodb_download` step envs in
  .github/workflows/validate_actions.yml, DYNAMODB_ARCHIVE below, the `archive`
  field of docs/local_server/DYNAMODB_DISTRIBUTION_MANIFEST.json (its `files`
  must be exactly the archive's contents; local_server.cli.verify_distribution
  checks that on the runner), the literal in tests/test_validate_actions.py
  (test_distribution_manifest_pins_the_archive_ci_downloads), and the CI
  paragraphs of docs/DEPLOY_GUIDE.md, docs/LOCAL_RUN.md and docs/VALIDATION.md.
* Shared DynamoDB Local step bodies (dependencies, JDK, download, verify): the
  yml (byte-identical in both jobs) and INTEGRATION_RUNS below. The suite step:
  the yml and SUITE_RUN_TEMPLATE below. The tests mutate a copy of the yml, so
  they follow the validator.
* Adding or removing a DynamoDB Local job: DYNAMODB_JOB_IDS below (step ids,
  suite commands and the `smoke` ref count derive from it), MAX_JOB_MINUTES
  below, the yml job, a scripts/test_suites.py LOCAL_SUITES entry of the same
  name, tests/test_ci_suites.py, tests/test_validate_actions.py (`smoke_job`
  job set and the smoke ref-count test) and docs/DEPLOY_GUIDE.md.
* Job timeouts: the yml `timeout-minutes` and its header comment (measured
  duration x3 + 3 min), MAX_JOB_MINUTES below and docs/DEPLOY_GUIDE.md.
* The offline unit suite selection: scripts/test_suites.py only.
* The Dev deployment workflow (D137, .github/workflows/deploy_dev.yml): its
  trigger, permissions, concurrency, job env, step ids/order, fixed run bodies
  (DEPLOY_RUNS), the single OIDC step (DEPLOY_OIDC_WITH), the artifact steps
  and MAX_DEPLOY_MINUTES below; tests/test_validate_actions.py mutates a copy
  of the yml; docs/DEPLOY_GUIDE.md "자동 배포" section.
* The check-config command of that workflow: the yml, DEPLOY_RUNS["check_config"]
  and, because the PR base is compared with it, CHECK_CONFIG_PREDECESSOR_RUNS
  (the one earlier body a base revision may still carry; D140 added
  `--registry`). A further change of that command moves the current body into
  the predecessor tuple in the same reviewed change.
"""

import argparse
import ast
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request


DEPLOYMENT = ".github/workflows/deploy_dev.yml"
VALIDATION = ".github/workflows/validate_actions.yml"
MANIFEST = "scripts/actions_contracts.json"
CHECKOUT = "actions/checkout"
PYTHON = "actions/setup-python"
AWS = "aws-actions/configure-aws-credentials"
UPLOAD = "actions/upload-artifact"
DOWNLOAD = "actions/download-artifact"
OWNERS = (CHECKOUT, PYTHON, AWS, UPLOAD, DOWNLOAD)
SHA = re.compile(r"[0-9a-f]{40}\Z")
MAX_METADATA = 128 * 1024
STEP_IDS = ("checkout_smoke", "python_smoke", "verify_smoke", "dependencies", "actionlint", "contracts", "regression")
# Keep the small validation job executable, not merely a list of action pins.
DEPENDENCIES_RUN = (
    'python3 -m venv "$RUNNER_TEMP/actions-venv"\n'
    'ci_constraints_args=()\n'
    'if [[ -f constraints-lambda.txt ]]; then\n'
    '  ci_constraints_args=(-c constraints-lambda.txt)\n'
    'fi\n'
    '"$RUNNER_TEMP/actions-venv/bin/python" -m pip install --only-binary=:all: \\\n'
    '  -r requirements.txt -r requirements-ci.txt "${ci_constraints_args[@]}"\n'
    '"$RUNNER_TEMP/actions-venv/bin/python" --version\n'
    '"$RUNNER_TEMP/actions-venv/bin/python" -m pip --version\n'
    '"$RUNNER_TEMP/actions-venv/bin/python" -m pip freeze'
)
CRITICAL_RUNS = {
    "dependencies": DEPENDENCIES_RUN,
    "actionlint": 'python3 scripts/install_actionlint.py --output-dir "$RUNNER_TEMP/actionlint"\n'
                  '"$RUNNER_TEMP/actionlint/actionlint" -shellcheck= -pyflakes= \\\n'
                  '  .github/workflows/deploy_dev.yml .github/workflows/validate_actions.yml',
    "contracts": '"$RUNNER_TEMP/actions-venv/bin/python" scripts/validate_actions.py check \\\n'
                 '  --base-sha "$PR_BASE_SHA" --metadata-dir "$RUNNER_TEMP/action-metadata"',
    "regression": '"$RUNNER_TEMP/actions-venv/bin/python" scripts/run_actions_regression.py',
}
# Two parallel DynamoDB Local jobs. Each job id is also the fixed
# scripts/test_suites.py suite it runs; the two suites are disjoint and, with the
# offline unit suite of the `validate` job, cover the local `--suite all`.
DYNAMODB_JOB_IDS = ("integration", "boundary")
# Steps whose id, body, env and shell are identical in every DynamoDB Local job.
SHARED_DYNAMODB_STEP_IDS = ("local_dependencies", "jdk", "dynamodb_download", "dynamodb_verify")
# Per job: its own checkout/Python step ids, the shared steps, then its suite step.
DYNAMODB_JOBS = {
    job_id: (job_id + "_checkout", job_id + "_python", *SHARED_DYNAMODB_STEP_IDS, "local_" + job_id)
    for job_id in DYNAMODB_JOB_IDS
}
# Every DynamoDB Local job downloads one digest-pinned archive from the official
# host, then checks every file against the checked-in distribution manifest.
# The manifest's `archive` field must name the same URL and digest (checked by
# `check`), so the digest chain archive -> extracted files is recorded in one
# reviewed place next to the file fingerprints it belongs to.
DYNAMODB_ARCHIVE = {
    "DYNAMODB_LOCAL_URL": "https://d1ni2b6xgvw0s0.cloudfront.net/v2.x/dynamodb_local_2026-07-31.tar.gz",
    "DYNAMODB_LOCAL_SHA256": "f80bcec477f85f57e2c77f8d54aa6b672a8403fceff0c450560aee1cf6c21163",
}
DISTRIBUTION_MANIFEST = "docs/local_server/DYNAMODB_DISTRIBUTION_MANIFEST.json"
# (run body, env, shell) per step; no other step keys or alternative bodies.
# These shared steps are byte-identical in both DynamoDB Local jobs.
INTEGRATION_RUNS = {
    "local_dependencies": (
        'python3 -m venv "$RUNNER_TEMP/integration-venv"\n'
        '"$RUNNER_TEMP/integration-venv/bin/python" -m pip install --only-binary=:all: \\\n'
        '  -r requirements-local.txt -r requirements-ci.txt -c constraints-lambda.txt\n'
        '"$RUNNER_TEMP/integration-venv/bin/python" -m pip freeze', None, None),
    "jdk": ("java -version\njavac -version", None, None),
    "dynamodb_download": (
        "curl --fail --silent --show-error --proto '=https' --tlsv1.2 --max-time 300 --retry 3 \\\n"
        '  --output "$RUNNER_TEMP/dynamodb_local.tar.gz" "$DYNAMODB_LOCAL_URL"\n'
        'echo "$DYNAMODB_LOCAL_SHA256  $RUNNER_TEMP/dynamodb_local.tar.gz" | sha256sum --check --strict\n'
        'mkdir -m 700 "$RUNNER_TEMP/dynamodb-local"\n'
        'tar -xzf "$RUNNER_TEMP/dynamodb_local.tar.gz" -C "$RUNNER_TEMP/dynamodb-local" --no-same-owner\n'
        'rm -f -- "$RUNNER_TEMP/dynamodb_local.tar.gz"', DYNAMODB_ARCHIVE, "bash"),
    "dynamodb_verify": (
        '"$RUNNER_TEMP/integration-venv/bin/python" -c \'import pathlib, sys; from local_server.cli import '
        'verify_distribution; verify_distribution(pathlib.Path(sys.argv[1]))\' \\\n'
        '  "$RUNNER_TEMP/dynamodb-local"', None, None),
}
# The last step of each DynamoDB Local job differs only in its fixed suite name,
# which is the job id.
SUITE_RUN_TEMPLATE = (
    '"$RUNNER_TEMP/integration-venv/bin/python" scripts/validate_local_integration.py \\\n'
    '  --dynamodb-home "$RUNNER_TEMP/dynamodb-local" --suite {suite}'
)
SUITE_RUNS = {
    step_ids[-1]: (SUITE_RUN_TEMPLATE.format(suite=job_id), {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}, None)
    for job_id, step_ids in DYNAMODB_JOBS.items()
}
# Upper bound of `timeout-minutes` per job: the value the yml header derives
# from the 2026-09-28 local measurement (about 3x the measured suite time plus
# ~3 min for checkout, installation and downloads, rounded up: 154 s -> 12,
# 193 s -> 13, 174 s -> 12). A longer timeout needs a new measurement and a
# reviewed change here, not only in the yml. Each DynamoDB Local job has its
# own bound; they run in parallel, so this is never a sum.
MAX_JOB_MINUTES = {"validate": 12, "integration": 13, "boundary": 12}
MAX_VALIDATE_MINUTES = MAX_JOB_MINUTES["validate"]
# The loosest DynamoDB Local bound; validate_integration applies the per-job one.
MAX_INTEGRATION_MINUTES = max(MAX_JOB_MINUTES[job_id] for job_id in DYNAMODB_JOBS)

# --- Dev deployment workflow (D137) ---
# A merge to develop deploys the three Dev Lambdas' code after the offline
# regression of the same revision and the `development` environment approval.
# Upper bound of its `timeout-minutes`: (154 s regression + ~60 s build) x 3 +
# 3 min, rounded up to hold the deploy and smoke (see the yml header comment).
MAX_DEPLOY_MINUTES = 15
DEPLOY_JOB_ID = "deploy-dev"
DEPLOY_ENVIRONMENT = "development"
DEPLOY_TRIGGERS = {
    "push": {"branches": ["develop"]},
    "workflow_dispatch": {"inputs": {"rollback_run_id": {"type": "string", "required": False, "default": ""}}},
}
DEPLOY_PERMISSIONS = {"contents": "read", "id-token": "write", "actions": "read"}
DEPLOY_CONCURRENCY = {"group": "deploy-development", "cancel-in-progress": False}
DEPLOY_ENV = {
    "AWS_REGION": "us-east-2",
    "ARC_DEV_FUNCTIONS_API": "arc-calc-dev-api",
    "ARC_DEV_FUNCTIONS_WORKER": "arc-calc-dev-worker",
    "ARC_DEV_FUNCTIONS_RELAY": "arc-calc-dev-relay",
    "AWS_ROLE_ARN": "${{ secrets.DEV_AWS_ROLE_ARN }}",
    "DEV_API_BASE_URL": "${{ vars.DEV_API_BASE_URL }}",
}
# The only secret and the only variable the workflow may reference, and only
# after the regression step (the job env is checked separately).
DEPLOY_SECRETS = {"DEV_AWS_ROLE_ARN"}
DEPLOY_VARIABLES = {"DEV_API_BASE_URL"}
DEPLOY_STEP_IDS = ("checkout", "python", "dependencies", "regression", "build", "upload", "download",
                   "credentials", "check_config", "deploy", "smoke", "summary")
BUILD_CONDITION = "inputs.rollback_run_id == ''"
ROLLBACK_CONDITION = "inputs.rollback_run_id != ''"
# Allowed `if` per step (None: the key must be absent). No step may set continue-on-error.
DEPLOY_STEP_CONDITIONS = {"build": BUILD_CONDITION, "upload": BUILD_CONDITION, "download": ROLLBACK_CONDITION,
                          "summary": "always()"}
DEPLOY_SCRIPT = "scripts/deploy_dev_lambdas.py"
DEPLOY_PYTHON = '"$RUNNER_TEMP/actions-venv/bin/python"'
DEPLOY_FUNCTION_ORDER = ("worker", "relay", "api")
# D140: the build writes the code's execution registry next to the ZIP (with
# the CI venv, which has the application requirements); the artifact keeps it,
# and check-config and deploy read that same file, so a rollback syncs the
# configuration to the registry of the ZIP it re-deploys.
DEPLOY_REGISTRY = '"$RUNNER_TEMP/artifact/execution-registry.json"'
DEPLOY_REGISTRY_ARGUMENT = "--registry " + DEPLOY_REGISTRY
# (run body, env, shell) per run step of the deployment job.
DEPLOY_RUNS = {
    "dependencies": (DEPENDENCIES_RUN, None, "bash"),
    "regression": (CRITICAL_RUNS["regression"], {"STAGE": "test", "AWS_EC2_METADATA_DISABLED": "true"}, None),
    "build": (
        'umask 077\n'
        'python3 -m venv "$RUNNER_TEMP/build-python"\n'
        '"$RUNNER_TEMP/build-python/bin/python" -m pip install -r requirements.txt -c constraints-lambda.txt \\\n'
        '  --target "$RUNNER_TEMP/packages" --no-compile --only-binary=:all: \\\n'
        '  --platform manylinux2014_x86_64 --implementation cp --python-version 3.12\n'
        '"$RUNNER_TEMP/build-python/bin/python" scripts/build_mock_artifact.py \\\n'
        '  --source-root "$GITHUB_WORKSPACE" --packages-dir "$RUNNER_TEMP/packages" --outdir "$RUNNER_TEMP/artifact"\n'
        '"$RUNNER_TEMP/build-python/bin/python" -m zipfile -t "$RUNNER_TEMP/artifact/mock-lambda.zip"\n'
        + DEPLOY_PYTHON + ' scripts/deploy_dev_lambdas.py registry \\\n'
        '  --output ' + DEPLOY_REGISTRY, None, None),
    "check_config": (
        DEPLOY_PYTHON + ' scripts/deploy_dev_lambdas.py check-config \\\n'
        '  --region "$AWS_REGION" --function api="$ARC_DEV_FUNCTIONS_API" --function worker="$ARC_DEV_FUNCTIONS_WORKER" \\\n'
        '  ' + DEPLOY_REGISTRY_ARGUMENT,
        None, None),
    "deploy": (
        DEPLOY_PYTHON + ' scripts/deploy_dev_lambdas.py deploy \\\n'
        '  --region "$AWS_REGION" --zip "$RUNNER_TEMP/artifact/mock-lambda.zip" \\\n'
        '  --function worker="$ARC_DEV_FUNCTIONS_WORKER" --function relay="$ARC_DEV_FUNCTIONS_RELAY" '
        '--function api="$ARC_DEV_FUNCTIONS_API" \\\n'
        '  --manifest "$RUNNER_TEMP/artifact/artifact-manifest.json" --report "$RUNNER_TEMP/deploy-report.json" \\\n'
        '  ' + DEPLOY_REGISTRY_ARGUMENT,
        None, None),
    "smoke": (
        DEPLOY_PYTHON + ' scripts/deploy_dev_lambdas.py smoke \\\n'
        '  --base-url "$DEV_API_BASE_URL" --report "$RUNNER_TEMP/smoke-report.json"', None, None),
    "summary": (
        DEPLOY_PYTHON + ' scripts/deploy_dev_lambdas.py summary \\\n'
        '  --commit "$GITHUB_SHA" --zip "$RUNNER_TEMP/artifact/mock-lambda.zip" \\\n'
        '  --deploy-report "$RUNNER_TEMP/deploy-report.json" --smoke-report "$RUNNER_TEMP/smoke-report.json" \\\n'
        '  --output "$GITHUB_STEP_SUMMARY"', None, None),
}
# The check-config bodies an older PR base may carry. The base comparison
# accepts exactly one kind of change of that step: from one of these to the
# current fixed body above (D137 -> D140: `--registry` added). Any other base
# body, env, condition or key is rejected as before.
CHECK_CONFIG_PREDECESSOR_RUNS = (
    DEPLOY_PYTHON + ' scripts/deploy_dev_lambdas.py check-config \\\n'
    '  --region "$AWS_REGION" --function api="$ARC_DEV_FUNCTIONS_API" --function worker="$ARC_DEV_FUNCTIONS_WORKER"',
)
DEPLOY_OIDC_WITH = {"role-to-assume": "${{ env.AWS_ROLE_ARN }}", "role-session-name": "arc-deploy-development",
                    "aws-region": "${{ env.AWS_REGION }}"}
DEPLOY_UPLOAD_WITH = {
    "name": "mock-lambda-${{ github.sha }}",
    "path": "${{ runner.temp }}/artifact/mock-lambda.zip\n${{ runner.temp }}/artifact/artifact-manifest.json\n"
            "${{ runner.temp }}/artifact/execution-registry.json\n",
    "if-no-files-found": "error",
    "retention-days": 30,
}
DEPLOY_DOWNLOAD_WITH = {
    "pattern": "mock-lambda-*",
    "path": "${{ runner.temp }}/artifact",
    "merge-multiple": True,
    "run-id": "${{ inputs.rollback_run_id }}",
    "github-token": "${{ github.token }}",
}
# Long-lived key inputs/secrets must not appear anywhere in the deployment workflow.
ACCESS_KEY_PATTERN = re.compile(r"aws[-_]?access[-_]?key|aws[-_]?secret[-_]?access|aws[-_]?session[-_]?token", re.I)
CONTEXT_REFERENCE = re.compile(r"\b(secrets|vars)\s*(?:\.\s*([A-Za-z_][A-Za-z0-9_]*)|\[)", re.I)


class ValidationError(ValueError):
    """Fixed diagnostic codes; no raw request, environment or file content."""


def require(condition, code):
    if not condition:
        raise ValidationError(code)


def load_yaml(text):
    # Imported only after tool installation; the earlier smoke needs stdlib only.
    import yaml

    class UniqueLoader(yaml.SafeLoader):
        pass

    UniqueLoader.yaml_implicit_resolvers = copy.deepcopy(yaml.SafeLoader.yaml_implicit_resolvers)
    for char, resolvers in UniqueLoader.yaml_implicit_resolvers.items():
        UniqueLoader.yaml_implicit_resolvers[char] = [
            (tag, pattern) for tag, pattern in resolvers if tag != "tag:yaml.org,2002:bool"
        ]
    UniqueLoader.add_implicit_resolver("tag:yaml.org,2002:bool", re.compile(r"^(?:true|false)$", re.I), list("tTfF"))

    def mapping(loader, node):
        result = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=True)
            require(isinstance(key, (str, bool, int, float)), "YAML_STRUCTURE_INVALID")
            require(key not in result, "YAML_DUPLICATE_KEY")
            result[key] = loader.construct_object(value_node, deep=True)
        return result

    UniqueLoader.add_constructor("tag:yaml.org,2002:map", mapping)
    try:
        require(not any(isinstance(token, (yaml.AliasToken, yaml.AnchorToken)) for token in yaml.scan(text)),
                "YAML_STRUCTURE_INVALID")
        result = yaml.load(text, Loader=UniqueLoader)
        require(isinstance(result, dict), "YAML_STRUCTURE_INVALID")
        return result
    except yaml.YAMLError:
        raise ValidationError("YAML_STRUCTURE_INVALID") from None


def action_ref(value):
    require(type(value) is str and "@" in value, "ACTION_REF_INVALID")
    repo, sha = value.rsplit("@", 1)
    require(repo in OWNERS and SHA.fullmatch(sha), "ACTION_REF_INVALID")
    return repo, sha


def action_steps(document):
    result = []
    jobs = document.get("jobs")
    require(isinstance(jobs, dict), "YAML_STRUCTURE_INVALID")
    for job in jobs.values():
        require(isinstance(job, dict) and isinstance(job.get("steps"), list), "YAML_STRUCTURE_INVALID")
        for step in job["steps"]:
            require(isinstance(step, dict), "YAML_STRUCTURE_INVALID")
            if "uses" in step:
                action_ref(step["uses"])
                result.append(step)
    return result


def context_references(value):
    """(context, name) pairs of every `secrets.`/`vars.` reference in a YAML subtree; `[` forms count as name None."""
    return [(m.group(1).lower(), m.group(2)) for m in CONTEXT_REFERENCE.finditer(json.dumps(value))]


def validate_deployment(document):
    """D137: fixed trigger, minimal permissions, one approved job, regression before any secret, OIDC only."""
    triggers = document.get("on")
    require(isinstance(triggers, dict) and set(triggers) == set(DEPLOY_TRIGGERS), "DEPLOYMENT_GATE_INVALID")
    require(triggers["push"] == DEPLOY_TRIGGERS["push"], "DEPLOYMENT_GATE_INVALID")
    dispatch = triggers["workflow_dispatch"]
    require(isinstance(dispatch, dict) and set(dispatch) == {"inputs"} and isinstance(dispatch["inputs"], dict)
            and set(dispatch["inputs"]) == {"rollback_run_id"}, "DEPLOYMENT_GATE_INVALID")
    rollback_input = dispatch["inputs"]["rollback_run_id"]
    require(isinstance(rollback_input, dict) and set(rollback_input) - {"description"} == {"type", "required", "default"}
            and {k: v for k, v in rollback_input.items() if k != "description"}
            == DEPLOY_TRIGGERS["workflow_dispatch"]["inputs"]["rollback_run_id"], "DEPLOYMENT_GATE_INVALID")
    require(document.get("permissions") == DEPLOY_PERMISSIONS, "DEPLOYMENT_AUTH_INVALID")
    require(document.get("concurrency") == DEPLOY_CONCURRENCY, "DEPLOYMENT_GATE_INVALID")
    require("env" not in document and "defaults" not in document, "DEPLOYMENT_AUTH_INVALID")
    require(set(document.get("jobs", {})) == {DEPLOY_JOB_ID}, "DEPLOYMENT_AUTH_INVALID")
    job = document["jobs"][DEPLOY_JOB_ID]
    require(isinstance(job, dict) and set(job) - {"name"} == {"runs-on", "timeout-minutes", "environment", "env", "steps"},
            "DEPLOYMENT_AUTH_INVALID")
    require(job["runs-on"] == "ubuntu-latest" and job["environment"] == DEPLOY_ENVIRONMENT, "DEPLOYMENT_AUTH_INVALID")
    require(type(job["timeout-minutes"]) is int and 1 <= job["timeout-minutes"] <= MAX_DEPLOY_MINUTES,
            "DEPLOYMENT_GATE_INVALID")
    require(job["env"] == DEPLOY_ENV, "DEPLOYMENT_AUTH_INVALID")
    steps = job["steps"]
    require(isinstance(steps, list) and all(isinstance(s, dict) for s in steps), "DEPLOYMENT_GATE_INVALID")
    require(tuple(s.get("id") for s in steps) == DEPLOY_STEP_IDS, "DEPLOYMENT_GATE_INVALID")
    selected = dict(zip(DEPLOY_STEP_IDS, steps))
    for step_id, step in selected.items():
        require(set(step) <= {"name", "id", "uses", "with", "run", "if", "env", "shell"}, "DEPLOYMENT_GATE_INVALID")
        require(step.get("if") == DEPLOY_STEP_CONDITIONS.get(step_id), "DEPLOYMENT_GATE_INVALID")
    # 1-2: checkout and Python exactly as the CI smoke job does.
    checkout, python = selected["checkout"], selected["python"]
    require(set(checkout) - {"name"} == {"id", "uses", "with"} and action_ref(checkout["uses"])[0] == CHECKOUT
            and checkout["with"] == {"persist-credentials": False, "ref": "${{ github.sha }}"}, "DEPLOYMENT_GATE_INVALID")
    require(set(python) - {"name"} == {"id", "uses", "with"} and action_ref(python["uses"])[0] == PYTHON
            and python["with"] == {"python-version": "3.12"}, "DEPLOYMENT_GATE_INVALID")
    # 3-4, 6-9: fixed run bodies; the regression step is the CI one, in the CI venv.
    for step_id, (body, env, shell) in DEPLOY_RUNS.items():
        step = selected[step_id]
        keys = {"id", "run"} | ({"env"} if env is not None else set()) | ({"shell"} if shell else set())
        if step_id in DEPLOY_STEP_CONDITIONS:
            keys.add("if")
        require(set(step) - {"name"} == keys and type(step["run"]) is str and step["run"].strip() == body
                and step.get("env") == env and step.get("shell") == shell,
                "DEPLOYMENT_AUTH_INVALID" if step_id == "check_config" else "DEPLOYMENT_GATE_INVALID")
    regression_index = DEPLOY_STEP_IDS.index("regression")
    require(not context_references(steps[:regression_index + 1]), "DEPLOYMENT_GATE_INVALID")
    require(not any(k in s for s in steps for k in ("continue-on-error",)), "DEPLOYMENT_GATE_INVALID")
    # 3-4: artifact steps; the download uses the run's own token, never a new secret.
    upload, download = selected["upload"], selected["download"]
    require(set(upload) - {"name"} == {"id", "uses", "with", "if"} and action_ref(upload["uses"])[0] == UPLOAD
            and upload["with"] == DEPLOY_UPLOAD_WITH, "DEPLOYMENT_GATE_INVALID")
    require(set(download) - {"name"} == {"id", "uses", "with", "if"} and action_ref(download["uses"])[0] == DOWNLOAD
            and download["with"] == DEPLOY_DOWNLOAD_WITH, "DEPLOYMENT_GATE_INVALID")
    # 5: exactly one credentials step, OIDC only, no env, no condition.
    credentials = [s for s in steps if "uses" in s and action_ref(s["uses"])[0] == AWS]
    require(credentials == [selected["credentials"]], "DEPLOYMENT_AUTH_INVALID")
    require(set(selected["credentials"]) - {"name"} == {"id", "uses", "with"}
            and selected["credentials"]["with"] == DEPLOY_OIDC_WITH, "DEPLOYMENT_AUTH_INVALID")
    require(not ACCESS_KEY_PATTERN.search(json.dumps(document)), "DEPLOYMENT_AUTH_INVALID")
    references = context_references(document)
    require({name for context, name in references if context == "secrets"} == DEPLOY_SECRETS
            and {name for context, name in references if context == "vars"} == DEPLOY_VARIABLES
            and all(name for _, name in references), "DEPLOYMENT_AUTH_INVALID")
    # 7: Worker -> Relay -> API, read back from the deploy command itself.
    require(tuple(re.findall(r"--function ([a-z]+)=", selected["deploy"]["run"])) == DEPLOY_FUNCTION_ORDER,
            "DEPLOYMENT_GATE_INVALID")
    require(all(re.search(re.escape(DEPLOY_SCRIPT) + " " + re.escape(sub) + r"\b", selected[step_id]["run"])
                for step_id, sub in (("build", "registry"), ("check_config", "check-config"), ("deploy", "deploy"),
                                     ("smoke", "smoke"))),
            "DEPLOYMENT_GATE_INVALID")
    # D140: the registry is written once by the build, kept in the artifact,
    # and both AWS steps read that one file (never a registry of their own).
    require(selected["build"]["run"].count("--output " + DEPLOY_REGISTRY) == 1
            and all(selected[step_id]["run"].count(DEPLOY_REGISTRY_ARGUMENT) == 1
                    and selected[step_id]["run"].count("--registry") == 1 for step_id in ("check_config", "deploy"))
            and "${{ runner.temp }}/artifact/execution-registry.json" in upload["with"]["path"].split("\n"),
            "DEPLOYMENT_GATE_INVALID")


def validate_integration(job, checkout, python, job_id):
    """Run one fixed DynamoDB Local suite only through the fixed, verified commands."""
    require(job_id in DYNAMODB_JOBS, "SMOKE_STRUCTURE_INVALID")
    step_ids = DYNAMODB_JOBS[job_id]
    require(isinstance(job, dict), "SMOKE_STRUCTURE_INVALID")
    require("permissions" not in job, "CI_BOUNDARY_INVALID")
    require(set(job) <= {"name", "runs-on", "timeout-minutes", "steps"}, "SMOKE_STRUCTURE_INVALID")
    require(job.get("runs-on") == "ubuntu-latest" and type(job.get("timeout-minutes")) is int
            and 1 <= job["timeout-minutes"] <= MAX_JOB_MINUTES[job_id], "CI_BOUNDARY_INVALID")
    steps = job.get("steps")
    require(isinstance(steps, list) and all(isinstance(s, dict) for s in steps), "SMOKE_STRUCTURE_INVALID")
    require(tuple(s.get("id") for s in steps) == step_ids, "SMOKE_STRUCTURE_INVALID")
    selected = dict(zip(step_ids, steps))
    first, second = selected[step_ids[0]], selected[step_ids[1]]
    require(set(first) - {"name"} == {"id", "uses", "with"} and first["uses"] == checkout["uses"]
            and first["with"] == {"persist-credentials": False, "ref": "${{ github.sha }}"},
            "SMOKE_STRUCTURE_INVALID")
    require(set(second) - {"name"} == {"id", "uses", "with"} and second["uses"] == python["uses"]
            and second["with"] == {"python-version": "3.12"}, "SMOKE_STRUCTURE_INVALID")
    suite_step = step_ids[-1]
    for step_id, (body, env, shell) in (*INTEGRATION_RUNS.items(), (suite_step, SUITE_RUNS[suite_step])):
        step = selected[step_id]
        keys = {"id", "run"} | ({"env"} if env is not None else set()) | ({"shell"} if shell else set())
        require(set(step) - {"name"} == keys and type(step["run"]) is str and step["run"].strip() == body
                and step.get("shell") == shell, "SMOKE_STRUCTURE_INVALID")
        require(step.get("env") == env, "CI_BOUNDARY_INVALID")


def is_check_config_step(step, bodies):
    """A check-config step (without `name`) that is nothing but its id and one of the given run bodies."""
    return (set(step) == {"id", "run"} and step["id"] == "check_config" and type(step["run"]) is str
            and step["run"].strip() in bodies)


def validate_workflows(base, deployment, ci):
    # No equality requirement between ALL deployment refs and the smoke refs.
    # `base` is None on the PR that introduces the deployment workflow (the
    # file is absent at the base revision): every ref is then "introduced" and
    # the checkout/Python refs must be the CI smoke refs; there is nothing to
    # compare the env, OIDC and check-config steps with. In both cases
    # validate_deployment pins the current check-config step to its fixed body.
    old_steps, current_steps = (action_steps(base) if base is not None else []), action_steps(deployment)
    if base is not None:
        require(base.get("env") == deployment.get("env"), "DEPLOYMENT_AUTH_INVALID")
        for name, job in deployment.get("jobs", {}).items():
            old_job = base.get("jobs", {}).get(name, {})
            require(isinstance(old_job, dict) and old_job.get("env") == job.get("env"), "DEPLOYMENT_AUTH_INVALID")
            # The AWS action also translates env variables such as ROLE_CHAINING
            # into inputs, so the whole step (except its pinned ref) is compared.
            # The check-config step is compared the same way, with one reviewed
            # exception (D140): a base that still carries a predecessor body may
            # become exactly the current fixed body. Nothing else about that step
            # (env, shell, condition, extra keys) may differ from the base.
            for step_name, selector in (("oidc", lambda s: s.get("uses", "").startswith(AWS + "@")),
                                        ("check_config", lambda s: s.get("id") == "check_config")):
                old = [s for s in old_job.get("steps", []) if selector(s)]
                new = [s for s in job.get("steps", []) if selector(s)]
                require(len(old) == len(new) == 1, "DEPLOYMENT_AUTH_INVALID")
                before = {k: v for k, v in old[0].items() if k not in ("uses", "name")}
                after = {k: v for k, v in new[0].items() if k not in ("uses", "name")}
                if step_name == "check_config" and before != after:
                    require(is_check_config_step(before, CHECK_CONFIG_PREDECESSOR_RUNS)
                            and is_check_config_step(after, (DEPLOY_RUNS["check_config"][0],)),
                            "DEPLOYMENT_AUTH_INVALID")
                else:
                    require(before == after, "DEPLOYMENT_AUTH_INVALID")
    require(ci.get("permissions") == {"contents": "read"}, "CI_BOUNDARY_INVALID")
    triggers = ci.get("on")
    require(isinstance(triggers, dict) and set(triggers) == {"pull_request"}, "CI_BOUNDARY_INVALID")
    require(triggers["pull_request"] == {"types": ["opened", "synchronize", "reopened"]}, "CI_BOUNDARY_INVALID")
    require(not re.search(r"\bsecrets\s*(?:\.|\[)", json.dumps(ci), re.I), "CI_BOUNDARY_INVALID")
    require(ci.get("concurrency", {}).get("cancel-in-progress") is True
            and "github.event.pull_request.number" in ci.get("concurrency", {}).get("group", ""),
            "CI_BOUNDARY_INVALID")
    require(set(ci.get("jobs", {})) == {"validate", *DYNAMODB_JOBS}, "SMOKE_STRUCTURE_INVALID")
    job = ci["jobs"]["validate"]
    require(isinstance(job, dict), "SMOKE_STRUCTURE_INVALID")
    require(job.get("runs-on") == "ubuntu-latest" and type(job.get("timeout-minutes")) is int
            and 1 <= job["timeout-minutes"] <= MAX_VALIDATE_MINUTES, "CI_BOUNDARY_INVALID")
    require(not any(k in job for k in ("if", "needs", "strategy", "uses", "continue-on-error", "container", "environment")),
            "SMOKE_STRUCTURE_INVALID")
    require("permissions" not in job, "CI_BOUNDARY_INVALID")
    steps = job.get("steps", [])
    require(isinstance(steps, list) and all(isinstance(s, dict) for s in steps), "SMOKE_STRUCTURE_INVALID")
    require(not any(k in s for s in steps for k in ("if", "continue-on-error")), "SMOKE_STRUCTURE_INVALID")
    ids = [s.get("id") for s in steps if "id" in s]
    require(tuple(ids) == STEP_IDS and len(steps) == len(STEP_IDS), "SMOKE_STRUCTURE_INVALID")
    selected = {s.get("id"): s for s in steps}
    require(all(name in selected for name in ("checkout_smoke", "python_smoke", "verify_smoke")),
            "SMOKE_STRUCTURE_INVALID")
    checkout, python, verify = (selected[n] for n in ("checkout_smoke", "python_smoke", "verify_smoke"))
    require(action_ref(checkout.get("uses"))[0] == CHECKOUT and action_ref(python.get("uses"))[0] == PYTHON,
            "SMOKE_STRUCTURE_INVALID")
    require(checkout.get("with") == {"persist-credentials": False, "fetch-depth": 0, "ref": "${{ github.sha }}"}
            and python.get("with") == {"python-version": "3.12"}, "SMOKE_STRUCTURE_INVALID")
    require(steps.index(checkout) < steps.index(python) < steps.index(verify), "SMOKE_STRUCTURE_INVALID")
    require(verify.get("run", "").strip() ==
            'python3 scripts/validate_actions.py smoke --expected-sha "$EXPECTED_SHA" --python-path "$SETUP_PYTHON_PATH"',
            "SMOKE_STRUCTURE_INVALID")
    require(verify.get("env") == {"EXPECTED_SHA": "${{ github.sha }}",
                                  "SETUP_PYTHON_PATH": "${{ steps.python_smoke.outputs.python-path }}"},
            "SMOKE_STRUCTURE_INVALID")
    require(all(selected[key].get("run", "").strip() == body for key, body in CRITICAL_RUNS.items()),
            "SMOKE_STRUCTURE_INVALID")
    require(selected["contracts"].get("env") == {"PR_BASE_SHA": "${{ github.event.pull_request.base.sha }}"},
            "CI_BOUNDARY_INVALID")
    action_steps(ci)
    # Only the unconditional smoke job counts as coverage; each DynamoDB Local
    # job must reuse exactly the same refs rather than introduce its own.
    smoke = {s["uses"] for s in steps if "uses" in s}
    require(smoke == {checkout["uses"], python["uses"]}, "SMOKE_STRUCTURE_INVALID")
    for job_id in DYNAMODB_JOBS:
        validate_integration(ci["jobs"][job_id], checkout, python, job_id)
    introduced = {}
    for repo in OWNERS:
        before = {s["uses"] for s in old_steps if action_ref(s["uses"])[0] == repo}
        after = {s["uses"] for s in current_steps if action_ref(s["uses"])[0] == repo}
        introduced[repo] = sorted(after - before)
        if repo in (CHECKOUT, PYTHON):
            require(after - before <= smoke, "SMOKE_REF_UNCOVERED")
    validate_deployment(deployment)
    return {"introduced": introduced, "smoke_refs": sorted(smoke),
            "contract_refs": sorted({s["uses"] for s in current_steps} | smoke),
            "base_deployment": "absent" if base is None else "compared"}


def validate_distribution_manifest(root):
    """The archive CI downloads must be the one whose files the manifest fingerprints."""
    manifest = json.loads((root / DISTRIBUTION_MANIFEST).read_text())
    require(isinstance(manifest, dict) and isinstance(manifest.get("files"), dict) and manifest["files"],
            "DISTRIBUTION_MANIFEST_INVALID")
    archive = manifest.get("archive")
    require(isinstance(archive, dict) and set(archive) == {"url", "sha256"}, "DISTRIBUTION_MANIFEST_INVALID")
    require(archive == {"url": DYNAMODB_ARCHIVE["DYNAMODB_LOCAL_URL"],
                        "sha256": DYNAMODB_ARCHIVE["DYNAMODB_LOCAL_SHA256"]}, "DISTRIBUTION_ARCHIVE_MISMATCH")


def validate_metadata(ref, raw, record):
    action_ref(ref)
    require(record.get("sha256") == hashlib.sha256(raw).hexdigest(), "ACTION_METADATA_INVALID")
    metadata = load_yaml(raw.decode("utf-8"))
    runtime = metadata.get("runs", {}).get("using")
    require(runtime in ("node20", "node24") and runtime == record.get("runtime"), "ACTION_METADATA_INVALID")
    require(isinstance(metadata.get("inputs"), dict), "ACTION_METADATA_INVALID")
    return metadata


def validate_inputs(step, metadata):
    supplied = step.get("with", {})
    inputs = metadata["inputs"]
    require(isinstance(supplied, dict) and set(supplied) <= set(inputs), "ACTION_INPUT_INVALID")
    for name, definition in inputs.items():
        if definition.get("required") is True and "default" not in definition:
            require(name in supplied, "ACTION_INPUT_INVALID")


def read_metadata(ref, record, directory, offline):
    repo, sha = action_ref(ref)
    path = directory / (repo.replace("/", "--") + "--" + sha + ".yml")
    if path.exists():
        raw = path.read_bytes()
    else:
        require(not offline, "ACTION_METADATA_MISSING")
        url = f"https://raw.githubusercontent.com/{repo}/{sha}/action.yml"
        try:
            with urllib.request.urlopen(url, timeout=20) as response:
                require(response.geturl() == url, "ACTION_METADATA_DOWNLOAD_FAILED")
                raw = response.read(MAX_METADATA + 1)
        except (OSError, urllib.error.URLError):
            raise ValidationError("ACTION_METADATA_DOWNLOAD_FAILED") from None
    require(len(raw) <= MAX_METADATA, "ACTION_METADATA_INVALID")
    metadata = validate_metadata(ref, raw, record)
    if not path.exists():
        directory.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    return metadata


def git_text(root, *args):
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=30)
    require(result.returncode == 0, "SOURCE_REVISION_UNAVAILABLE")
    return result.stdout.strip()


def base_deployment(root, base_sha):
    """The deployment workflow at the PR base, or None when the base has no such file (introduction PR)."""
    listing = git_text(root, "ls-tree", "--name-only", base_sha, "--", DEPLOYMENT)
    if listing == "":
        return None
    require(listing == DEPLOYMENT, "SOURCE_REVISION_UNAVAILABLE")
    return load_yaml(git_text(root, "show", base_sha + ":" + DEPLOYMENT))


def syntax_check(root):
    shell_files = ("scripts/deploy_arc_lambda.sh", "scripts/deploy_arc_api_gateway.sh", "scripts/ship_dev.sh")
    python_files = ("scripts/deployment_preflight.py", "scripts/build_mock_artifact.py", "scripts/deploy_dev_lambdas.py",
                    "scripts/validate_actions.py", "scripts/install_actionlint.py", "scripts/run_actions_regression.py",
                    "scripts/test_suites.py", "scripts/validate_local_integration.py")
    for name in shell_files:
        result = subprocess.run(["bash", "-n", str(root / name)], capture_output=True, timeout=15)
        require(result.returncode == 0, "SHELL_SYNTAX_INVALID")
    for name in python_files:
        try:
            ast.parse((root / name).read_text(), filename=name)
        except SyntaxError:
            raise ValidationError("PYTHON_SYNTAX_INVALID") from None
    for name in (DEPLOYMENT, VALIDATION):
        for job in load_yaml((root / name).read_text())["jobs"].values():
            for step in job["steps"]:
                if "run" in step:
                    result = subprocess.run(["bash", "-n"], input=step["run"], text=True, capture_output=True, timeout=15)
                    require(result.returncode == 0, "SHELL_SYNTAX_INVALID")


def check(args):
    root = Path(args.root).resolve()
    require(SHA.fullmatch(args.base_sha), "SOURCE_REVISION_INVALID")
    base = base_deployment(root, args.base_sha)
    deployment, ci = (load_yaml((root / name).read_text()) for name in (DEPLOYMENT, VALIDATION))
    report = validate_workflows(base, deployment, ci)
    validate_distribution_manifest(root)
    manifest = json.loads((root / MANIFEST).read_text())
    require(manifest.get("schema_version") == 1 and isinstance(manifest.get("actions"), dict)
            and isinstance(manifest.get("contract_candidates"), list) and manifest["contract_candidates"],
            "ACTION_METADATA_INVALID")
    refs = sorted(set(report["contract_refs"]) | set(manifest["contract_candidates"]))
    metadata = {}
    for ref in refs:
        require(ref in manifest["actions"], "ACTION_CONTRACT_UNREVIEWED")
        record = manifest["actions"][ref]
        metadata[ref] = read_metadata(ref, record, Path(args.metadata_dir), args.offline)
    for step in action_steps(deployment) + action_steps(ci):
        validate_inputs(step, metadata[step["uses"]])
    # The AWS candidate is checked against the CURRENT authentication use,
    # even on a CI-only introduction PR where deployment still uses the old SHA.
    for ref in manifest["contract_candidates"]:
        require(action_ref(ref)[0] == AWS, "ACTION_REF_INVALID")
        for step in action_steps(deployment):
            if action_ref(step["uses"])[0] == AWS:
                validate_inputs(step, metadata[ref])
    syntax_check(root)
    report.update(base_sha=args.base_sha, checkout_sha=git_text(root, "rev-parse", "HEAD"),
                  metadata={ref: {"sha256": manifest["actions"][ref]["sha256"],
                                  "runtime": metadata[ref]["runs"]["using"]} for ref in refs},
                  source_hashes={name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                                 for name in (DEPLOYMENT, VALIDATION, MANIFEST, DISTRIBUTION_MANIFEST,
                                              "requirements.txt", "requirements-ci.txt",
                                              "requirements-local.txt", "scripts/test_suites.py")},
                  aws_action_executed=False, checks="static_contracts_and_syntax_passed")
    print(json.dumps(report, sort_keys=True))


def smoke(args):
    require(SHA.fullmatch(args.expected_sha), "CHECKOUT_SHA_INVALID")
    root = Path.cwd()
    actual = git_text(root, "rev-parse", "HEAD")
    require(actual == args.expected_sha, "CHECKOUT_SHA_MISMATCH")
    require(sys.version_info[:2] == (3, 12), "PYTHON_VERSION_MISMATCH")
    selected = Path(args.python_path)
    require(selected.is_file() and os.path.samefile(selected, sys.executable), "PYTHON_PATH_MISMATCH")
    require(shutil.which("python3") is not None and os.path.samefile(shutil.which("python3"), selected),
            "PYTHON_PATH_MISMATCH")
    workflow = (root / VALIDATION).read_text()
    refs = re.findall(r"^\s*uses:\s*(\S+)", workflow, re.M)
    # The smoke job and every DynamoDB Local job use the same checkout and
    # Python refs: (1 + len(DYNAMODB_JOBS)) jobs x 2 steps.
    require(len(refs) == 2 * (1 + len(DYNAMODB_JOBS)) and len(set(refs)) == 2
            and {action_ref(ref)[0] for ref in refs} == {CHECKOUT, PYTHON}, "SMOKE_STRUCTURE_INVALID")
    evidence = {"checkout_sha": actual, "event_merge_sha": args.expected_sha, "python": sys.version.split()[0],
                "python_path": str(selected), "action_refs": refs, "aws_action_executed": False}
    if os.environ.get("GITHUB_EVENT_PATH"):
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
        pr = event["pull_request"]
        require(all(SHA.fullmatch(pr[key]["sha"]) for key in ("base", "head")), "SOURCE_REVISION_INVALID")
        evidence.update(pr_number=event["number"], base_sha=pr["base"]["sha"], pr_head_sha=pr["head"]["sha"])
    # Select explicit non-secret fields, never dump contexts or environment.
    for name in ("GITHUB_WORKFLOW_SHA", "GITHUB_WORKFLOW_REF", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "RUNNER_OS", "RUNNER_ARCH"):
        if name in os.environ:
            evidence[name.lower()] = os.environ[name]
    if os.environ.get("GITHUB_RUN_ID"):
        evidence["run_url"] = "https://github.com/" + os.environ["GITHUB_REPOSITORY"] + "/actions/runs/" + os.environ["GITHUB_RUN_ID"]
    print(json.dumps(evidence, sort_keys=True))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    first = commands.add_parser("smoke")
    first.add_argument("--expected-sha", required=True)
    first.add_argument("--python-path", required=True)
    second = commands.add_parser("check")
    second.add_argument("--root", default=".")
    second.add_argument("--base-sha", required=True)
    second.add_argument("--metadata-dir", required=True)
    second.add_argument("--offline", action="store_true")
    args = parser.parse_args(argv)
    try:
        (smoke if args.command == "smoke" else check)(args)
        return 0
    except (ValidationError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(str(error) if isinstance(error, ValidationError) else "VALIDATION_FAILED", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""PR smoke evidence and static action contracts. Never runs AWS actions.

The coverage guard compares newly introduced deployment refs with unconditional
smoke steps, so CI-only introduction and individual dependency PRs remain valid.
This self-check is review support, not a sandbox against a PR changing the guard.
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


DEPLOYMENT = ".github/workflows/deploy_arc_lambdas.yml"
VALIDATION = ".github/workflows/validate_actions.yml"
MANIFEST = "scripts/actions_contracts.json"
CHECKOUT = "actions/checkout"
PYTHON = "actions/setup-python"
AWS = "aws-actions/configure-aws-credentials"
OWNERS = (CHECKOUT, PYTHON, AWS)
SHA = re.compile(r"[0-9a-f]{40}\Z")
MAX_METADATA = 128 * 1024
STEP_IDS = ("checkout_smoke", "python_smoke", "verify_smoke", "dependencies", "actionlint", "contracts", "regression")
# Keep the small validation job executable, not merely a list of action pins.
CRITICAL_RUNS = {
    "actionlint": 'python3 scripts/install_actionlint.py --output-dir "$RUNNER_TEMP/actionlint"\n'
                  '"$RUNNER_TEMP/actionlint/actionlint" -shellcheck= -pyflakes= \\\n'
                  '  .github/workflows/deploy_arc_lambdas.yml .github/workflows/validate_actions.yml',
    "contracts": '"$RUNNER_TEMP/actions-venv/bin/python" scripts/validate_actions.py check \\\n'
                 '  --base-sha "$PR_BASE_SHA" --metadata-dir "$RUNNER_TEMP/action-metadata"',
    "regression": '"$RUNNER_TEMP/actions-venv/bin/python" scripts/run_actions_regression.py',
}


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


def validate_deployment(document):
    """Preserve the two existing auth paths and their offline prerequisite."""
    require(document.get("permissions") == {"contents": "read", "id-token": "write"}, "DEPLOYMENT_AUTH_INVALID")
    require(set(document.get("jobs", {})) == {"deploy-dev", "deploy-prod"}, "DEPLOYMENT_AUTH_INVALID")
    for suffix, environment, prefix in (("dev", "development", "DEV"), ("prod", "production", "PROD")):
        job = document["jobs"]["deploy-" + suffix]
        require(job.get("runs-on") == "ubuntu-latest" and "container" not in job, "DEPLOYMENT_AUTH_INVALID")
        require(job.get("permissions", document["permissions"]) == document["permissions"]
                and "environment" not in job, "DEPLOYMENT_AUTH_INVALID")
        require(job.get("env", {}).get("AWS_ROLE_ARN") == "${{ secrets." + prefix + "_AWS_ROLE_ARN }}",
                "DEPLOYMENT_AUTH_INVALID")
        steps = job.get("steps", [])
        preflights = [(i, s) for i, s in enumerate(steps) if s.get("id") == "preflight"]
        require(len(preflights) == 1, "DEPLOYMENT_AUTH_INVALID")
        before, preflight = preflights[0]
        command = preflight.get("run", "")
        require("scripts/deployment_preflight.py" in command and "--environment " + environment in command
                and '--github-output "$GITHUB_OUTPUT"' in command
                and not any(k in preflight for k in ("if", "continue-on-error")), "DEPLOYMENT_AUTH_INVALID")
        credentials = [(i, s) for i, s in enumerate(steps) if s.get("uses", "").startswith(AWS + "@")]
        require(len(credentials) == 2 and all(i > before for i, _ in credentials), "DEPLOYMENT_AUTH_INVALID")
        oidc, keys = [s for _, s in credentials]
        region = "${{ steps.preflight.outputs.aws_region }}"
        require(oidc.get("if") == "env.AWS_ROLE_ARN != ''" and keys.get("if") == "env.AWS_ROLE_ARN == ''",
                "DEPLOYMENT_AUTH_INVALID")
        require(oidc.get("with") == {"role-to-assume": "${{ env.AWS_ROLE_ARN }}",
                                    "role-session-name": "arc-deploy-" + environment, "aws-region": region},
                "DEPLOYMENT_AUTH_INVALID")
        require(keys.get("with") == {"aws-access-key-id": "${{ secrets." + prefix + "_AWS_ACCESS_KEY_ID }}",
                                    "aws-secret-access-key": "${{ secrets." + prefix + "_AWS_SECRET_ACCESS_KEY }}",
                                    "aws-region": region}, "DEPLOYMENT_AUTH_INVALID")
        require(not any("continue-on-error" in s for _, s in credentials), "DEPLOYMENT_AUTH_INVALID")


def validate_workflows(base, deployment, ci):
    # No equality requirement between ALL deployment refs and the smoke refs.
    old_steps, current_steps = action_steps(base), action_steps(deployment)
    require(base.get("env") == deployment.get("env"), "DEPLOYMENT_AUTH_INVALID")
    # A textual mention of preflight is not evidence it runs (e.g. echo ...).
    # Preserve its executable body and bindings against the PR base as well.
    for name, job in deployment.get("jobs", {}).items():
        old_job = base.get("jobs", {}).get(name, {})
        require(old_job.get("env") == job.get("env"), "DEPLOYMENT_AUTH_INVALID")
        old_preflight = [s for s in old_job.get("steps", []) if s.get("id") == "preflight"]
        new_preflight = [s for s in job.get("steps", []) if s.get("id") == "preflight"]
        require(len(old_preflight) == len(new_preflight) == 1, "DEPLOYMENT_AUTH_INVALID")
        for key in ("run", "env", "id", "if", "continue-on-error", "shell", "working-directory"):
            require(old_preflight[0].get(key) == new_preflight[0].get(key), "DEPLOYMENT_AUTH_INVALID")
        # The AWS action also translates env variables such as ROLE_CHAINING
        # into inputs. Comparing only `with` would miss an auth-path change.
        old_auth = [s for s in old_job.get("steps", []) if s.get("uses", "").startswith(AWS + "@")]
        new_auth = [s for s in job.get("steps", []) if s.get("uses", "").startswith(AWS + "@")]
        require(len(old_auth) == len(new_auth) == 2, "DEPLOYMENT_AUTH_INVALID")
        require(all(old.get("env") == new.get("env") for old, new in zip(old_auth, new_auth)),
                "DEPLOYMENT_AUTH_INVALID")
    require(ci.get("permissions") == {"contents": "read"}, "CI_BOUNDARY_INVALID")
    triggers = ci.get("on")
    require(isinstance(triggers, dict) and set(triggers) == {"pull_request"}, "CI_BOUNDARY_INVALID")
    require(triggers["pull_request"] == {"types": ["opened", "synchronize", "reopened"]}, "CI_BOUNDARY_INVALID")
    require(not re.search(r"\bsecrets\s*(?:\.|\[)", json.dumps(ci), re.I), "CI_BOUNDARY_INVALID")
    require(ci.get("concurrency", {}).get("cancel-in-progress") is True
            and "github.event.pull_request.number" in ci.get("concurrency", {}).get("group", ""),
            "CI_BOUNDARY_INVALID")
    require(set(ci.get("jobs", {})) == {"validate"}, "SMOKE_STRUCTURE_INVALID")
    job = ci["jobs"]["validate"]
    require(isinstance(job, dict), "SMOKE_STRUCTURE_INVALID")
    require(job.get("runs-on") == "ubuntu-latest" and type(job.get("timeout-minutes")) is int
            and 1 <= job["timeout-minutes"] <= 15, "CI_BOUNDARY_INVALID")
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
    smoke = {s["uses"] for s in action_steps(ci)}
    require(smoke == {checkout["uses"], python["uses"]}, "SMOKE_STRUCTURE_INVALID")
    introduced = {}
    for repo in OWNERS:
        before = {s["uses"] for s in old_steps if action_ref(s["uses"])[0] == repo}
        after = {s["uses"] for s in current_steps if action_ref(s["uses"])[0] == repo}
        introduced[repo] = sorted(after - before)
        if repo in (CHECKOUT, PYTHON):
            require(after - before <= smoke, "SMOKE_REF_UNCOVERED")
    validate_deployment(deployment)
    return {"introduced": introduced, "smoke_refs": sorted(smoke),
            "contract_refs": sorted({s["uses"] for s in current_steps} | smoke)}


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


def syntax_check(root):
    shell_files = ("scripts/deploy_arc_lambda.sh", "scripts/deploy_arc_api_gateway.sh")
    python_files = ("scripts/deployment_preflight.py", "scripts/build_mock_artifact.py",
                    "scripts/validate_actions.py", "scripts/install_actionlint.py", "scripts/run_actions_regression.py")
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
    base = load_yaml(git_text(root, "show", args.base_sha + ":" + DEPLOYMENT))
    deployment, ci = (load_yaml((root / name).read_text()) for name in (DEPLOYMENT, VALIDATION))
    report = validate_workflows(base, deployment, ci)
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
    # The AWS candidate is checked against the CURRENT four authentication uses,
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
                                 for name in (DEPLOYMENT, VALIDATION, MANIFEST, "requirements.txt", "requirements-ci.txt")},
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
    require(len(refs) == 2 and {action_ref(ref)[0] for ref in refs} == {CHECKOUT, PYTHON}, "SMOKE_STRUCTURE_INVALID")
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

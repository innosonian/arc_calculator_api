"""Offline counterexamples for the PR action validator; no action or AWS runs."""

from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
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


def smoke_job(ci):
    assert len(ci["jobs"]) == 1, "the proposed minimal CI has one smoke job"
    return next(iter(ci["jobs"].values()))


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

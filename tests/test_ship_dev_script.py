"""scripts/ship_dev.sh (D140) against local stand-ins for `gh` and `git`.

Nothing here reaches GitHub or a remote: a temporary git repository, a fake
`gh` that answers from a state folder, and a `git` wrapper that passes every
local command to the real git but never runs `push` or `pull`. The subprocess
is only ever bash running those local files.
"""

import json
import os
from pathlib import Path
import shutil
import stat
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/ship_dev.sh"
REAL_GIT = shutil.which("git")
BASH = shutil.which("bash")
MERGE_SHA = "b" * 40
PRIVATE_TOKEN = "fixture-token-value"
EXCLUDE = ":(exclude).claude/worktrees"

FAKE_GH = r'''#!/bin/bash
# Test double for the GitHub CLI: records its arguments and answers from $SHIP_FAKE_STATE.
state="$SHIP_FAKE_STATE"
{ printf 'gh'; for argument in "$@"; do printf '\t%s' "$argument"; done; printf '\n'; } >> "$state/calls.log"
# nth <name> <default>: this call's line of $state/<name> (the last line repeats); the default without the file.
# Shell builtins only, so a call costs no extra process.
nth() {
  file="$state/$1"
  if [ ! -f "$file" ]; then printf '%s\n' "$2"; return; fi
  count=0
  if [ -f "$file.count" ]; then read -r count < "$file.count"; fi
  count=$((count + 1))
  printf '%s\n' "$count" > "$file.count"
  index=0
  value=""
  while IFS= read -r line; do
    index=$((index + 1))
    value="$line"
    if [ "$index" -eq "$count" ]; then break; fi
  done < "$file"
  printf '%s\n' "$value"
}
case "$1 $2" in
  "repo view") echo "fixture-owner/fixture-repo" ;;
  "pr list") if [ -f "$state/pr_open" ]; then echo 7; fi ;;
  "pr create") : > "$state/pr_open"; echo "https://github.invalid/fixture-owner/fixture-repo/pull/7" ;;
  "pr view")
    case "$*" in
      *headRefOid*) if [ -f "$state/pr_head" ]; then nth pr_head ""; else "$SHIP_REAL_GIT" rev-parse HEAD; fi ;;
      *"statusCheckRollup | length"*) nth rollup_length 3 ;;
      *statusCheckRollup*)
        if [ -f "$state/failed_checks" ] && [ ! -f "$state/rerun_done" ]; then cat "$state/failed_checks"; fi ;;
      *mergeCommit*) nth merge_sha "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb" ;;
      *state*) nth pr_state OPEN ;;
      *) exit 97 ;;
    esac ;;
  "pr checks") exit "$(nth checks_exit 0)" ;;
  "pr merge") exit "$(nth merge_exit 0)" ;;
  "run list")
    case "$*" in
      *conclusion*) nth latest_run "" ;;
      *) nth run_for_commit 9001 ;;
    esac ;;
  "run view")
    case "$*" in
      *--log-failed*) if [ -f "$state/failed_log" ]; then cat "$state/failed_log"; fi ;;
      *jobs*) printf 'job deploy-dev: %s\n  success\tCheckout\n' "$(nth job_conclusion success)" ;;
      *status*) nth run_status waiting ;;
      *) exit 97 ;;
    esac ;;
  "run watch") exit "$(nth watch_exit 0)" ;;
  "run rerun") : > "$state/rerun_done"; exit "$(nth rerun_exit 0)" ;;
  "api repos/fixture-owner/fixture-repo/environments") nth environment_id 4242 ;;
  "api -X")
    cat > "$state/approval_body"
    echo '{"fixture": "approval response body that the script must not print"}'
    exit "$(nth approve_exit 0)" ;;
  *) echo "unexpected gh call: $*" >&2; exit 97 ;;
esac
'''

FAKE_GIT = r'''#!/bin/bash
# Test double: every local git command runs for real; nothing is pushed or pulled.
state="$SHIP_FAKE_STATE"
{ printf 'git'; for argument in "$@"; do printf '\t%s' "$argument"; done; printf '\n'; } >> "$state/calls.log"
case "$1" in
  push)
    code=0
    if [ -f "$state/push_exit" ]; then read -r code < "$state/push_exit"; fi
    exit "$code" ;;
  pull) exit 0 ;;
esac
exec "$SHIP_REAL_GIT" "$@"
'''


class Workspace:
    def __init__(self, tmp_path):
        self.repo = tmp_path / "repo"
        self.state = tmp_path / "state"
        self.bin = tmp_path / "bin"
        for folder in (self.repo, self.state, self.bin, tmp_path / "home"):
            folder.mkdir()
        for name, body in (("gh", FAKE_GH), ("git", FAKE_GIT)):
            path = self.bin / name
            path.write_text(body, encoding="utf-8")
            path.chmod(path.stat().st_mode | stat.S_IXUSR)
        self.environment = {
            "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
            "HOME": str(tmp_path / "home"), "TMPDIR": str(tmp_path),
            "SHIP_FAKE_STATE": str(self.state), "SHIP_REAL_GIT": REAL_GIT,
            "SHIP_DEV_POLL_SECONDS": "0", "SHIP_DEV_WAIT_SECONDS": "0",
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
            # A login the script must never read or print.
            "GH_TOKEN": PRIVATE_TOKEN, "GITHUB_TOKEN": PRIVATE_TOKEN,
        }
        self.git("init", "-q", ".")
        self.git("symbolic-ref", "HEAD", "refs/heads/develop")
        (self.repo / "README.md").write_text("fixture\n", encoding="utf-8")
        self.git("add", "README.md")
        self.git("commit", "-q", "-m", "initial")

    def git(self, *arguments):
        completed = subprocess.run([REAL_GIT, *arguments], cwd=self.repo, env=self.environment,
                                   capture_output=True, text=True, timeout=30)
        assert completed.returncode == 0, completed.stderr
        return completed.stdout.strip()

    def work_branch(self, name="feature/fixture", *, change=True):
        self.git("checkout", "-q", "-b", name)
        if change:
            (self.repo / "change.txt").write_text("change\n", encoding="utf-8")
        return name

    def set(self, name, *lines):
        (self.state / name).write_text("".join(str(line) + "\n" for line in lines), encoding="utf-8")

    def run(self, *arguments, cwd=None, **environment):
        completed = subprocess.run([BASH, str(SCRIPT), *arguments], cwd=cwd or self.repo,
                                   env={**self.environment, **environment}, capture_output=True, text=True,
                                   timeout=60, stdin=subprocess.DEVNULL)
        visible = completed.stdout + completed.stderr
        assert PRIVATE_TOKEN not in visible, "no token in the output"
        assert "approval response body" not in visible, "API response bodies are not printed"
        assert "unexpected gh call" not in visible, visible
        return completed.returncode, visible

    def calls(self):
        log = self.state / "calls.log"
        return [line.split("\t") for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []

    def gh_calls(self):
        return [call[1:] for call in self.calls() if call[0] == "gh"]

    def git_calls(self):
        return [call[1:] for call in self.calls() if call[0] == "git"]

    def milestones(self):
        """The side-effect commands in the order they ran."""
        names = {("git", "add"): "add", ("git", "commit"): "commit", ("git", "push"): "push",
                 ("gh", "pr", "create"): "pr_create", ("gh", "pr", "checks"): "checks", ("gh", "pr", "merge"): "merge",
                 ("gh", "run", "rerun"): "rerun", ("gh", "api", "-X"): "approve", ("gh", "run", "watch"): "watch",
                 ("git", "checkout"): "checkout", ("git", "pull"): "pull"}
        result = []
        for call in self.calls():
            for prefix, name in names.items():
                if tuple(call[:len(prefix)]) == prefix:
                    result.append(name)
        return result


@pytest.fixture
def workspace(tmp_path):
    assert REAL_GIT and BASH, "git and bash are required by the script itself"
    return Workspace(tmp_path)


# --- static -----------------------------------------------------------------

def test_script_is_executable_bash_with_valid_syntax():
    assert SCRIPT.read_text(encoding="utf-8").startswith("#!/usr/bin/env bash\n")
    assert os.access(SCRIPT, os.X_OK)
    completed = subprocess.run([BASH, "-n", str(SCRIPT)], capture_output=True, text=True, timeout=15)
    assert completed.returncode == 0, completed.stderr


def test_script_uses_only_git_and_gh_and_never_forces_or_reads_a_token():
    text = SCRIPT.read_text(encoding="utf-8")
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert "set -euo pipefail" in code
    for forbidden in ("aws ", "boto", "curl ", "--force", "push -f", "+HEAD", "gh auth token", "auth status -t",
                      "GH_TOKEN", "GITHUB_TOKEN", "set -x", "--admin", "printenv"):
        assert forbidden not in code, forbidden
    # The base branch is a constant; no option or argument can change it.
    assert 'BASE_BRANCH="develop"' in code and code.count("BASE_BRANCH=") == 1
    assert 'gh pr create --base "$BASE_BRANCH" --head "$branch" --fill' in code
    pushes = [line.strip() for line in code.splitlines() if line.strip().startswith("git push")]
    assert pushes == ['git push -u origin "$branch" ||'], "the only push names the work branch and no force flag"


def test_help_exits_zero_and_describes_every_option(workspace):
    code, output = workspace.run("--help")
    assert code == 0
    for text in ("--no-approve", "--rerun-failed-once", "--redeploy", "--help", "develop", "force push"):
        assert text in output
    assert workspace.calls() == [], "help needs neither git nor gh"


@pytest.mark.parametrize("arguments", [("--force",), ("first message", "second message"), ("--redeploy", "message"),
                                       ("--redeploy", "--rerun-failed-once")])
def test_unknown_or_conflicting_arguments_are_refused_before_any_command(workspace, arguments):
    workspace.work_branch()
    code, output = workspace.run(*arguments)
    assert code == 2 and "ship_dev:" in output
    assert workspace.calls() == []


# --- refusals ---------------------------------------------------------------

@pytest.mark.parametrize("branch", ["develop", "master", "main"])
def test_a_protected_branch_is_refused_before_anything_is_committed_or_pushed(workspace, branch):
    if branch != "develop":
        workspace.git("checkout", "-q", "-b", branch)
    (workspace.repo / "change.txt").write_text("change\n", encoding="utf-8")
    code, output = workspace.run("fixture message")
    assert code == 2 and "작업 브랜치에서 실행" in output and "git checkout -b" in output
    assert workspace.milestones() == [] and workspace.gh_calls() == []
    assert workspace.git("status", "--porcelain") == "?? change.txt", "nothing was staged or committed"


def test_a_detached_head_is_refused(workspace):
    workspace.git("checkout", "-q", "--detach")
    code, output = workspace.run("fixture message")
    assert code == 2 and "detached HEAD" in output and workspace.milestones() == []


def test_changes_without_a_commit_message_are_refused(workspace):
    workspace.work_branch()
    before = workspace.git("rev-parse", "HEAD")
    code, output = workspace.run()
    assert code == 2 and "커밋 메시지가 없습니다" in output
    assert workspace.milestones() == [] and workspace.gh_calls() == []
    assert workspace.git("rev-parse", "HEAD") == before and workspace.git("status", "--porcelain") == "?? change.txt"
    # An empty message is no message.
    code, _ = workspace.run("")
    assert code == 2 and workspace.milestones() == []


# --- the whole flow ---------------------------------------------------------

def test_full_flow_commits_pushes_opens_the_pr_checks_merges_approves_and_watches(workspace):
    branch = workspace.work_branch()
    code, output = workspace.run("fixture: ship it")
    assert code == 0, output
    assert workspace.milestones() == ["add", "commit", "push", "pr_create", "checks", "merge", "approve", "watch",
                                      "checkout", "pull"]
    head = workspace.git("rev-parse", branch)
    assert workspace.git("log", "-1", "--format=%s", branch) == "fixture: ship it"
    assert workspace.git("show", "--name-only", "--format=", branch) == "change.txt"
    assert workspace.git_calls() == [
        ["rev-parse", "--show-toplevel"], ["symbolic-ref", "--quiet", "--short", "HEAD"],
        ["status", "--porcelain", "--", ".", EXCLUDE], ["add", "-A", "--", ".", EXCLUDE],
        ["status", "--short", "--", ".", EXCLUDE], ["commit", "-m", "fixture: ship it"], ["rev-parse", "HEAD"],
        ["push", "-u", "origin", branch], ["checkout", "develop"], ["pull", "--ff-only", "origin", "develop"]]
    calls = workspace.gh_calls()
    assert [call[:2] for call in calls] == [
        ["repo", "view"], ["pr", "list"], ["pr", "create"], ["pr", "list"], ["pr", "view"], ["pr", "view"],
        ["pr", "checks"], ["pr", "merge"], ["pr", "view"], ["run", "list"], ["run", "view"], ["api", "repos/fixture-owner/fixture-repo/environments"],
        ["api", "-X"], ["run", "watch"], ["run", "view"]]
    assert calls[0] == ["repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"]
    assert calls[1][:9] == ["pr", "list", "--head", branch, "--base", "develop", "--state", "open", "--json"]
    assert calls[2] == ["pr", "create", "--base", "develop", "--head", branch, "--fill"]
    assert calls[6] == ["pr", "checks", "7", "--watch", "--fail-fast"]
    # Only the commit that was checked is merged, as a merge commit, never with --admin.
    assert calls[7] == ["pr", "merge", "7", "--merge", "--delete-branch", "--match-head-commit", head]
    assert calls[9][:6] == ["run", "list", "--workflow", "deploy_dev.yml", "--branch", "develop"] and MERGE_SHA in calls[9][-1]
    assert calls[10][:3] == ["run", "view", "9001"]
    assert calls[11][2:] == ["--jq", '.environments[] | select(.name == "development") | .id']
    assert calls[12] == ["api", "-X", "POST", "repos/fixture-owner/fixture-repo/actions/runs/9001/pending_deployments",
                         "--input", "-"]
    assert json.loads((workspace.state / "approval_body").read_text()) == {
        "environment_ids": [4242], "state": "approved", "comment": "Approved from scripts/ship_dev.sh"}
    assert calls[13] == ["run", "watch", "9001", "--exit-status"]
    assert "job deploy-dev: success" in output and "Deploy Dev 성공" in output
    assert workspace.git("symbolic-ref", "--short", "HEAD") == "develop"
    assert not any("auth" in call for call in calls)


def test_no_approve_never_calls_the_approval_api_but_still_watches(workspace):
    workspace.work_branch()
    code, output = workspace.run("--no-approve", "fixture message")
    assert code == 0, output
    assert workspace.milestones() == ["add", "commit", "push", "pr_create", "checks", "merge", "watch", "checkout", "pull"]
    assert not [call for call in workspace.gh_calls() if call[0] == "api"]
    assert not (workspace.state / "approval_body").exists()
    assert "--no-approve" in output and "gh run view 9001 --web" in output


def test_without_changes_the_commit_is_skipped_and_an_open_pr_is_reused(workspace):
    branch = workspace.work_branch(change=False)
    (workspace.state / "pr_open").write_text("")
    before = workspace.git("rev-parse", "HEAD")
    code, output = workspace.run()
    assert code == 0, output
    assert workspace.milestones() == ["push", "checks", "merge", "approve", "watch", "checkout", "pull"]
    assert workspace.git("rev-parse", branch) == before and "커밋할 변경이 없습니다" in output


def test_local_agent_worktrees_are_never_committed_or_counted_as_a_change(workspace):
    branch = workspace.work_branch(change=False)
    nested = workspace.repo / ".claude/worktrees/fixture"
    nested.mkdir(parents=True)
    (nested / "file.txt").write_text("another checkout\n", encoding="utf-8")
    # Only the worktree folder is untracked: no message is needed and nothing is committed.
    before = workspace.git("rev-parse", "HEAD")
    code, output = workspace.run()
    assert code == 0, output
    assert "add" not in workspace.milestones() and workspace.git("rev-parse", branch) == before
    # With a real change next to it, only the real change is committed.
    workspace.git("checkout", "-q", branch)
    (workspace.repo / "change.txt").write_text("change\n", encoding="utf-8")
    (workspace.repo / ".claude/settings.json").write_text("{}\n", encoding="utf-8")
    code, output = workspace.run("fixture message")
    assert code == 0, output
    assert sorted(workspace.git("show", "--name-only", "--format=", branch).splitlines()) == [".claude/settings.json", "change.txt"]
    assert (nested / "file.txt").exists()


def test_the_script_works_from_a_subdirectory(workspace):
    branch = workspace.work_branch()
    (workspace.repo / "docs").mkdir()
    (workspace.repo / "docs/note.md").write_text("note\n", encoding="utf-8")
    code, output = workspace.run("fixture message", cwd=workspace.repo / "docs")
    assert code == 0, output
    assert sorted(workspace.git("show", "--name-only", "--format=", branch).splitlines()) == ["change.txt", "docs/note.md"]


def test_a_rejected_push_stops_without_a_pr_and_is_never_forced(workspace):
    workspace.work_branch()
    workspace.set("push_exit", 1)
    code, output = workspace.run("fixture message")
    assert code == 1 and "git push에 실패" in output and "force push는 하지 않습니다" in output
    assert workspace.milestones() == ["add", "commit", "push"] and workspace.gh_calls() == []
    assert [call for call in workspace.git_calls() if call[0] == "push"] == [["push", "-u", "origin", "feature/fixture"]]


# --- polling ----------------------------------------------------------------

def test_the_script_waits_for_the_pr_head_the_checks_the_run_and_the_approval_state(workspace):
    branch = workspace.work_branch()
    workspace.set("rollup_length", 0, 0, 3)
    workspace.set("run_for_commit", "", "", 9002)
    workspace.set("run_status", "queued", "queued", "waiting")
    code, output = workspace.run("fixture message", SHIP_DEV_WAIT_SECONDS="60")
    assert code == 0, output
    calls = workspace.gh_calls()
    assert len([call for call in calls if call[:2] == ["run", "list"]]) == 3
    assert len([call for call in calls if call[:3] == ["run", "view", "9002"] and "status" in call]) == 3
    assert ["run", "watch", "9002", "--exit-status"] in calls
    assert workspace.milestones().index("checks") > workspace.milestones().index("pr_create")


def test_a_pr_head_that_is_not_the_pushed_commit_is_never_checked_or_merged(workspace):
    workspace.work_branch()
    workspace.set("pr_head", "c" * 40)
    code, output = workspace.run("fixture message")
    assert code == 5 and "헤드" in output
    assert workspace.milestones() == ["add", "commit", "push", "pr_create"]


def test_a_missing_deploy_run_times_out_with_the_commands_to_continue(workspace):
    workspace.work_branch()
    workspace.set("run_for_commit", "")
    code, output = workspace.run("fixture message")
    assert code == 5 and "gh run list --workflow deploy_dev.yml --branch develop" in output
    assert workspace.milestones() == ["add", "commit", "push", "pr_create", "checks", "merge"]


def test_a_run_that_needs_no_approval_is_watched_without_an_approval_call(workspace):
    workspace.work_branch()
    workspace.set("run_status", "in_progress")
    code, output = workspace.run("fixture message")
    assert code == 0, output
    assert "approve" not in workspace.milestones() and "watch" in workspace.milestones()


def test_a_refused_approval_stops_with_the_manual_commands(workspace):
    workspace.work_branch()
    workspace.set("approve_exit", 1)
    code, output = workspace.run("fixture message")
    assert code == 1 and "승인에 실패" in output and "gh run watch 9001 --exit-status" in output
    assert workspace.milestones()[-1] == "approve"


# --- failing checks ---------------------------------------------------------

FAILED_CHECKS = ("validate\thttps://github.invalid/fixture-owner/fixture-repo/actions/runs/5001/job/1",
                 "boundary\thttps://github.invalid/fixture-owner/fixture-repo/actions/runs/5001/job/2")


def test_failed_checks_stop_before_the_merge_and_name_the_rerun_command(workspace):
    workspace.work_branch()
    workspace.set("checks_exit", 1)
    workspace.set("failed_checks", *FAILED_CHECKS)
    code, output = workspace.run("fixture message")
    assert code == 3
    assert "- validate" in output and "- boundary" in output
    assert output.count("gh run rerun 5001 --failed") == 1, "one hint per run, not per job"
    assert workspace.milestones() == ["add", "commit", "push", "pr_create", "checks"], "no automatic rerun, no merge"


def test_rerun_failed_once_reruns_a_single_time_and_then_merges(workspace):
    workspace.work_branch()
    workspace.set("checks_exit", 1, 0)
    workspace.set("failed_checks", *FAILED_CHECKS)
    code, output = workspace.run("--rerun-failed-once", "fixture message")
    assert code == 0, output
    assert workspace.milestones() == ["add", "commit", "push", "pr_create", "checks", "rerun", "checks", "merge",
                                      "approve", "watch", "checkout", "pull"]
    assert [call for call in workspace.gh_calls() if call[:2] == ["run", "rerun"]] == [["run", "rerun", "5001", "--failed"]]


def test_rerun_failed_once_does_not_rerun_a_second_time(workspace):
    workspace.work_branch()
    workspace.set("checks_exit", 1, 1)
    workspace.set("failed_checks", *FAILED_CHECKS)
    code, output = workspace.run("--rerun-failed-once", "fixture message")
    assert code == 3 and "다시 실행한 뒤에도 실패" in output
    assert workspace.milestones() == ["add", "commit", "push", "pr_create", "checks", "rerun", "checks"]


# --- merge and deployment results -------------------------------------------

def test_a_merge_that_did_not_happen_stops_before_any_deployment_step(workspace):
    workspace.work_branch()
    workspace.set("merge_exit", 1)
    code, output = workspace.run("fixture message")
    assert code == 1 and "머지에 실패" in output
    assert workspace.milestones() == ["add", "commit", "push", "pr_create", "checks", "merge"]


def test_a_merge_that_succeeded_remotely_continues_when_only_the_local_cleanup_failed(workspace):
    workspace.work_branch()
    workspace.set("merge_exit", 1)
    workspace.set("pr_state", "MERGED")
    code, output = workspace.run("fixture message")
    assert code == 0, output
    assert workspace.milestones()[-5:] == ["merge", "approve", "watch", "checkout", "pull"]


def test_a_failed_deployment_prints_the_step_results_and_only_the_fixed_code_line(workspace):
    workspace.work_branch()
    workspace.set("watch_exit", 1)
    workspace.set("job_conclusion", "failure")
    (workspace.state / "failed_log").write_text(
        "deploy-dev\tDeploy\t2026-09-30T00:00:00Z some earlier output\n"
        'deploy-dev\tCheck\t2026-09-30T00:00:01Z {"code": "CONFIG_DRIFT", "expected": "valid", '
        '"field": "AwsSettings.parse", "role": "worker"}\n'
        "deploy-dev\tCheck\t2026-09-30T00:00:02Z ##[error]Process completed with exit code 2.\n", encoding="utf-8")
    code, output = workspace.run("fixture message")
    assert code == 4
    assert "job deploy-dev: failure" in output
    assert ('실패 단계의 마지막 코드 줄: {"code": "CONFIG_DRIFT", "expected": "valid", '
            '"field": "AwsSettings.parse", "role": "worker"}') in output
    assert "some earlier output" not in output and "scripts/ship_dev.sh --redeploy" in output
    assert workspace.milestones()[-1] == "watch", "develop is not checked out after a failed deployment"


# --- --redeploy -------------------------------------------------------------

def test_redeploy_reruns_the_latest_failed_run_approves_and_watches(workspace):
    workspace.set("latest_run", "9100 completed failure")
    # The first status read is still the finished attempt; the re-run then waits for approval.
    workspace.set("run_status", "completed", "queued", "waiting")
    code, output = workspace.run("--redeploy", SHIP_DEV_WAIT_SECONDS="60")
    assert code == 0, output
    assert workspace.milestones() == ["rerun", "approve", "watch"]
    calls = workspace.gh_calls()
    assert calls[1][:8] == ["run", "list", "--workflow", "deploy_dev.yml", "--branch", "develop", "--limit", "1"]
    assert ["run", "rerun", "9100", "--failed"] in calls and ["run", "watch", "9100", "--exit-status"] in calls
    assert workspace.git_calls() == [], "no commit, push or checkout"


@pytest.mark.parametrize("latest", ["9100 completed success", "9100 completed skipped"])
def test_redeploy_refuses_when_the_latest_run_did_not_fail(workspace, latest):
    # Re-running an older failed run would put older code over a newer deployment.
    workspace.set("latest_run", latest)
    code, output = workspace.run("--redeploy")
    assert code == 2 and "실패가 아닙니다" in output and workspace.milestones() == []


def test_redeploy_without_any_run_is_refused(workspace):
    workspace.set("latest_run", "")
    code, output = workspace.run("--redeploy")
    assert code == 2 and workspace.milestones() == []


def test_redeploy_resumes_a_run_that_is_still_waiting_without_rerunning_it(workspace):
    workspace.set("latest_run", "9100 waiting ")
    code, output = workspace.run("--redeploy")
    assert code == 0, output
    assert workspace.milestones() == ["approve", "watch"]


def test_redeploy_with_no_approve_only_reruns_and_watches(workspace):
    workspace.set("latest_run", "9100 completed failure")
    code, output = workspace.run("--redeploy", "--no-approve")
    assert code == 0, output
    assert workspace.milestones() == ["rerun", "watch"]


def test_redeploy_reports_a_deployment_that_failed_again(workspace):
    workspace.set("latest_run", "9100 completed failure")
    workspace.set("watch_exit", 1)
    code, output = workspace.run("--redeploy")
    assert code == 4 and "gh run view 9100 --log-failed" in output

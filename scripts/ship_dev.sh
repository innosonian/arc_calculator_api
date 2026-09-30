#!/usr/bin/env bash
# D140: ship the current work branch to Dev from the terminal, with `git` and
# `gh` only (no AWS call, no browser):
#
#   commit + push -> PR to develop -> PR checks -> merge -> approve the
#   `development` environment of the `Deploy Dev` run -> watch the deployment.
#
# The base branch is always `develop`. Nothing is force-pushed. No token or
# secret is read or printed: `gh` uses its own stored login, and this script
# never calls `gh auth`. Works with the bash 3.2 of macOS.
#
# Exit codes: 0 deployed (or nothing left to do), 1 a git/gh step failed,
# 2 refused (usage, protected branch, missing commit message), 3 PR checks
# failed, 4 the Deploy Dev run failed, 5 timed out waiting for GitHub.
set -euo pipefail

BASE_BRANCH="develop"
PROTECTED_BRANCHES="develop master main"
WORKFLOW_FILE="deploy_dev.yml"
WORKFLOW_NAME="Deploy Dev"
ENVIRONMENT_NAME="development"
# Test hooks only (seconds between polls, how long to wait for GitHub to show
# a PR head, its checks, the run and its approval state).
POLL_SECONDS="${SHIP_DEV_POLL_SECONDS:-10}"
WAIT_SECONDS="${SHIP_DEV_WAIT_SECONDS:-180}"

usage() {
  cat <<'EOF'
사용법:
  scripts/ship_dev.sh [--no-approve] [--rerun-failed-once] "<커밋 메시지>"
  scripts/ship_dev.sh --redeploy [--no-approve]
  scripts/ship_dev.sh --help

작업 브랜치에서 실행하면 다음을 차례로 수행한다(git + gh만 사용, AWS 호출 없음).
  1. 변경이 있으면 git add -A + commit(메시지 필수), git push -u origin <브랜치>
  2. develop을 향한 열린 PR이 없으면 gh pr create --base develop --fill
  3. PR 검사 대기(gh pr checks --watch --fail-fast)
  4. gh pr merge --merge --delete-branch (검사한 커밋과 같을 때만)
  5. 머지 커밋의 Deploy Dev 실행을 찾아 environment `development` 승인
  6. gh run watch 로 배포를 지켜보고 단계별 결과를 출력, 성공하면 develop으로 돌아와 pull

옵션:
  --no-approve          5번의 승인을 하지 않는다(다른 승인자가 승인하면 계속 지켜본다).
  --rerun-failed-once   PR 검사가 실패하면 실패한 job만 한 번 다시 실행해 본다.
  --redeploy            코드 변경 없이, develop의 가장 최근 Deploy Dev 실행이 실패 상태일 때
                        그 실행의 실패한 job을 다시 실행 -> 승인 -> 지켜본다
                        (예: 배포 역할 권한을 고친 뒤). 가장 최근 실행이 성공이면 거절한다.
  --help                이 도움말.

종료 코드: 0 완료, 1 git/gh 단계 실패, 2 거절(사용법·보호 브랜치·커밋 메시지 없음),
          3 PR 검사 실패, 4 Deploy Dev 실패, 5 GitHub 대기 시간 초과.
base 브랜치는 항상 develop이며 force push를 하지 않는다. 토큰·비밀값을 읽거나 출력하지 않는다.
EOF
}

say() { printf '%s\n' "$*"; }
step() { printf '\n==> %s\n' "$*"; }

# die <exit code> <message> [next command or hint]...
die() {
  local code="$1"
  shift
  printf 'ship_dev: %s\n' "$1" >&2
  shift
  while [ $# -gt 0 ]; do
    printf '  %s\n' "$1" >&2
    shift
  done
  exit "$code"
}

is_number() {
  case "$1" in
    '' | *[!0-9]*) return 1 ;;
    *) return 0 ;;
  esac
}

is_sha() {
  [ "${#1}" -eq 40 ] || return 1
  case "$1" in
    *[!0-9a-f]*) return 1 ;;
    *) return 0 ;;
  esac
}

# poll_until <command> [args...]: run the command every POLL_SECONDS until it
# succeeds (0) or WAIT_SECONDS passed (1). The first attempt is immediate.
poll_until() {
  local deadline=$((SECONDS + WAIT_SECONDS))
  while :; do
    if "$@"; then
      return 0
    fi
    if [ "$SECONDS" -ge "$deadline" ]; then
      return 1
    fi
    sleep "$POLL_SECONDS"
  done
}

# --- arguments --------------------------------------------------------------

approve=1
rerun_once=0
redeploy=0
message=""
have_message=0
while [ $# -gt 0 ]; do
  case "$1" in
    --help | -h)
      usage
      exit 0
      ;;
    --no-approve) approve=0 ;;
    --rerun-failed-once) rerun_once=1 ;;
    --redeploy) redeploy=1 ;;
    -*)
      die 2 "알 수 없는 옵션입니다: $1" "scripts/ship_dev.sh --help"
      ;;
    *)
      if [ "$have_message" -eq 1 ]; then
        die 2 "커밋 메시지는 따옴표로 묶은 인자 하나여야 합니다." 'scripts/ship_dev.sh "<커밋 메시지>"'
      fi
      message="$1"
      have_message=1
      ;;
  esac
  shift
done
if [ "$redeploy" -eq 1 ] && { [ "$have_message" -eq 1 ] || [ "$rerun_once" -eq 1 ]; }; then
  die 2 "--redeploy는 커밋 메시지나 --rerun-failed-once와 함께 쓰지 않습니다." "scripts/ship_dev.sh --redeploy"
fi
if ! is_number "$POLL_SECONDS" || ! is_number "$WAIT_SECONDS"; then
  die 2 "SHIP_DEV_POLL_SECONDS와 SHIP_DEV_WAIT_SECONDS는 0 이상의 정수여야 합니다."
fi
for tool in git gh; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    die 2 "$tool 명령을 찾을 수 없습니다." "설치 후 다시 실행하세요(gh는 'gh auth login'으로 로그인)."
  fi
done

# --- shared: repository, approval, watch ------------------------------------

repo=""
run_id=""
run_status=""

resolve_repository() {
  repo="$(gh repo view --json nameWithOwner --jq .nameWithOwner)" ||
    die 1 "저장소 정보를 읽지 못했습니다." "gh auth status 로 로그인 상태를 확인하세요."
  case "$repo" in
    */*/* | /* | */ | *[!A-Za-z0-9_./-]*) die 1 "저장소 이름 형식이 예상과 다릅니다." ;;
    */*) ;;
    *) die 1 "저장소 이름 형식이 예상과 다릅니다." ;;
  esac
}

# read_run_status <mode>: 0 when the run reached a state we can act on.
# `waiting` = waiting for the environment approval. A `completed` seen right
# after a re-run is the previous attempt, so only a fresh run stops on it.
read_run_status() {
  run_status="$(gh run view "$run_id" --json status --jq .status 2>/dev/null || true)"
  case "$run_status" in
    waiting | in_progress) return 0 ;;
    completed) [ "$1" = "fresh" ] ;;
    *) return 1 ;;
  esac
}

approve_run() {
  local mode="$1" environment_id
  step "Deploy Dev 실행 $run_id: 승인 대기 상태 확인"
  if ! poll_until read_run_status "$mode"; then
    die 5 "실행이 승인 대기 상태가 되지 않았습니다(마지막 상태: ${run_status:-알 수 없음})." \
      "gh run view $run_id" "gh run watch $run_id --exit-status"
  fi
  if [ "$run_status" != "waiting" ]; then
    say "승인 대기가 없습니다(상태: $run_status). 승인 없이 계속합니다."
    return 0
  fi
  if [ "$approve" -eq 0 ]; then
    say "--no-approve: 승인하지 않습니다. 승인자가 승인하면 아래 감시가 이어집니다."
    say "  브라우저에서 승인: gh run view $run_id --web"
    return 0
  fi
  environment_id="$(gh api "repos/$repo/environments" \
    --jq ".environments[] | select(.name == \"$ENVIRONMENT_NAME\") | .id")" ||
    die 1 "environment 목록을 읽지 못했습니다." "gh run view $run_id --web 에서 직접 승인하세요."
  if ! is_number "$environment_id"; then
    die 1 "environment '$ENVIRONMENT_NAME'을 찾지 못했습니다." "gh run view $run_id --web 에서 직접 승인하세요."
  fi
  printf '{"environment_ids":[%s],"state":"approved","comment":"Approved from scripts/ship_dev.sh"}' \
    "$environment_id" |
    gh api -X POST "repos/$repo/actions/runs/$run_id/pending_deployments" --input - >/dev/null ||
    die 1 "environment '$ENVIRONMENT_NAME' 승인에 실패했습니다(승인 권한이 없거나 본인 승인이 막혀 있을 수 있음)." \
      "gh run view $run_id --web 에서 승인자가 승인한 뒤: gh run watch $run_id --exit-status"
  say "environment '$ENVIRONMENT_NAME' 승인 완료."
}

watch_run() {
  local result=0 code_line
  step "Deploy Dev 실행 $run_id 감시"
  gh run watch "$run_id" --exit-status || result=$?
  say ""
  say "단계별 결과:"
  gh run view "$run_id" --json jobs \
    --jq '.jobs[] | "job \(.name): \(.conclusion // .status)", (.steps[] | "  \(.conclusion // .status)\t\(.name)")' ||
    say "  (단계 목록을 읽지 못했습니다: gh run view $run_id)"
  if [ "$result" -ne 0 ]; then
    # The deploy script prints one JSON line with a fixed code and safe fields.
    code_line="$(gh run view "$run_id" --log-failed 2>/dev/null |
      grep -oE '\{.*"code": "[A-Z][A-Z0-9_]*".*\}' | tail -n 1 || true)"
    if [ -n "$code_line" ]; then
      say "실패 단계의 마지막 코드 줄: $code_line"
    fi
    die 4 "Deploy Dev 실행이 실패했습니다." "자세한 로그: gh run view $run_id --log-failed" \
      "원인(권한·설정 등)을 고친 뒤 코드 변경 없이 다시 배포: scripts/ship_dev.sh --redeploy"
  fi
  say "Deploy Dev 성공."
}

# --- --redeploy -------------------------------------------------------------

if [ "$redeploy" -eq 1 ]; then
  resolve_repository
  step "develop의 가장 최근 $WORKFLOW_NAME 실행 확인"
  latest="$(gh run list --workflow "$WORKFLOW_FILE" --branch "$BASE_BRANCH" --limit 1 \
    --json databaseId,status,conclusion --jq '.[0] | "\(.databaseId) \(.status) \(.conclusion)"')" ||
    die 1 "실행 목록을 읽지 못했습니다." "gh run list --workflow $WORKFLOW_FILE --branch $BASE_BRANCH"
  read -r run_id latest_status latest_conclusion <<EOF
$latest
EOF
  if ! is_number "$run_id"; then
    die 2 "$BASE_BRANCH에 $WORKFLOW_NAME 실행이 없습니다." 'scripts/ship_dev.sh "<커밋 메시지>"'
  fi
  if [ "$latest_status" != "completed" ]; then
    say "가장 최근 실행 $run_id 이 아직 끝나지 않았습니다(상태: $latest_status). 다시 실행하지 않고 이어서 지켜봅니다."
    approve_run fresh
  else
    case "$latest_conclusion" in
      failure | cancelled | timed_out) ;;
      *)
        # Re-running an older failed run would put older code over a newer deployment.
        die 2 "가장 최근 실행 $run_id 의 결과가 실패가 아닙니다(결과: $latest_conclusion). 다시 배포하지 않습니다." \
          "gh run view $run_id"
        ;;
    esac
    step "실행 $run_id 의 실패한 job 다시 실행"
    gh run rerun "$run_id" --failed ||
      die 1 "다시 실행 요청에 실패했습니다." "gh run rerun $run_id --failed"
    approve_run rerun
  fi
  watch_run
  exit 0
fi

# --- 1. branch, commit, push ------------------------------------------------

top="$(git rev-parse --show-toplevel)" || die 2 "git 저장소 안에서 실행하세요."
cd "$top"
branch="$(git symbolic-ref --quiet --short HEAD)" ||
  die 2 "브랜치가 아닌 상태(detached HEAD)입니다. 작업 브랜치에서 실행하세요." "git checkout -b <작업-브랜치>"
for protected in $PROTECTED_BRANCHES; do
  if [ "$branch" = "$protected" ]; then
    die 2 "'$branch' 브랜치에서는 실행하지 않습니다. 작업 브랜치에서 실행하세요." \
      "git checkout -b <작업-브랜치>" 'scripts/ship_dev.sh "<커밋 메시지>"'
  fi
done

step "1/6 커밋과 push ($branch)"
# Everything in the working tree except local agent worktrees: those are other
# checkouts of this repository, never content of a commit.
if [ -n "$(git status --porcelain -- . ':(exclude).claude/worktrees')" ]; then
  if [ -z "$message" ]; then
    die 2 "커밋하지 않은 변경이 있는데 커밋 메시지가 없습니다." 'scripts/ship_dev.sh "<커밋 메시지>"'
  fi
  git add -A -- . ':(exclude).claude/worktrees' || die 1 "git add에 실패했습니다." "git status"
  say "커밋에 들어가는 변경:"
  git status --short -- . ':(exclude).claude/worktrees'
  git commit -m "$message" || die 1 "git commit에 실패했습니다." "git status"
else
  say "커밋할 변경이 없습니다. 이미 커밋된 내용으로 계속합니다."
fi
head_sha="$(git rev-parse HEAD)"
is_sha "$head_sha" || die 1 "현재 커밋을 읽지 못했습니다." "git log -1"
# Never forced: a rejected push stops here.
git push -u origin "$branch" ||
  die 1 "git push에 실패했습니다(force push는 하지 않습니다)." \
    "원격이 앞서 있으면: git pull --rebase origin $branch" 'scripts/ship_dev.sh "<커밋 메시지>"'

# --- 2. pull request to develop ---------------------------------------------

resolve_repository
step "2/6 PR 확인 ($branch -> $BASE_BRANCH)"
open_pr() {
  gh pr list --head "$branch" --base "$BASE_BRANCH" --state open --json number --jq '.[0].number // empty'
}
pr="$(open_pr)" || die 1 "PR 목록을 읽지 못했습니다." "gh pr list --head $branch"
if [ -z "$pr" ]; then
  gh pr create --base "$BASE_BRANCH" --head "$branch" --fill ||
    die 1 "PR 생성에 실패했습니다." "gh pr create --base $BASE_BRANCH --head $branch --fill"
  pr="$(open_pr)" || die 1 "PR 목록을 읽지 못했습니다." "gh pr list --head $branch"
fi
is_number "$pr" || die 1 "develop을 향한 열린 PR을 찾지 못했습니다." "gh pr list --head $branch --base $BASE_BRANCH"
say "PR #$pr"

# --- 3. checks --------------------------------------------------------------

pr_head_is_pushed() {
  [ "$(gh pr view "$pr" --json headRefOid --jq .headRefOid 2>/dev/null || true)" = "$head_sha" ]
}
checks_registered() {
  local count
  count="$(gh pr view "$pr" --json statusCheckRollup --jq '.statusCheckRollup | length' 2>/dev/null || true)"
  is_number "$count" && [ "$count" -gt 0 ]
}
# One line per failed check of the PR head: "<name><TAB><details url>".
failed_checks() {
  gh pr view "$pr" --json statusCheckRollup --jq '.statusCheckRollup[]
    | select((.conclusion // .state // "") | test("^(FAILURE|TIMED_OUT|CANCELLED|ERROR|ACTION_REQUIRED|STARTUP_FAILURE)$"))
    | "\(.name // .context)\t\(.detailsUrl // .targetUrl // "")"'
}
no_failed_checks() {
  local lines
  lines="$(failed_checks 2>/dev/null)" || return 1
  [ -z "$lines" ]
}
watch_checks() {
  gh pr checks "$pr" --watch --fail-fast
}

step "3/6 PR 검사 대기"
if ! poll_until pr_head_is_pushed; then
  die 5 "PR #$pr 의 헤드가 방금 push한 커밋으로 바뀌지 않았습니다." "gh pr view $pr" 'scripts/ship_dev.sh "<커밋 메시지>"'
fi
if ! poll_until checks_registered; then
  die 5 "PR #$pr 에 검사가 등록되지 않았습니다." "gh pr checks $pr" 'scripts/ship_dev.sh "<커밋 메시지>"'
fi
if ! watch_checks; then
  failed="$(failed_checks 2>/dev/null || true)"
  failed_runs=""
  say "실패한 검사:"
  while IFS="$(printf '\t')" read -r check_name check_url; do
    [ -n "$check_name" ] || continue
    say "  - $check_name"
    case "$check_url" in
      */actions/runs/*)
        failed_run="${check_url#*/actions/runs/}"
        failed_run="${failed_run%%/*}"
        if is_number "$failed_run"; then
          case " $failed_runs " in
            *" $failed_run "*) ;;
            *) failed_runs="$failed_runs $failed_run" ;;
          esac
        fi
        ;;
    esac
  done <<EOF
$failed
EOF
  rerun_hints=""
  for failed_run in $failed_runs; do
    rerun_hints="$rerun_hints gh run rerun $failed_run --failed;"
  done
  if [ "$rerun_once" -eq 0 ] || [ -z "$failed_runs" ]; then
    die 3 "PR #$pr 검사가 실패했습니다. 머지하지 않습니다." \
      "로그: gh pr checks $pr" "일시적 실패라면:${rerun_hints:- gh pr checks $pr 에서 실행을 확인}" \
      '그 뒤 다시: scripts/ship_dev.sh "<커밋 메시지>"'
  fi
  step "실패한 job을 한 번 다시 실행 (--rerun-failed-once)"
  for failed_run in $failed_runs; do
    gh run rerun "$failed_run" --failed || die 1 "다시 실행 요청에 실패했습니다." "gh run rerun $failed_run --failed"
  done
  if ! poll_until no_failed_checks; then
    die 5 "다시 실행한 검사가 시작되지 않았습니다." "gh pr checks $pr"
  fi
  if ! watch_checks; then
    die 3 "PR #$pr 검사가 다시 실행한 뒤에도 실패했습니다. 머지하지 않습니다." "로그: gh pr checks $pr"
  fi
fi
say "PR 검사 통과."

# --- 4. merge ---------------------------------------------------------------

step "4/6 머지 (PR #$pr -> $BASE_BRANCH)"
if ! gh pr merge "$pr" --merge --delete-branch --match-head-commit "$head_sha"; then
  # gh can fail after the merge itself succeeded (for example while tidying the local branch).
  pr_state="$(gh pr view "$pr" --json state --jq .state 2>/dev/null || true)"
  if [ "$pr_state" != "MERGED" ]; then
    die 1 "머지에 실패했습니다(필수 검사·리뷰·충돌을 확인하세요)." "gh pr view $pr" \
      '해결한 뒤 다시: scripts/ship_dev.sh "<커밋 메시지>"'
  fi
  say "머지는 완료됐고 로컬 브랜치 정리만 실패했습니다. 계속합니다."
fi
merge_sha="$(gh pr view "$pr" --json mergeCommit --jq '.mergeCommit.oid // empty')" ||
  die 1 "머지 커밋을 읽지 못했습니다." "gh pr view $pr"
is_sha "$merge_sha" || die 1 "머지 커밋을 읽지 못했습니다." "gh pr view $pr"
say "머지 커밋 $merge_sha"

# --- 5. the Deploy Dev run of the merge commit, approval --------------------

find_run() {
  run_id="$(gh run list --workflow "$WORKFLOW_FILE" --branch "$BASE_BRANCH" --limit 20 --json databaseId,headSha \
    --jq "map(select(.headSha == \"$merge_sha\"))[0].databaseId // empty" 2>/dev/null || true)"
  is_number "$run_id"
}

step "5/6 머지 커밋의 $WORKFLOW_NAME 실행 찾기"
if ! poll_until find_run; then
  die 5 "머지 커밋의 $WORKFLOW_NAME 실행이 나타나지 않았습니다." \
    "gh run list --workflow $WORKFLOW_FILE --branch $BASE_BRANCH" \
    "나타나면 승인: gh run view <run-id> --web, 감시: gh run watch <run-id> --exit-status"
fi
say "실행 $run_id"
approve_run fresh

# --- 6. watch, then return to develop ---------------------------------------

step "6/6 배포 감시"
watch_run
if git checkout "$BASE_BRANCH" && git pull --ff-only origin "$BASE_BRANCH"; then
  say "로컬 $BASE_BRANCH 를 최신으로 맞췄습니다."
else
  say "배포는 성공했지만 로컬 $BASE_BRANCH 갱신은 실패했습니다: git checkout $BASE_BRANCH && git pull"
fi

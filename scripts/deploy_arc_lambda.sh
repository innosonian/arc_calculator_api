#!/usr/bin/env bash
# Explicit existing-calculator deployment. Preflight is offline; it grants no approval.
# Usage: deploy_arc_lambda.sh <environment> [env-file] [bindings-json]
# Bindings may instead come from ARC_DEPLOYMENT_BINDINGS. No resource defaults exist.
set -euo pipefail
umask 077
if [[ $# -lt 1 || $# -gt 3 ]]; then
  echo "Usage: deploy_arc_lambda.sh <environment> [env-file] [bindings-json]" >&2
  exit 1
fi
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
BUILD_DIR="$(mktemp -d)"
cleanup() { rm -rf -- "${BUILD_DIR}"; }
trap cleanup EXIT
# Only the directory just created here is resolved. Preserve the builder's
# refusal of symlinked source/dependency inputs (macOS /var is a symlink).
if ! BUILD_PHYSICAL="$(cd -- "${BUILD_DIR}" 2>/dev/null && pwd -P)"; then
  echo "BUILD_DIRECTORY_UNAVAILABLE" >&2; exit 1
fi
BUILD_DIR="${BUILD_PHYSICAL}"
unset BUILD_PHYSICAL
# Must precede AWS, credential inspection, installers and artifact copy.
python3 "${SCRIPT_DIR}/deployment_preflight.py" \
  --environment "$1" --component calculator \
  --bindings "${3:-${ARC_DEPLOYMENT_BINDINGS:-}}" --env-file "${2:-}" \
  --output-dir "${BUILD_DIR}/preflight"
# Validated non-secret allowlist only. Never source/eval this file or dotenv.
# Inherited shell resource overrides do not override the explicit binding.
while IFS=$'\t' read -r key value; do
  case "${key}" in
    ENVIRONMENT) ENVIRONMENT="${value}" ;;
    AWS_REGION) AWS_REGION="${value}" ;;
    ARC_EXPECTED_ACCOUNT_ID) ARC_EXPECTED_ACCOUNT_ID="${value}" ;;
    CALC_LAMBDA_NAME) CALC_LAMBDA_NAME="${value}" ;;
    LAMBDA_ROLE_NAME) LAMBDA_ROLE_NAME="${value}" ;;
    ARC_EXPECTED_ROLE_ARN) ARC_EXPECTED_ROLE_ARN="${value}" ;;
    ARC_LAMBDA_MEMORY_MB) ARC_LAMBDA_MEMORY_MB="${value}" ;;
    ARC_LAMBDA_TIMEOUT_SEC) ARC_LAMBDA_TIMEOUT_SEC="${value}" ;;
    ARC_LOG_RETENTION_DAYS) ARC_LOG_RETENTION_DAYS="${value}" ;;
    ARC_LAMBDA_RESERVED_CONCURRENCY) ARC_LAMBDA_RESERVED_CONCURRENCY="${value}" ;;
    ARC_STORAGE_BUCKET) ARC_STORAGE_BUCKET="${value}" ;;
    ARC_STORAGE_PREFIX) ARC_STORAGE_PREFIX="${value}" ;;
    *) echo "PREFLIGHT_OUTPUT_INVALID" >&2; exit 1 ;;
  esac
done < "${BUILD_DIR}/preflight/bindings.tsv"
unset key value
ENV_JSON="${BUILD_DIR}/preflight/environment.json"
# SDK output may contain secrets. Never echo it or raw deployment arguments.
aws_value() {
  local result
  if ! result=$(aws "$@" 2>/dev/null); then echo "AWS_READ_FAILED" >&2; return 1; fi
  printf '%s' "${result}"
}
aws_quiet() {
  if ! aws "$@" >/dev/null 2>&1; then echo "AWS_COMMAND_FAILED" >&2; return 1; fi
}
lambda_retry() {
  local result attempt
  for attempt in {1..8}; do
    if result=$(aws "$@" 2>&1); then return 0; fi
    if [[ "${result}" != *ResourceConflictException* && "${result}" != *"cannot be assumed by Lambda"* ]]; then
      echo "LAMBDA_COMMAND_FAILED" >&2; return 1
    fi
    if [[ "${attempt}" == 8 ]]; then echo "LAMBDA_RETRY_EXHAUSTED" >&2; return 1; fi
    echo "LAMBDA_RETRY_PENDING" >&2
    sleep 5
  done
}
ACCOUNT_ID="$(aws_value sts get-caller-identity --query Account --output text)"
if [[ "${ACCOUNT_ID}" != "${ARC_EXPECTED_ACCOUNT_ID}" ]]; then echo "AWS_ACCOUNT_MISMATCH" >&2; exit 1; fi
# Build before IAM/Lambda mutations; no whole-repository rsync/zip.
if ! python3 -m pip install -r "${SOURCE_ROOT}/requirements.txt" -t "${BUILD_DIR}/packages" \
    -c "${SOURCE_ROOT}/constraints-lambda.txt" \
    --upgrade --no-compile --only-binary=:all: --platform manylinux2014_x86_64 \
    --implementation cp --python-version 3.12 --quiet >/dev/null 2>&1; then
  echo "DEPENDENCY_INSTALL_FAILED" >&2; exit 1
fi
python3 "${SCRIPT_DIR}/build_mock_artifact.py" --source-root "${SOURCE_ROOT}" \
  --packages-dir "${BUILD_DIR}/packages" --outdir "${BUILD_DIR}/artifact"
ZIP_PATH="${BUILD_DIR}/artifact/mock-lambda.zip"
# New infrastructure/permissions require a separate explicit plan. Existing
# bindings are reused without creating roles or guessed storage policies.
ROLE_ARN="$(aws_value iam get-role --role-name "${LAMBDA_ROLE_NAME}" --query Role.Arn --output text)"
if [[ "${ROLE_ARN}" != "${ARC_EXPECTED_ROLE_ARN}" ]]; then
  echo "EXISTING_ROLE_BINDING_MISMATCH" >&2; exit 1
fi
FUNCTION_ROLE="$(aws_value lambda get-function --function-name "${CALC_LAMBDA_NAME}" --region "${AWS_REGION}" \
  --query Configuration.Role --output text)"
if [[ "${FUNCTION_ROLE}" != "${ROLE_ARN}" ]]; then
  echo "EXISTING_FUNCTION_ROLE_MISMATCH" >&2; exit 1
fi
FUNCTION_HANDLER="$(aws_value lambda get-function --function-name "${CALC_LAMBDA_NAME}" --region "${AWS_REGION}" \
  --query Configuration.Handler --output text)"
if [[ "${FUNCTION_HANDLER}" != lambda_handler.run ]]; then
  # Do not install new code while an existing private helper is exposed. A
  # separate reviewed correction is needed before this deployment can proceed.
  echo "EXISTING_PUBLIC_HANDLER_REQUIRED" >&2; exit 1
fi
# Resolve the actual existing destination before any AWS mutation. A successful
# null response means Lambda's documented default; a failed/invalid read never
# silently falls back. Null retention requires no log-group lookup or change.
if [[ -n "${ARC_LOG_RETENTION_DAYS}" ]]; then
  FUNCTION_LOG_GROUP_JSON="$(aws_value lambda get-function --function-name "${CALC_LAMBDA_NAME}" --region "${AWS_REGION}" \
    --query Configuration.LoggingConfig.LogGroup --output json)"
  LOG_GROUP_JSON_PATTERN='^"([A-Za-z0-9._/#-]+)"$'
  if [[ "${FUNCTION_LOG_GROUP_JSON}" == null ]]; then
    ARC_FUNCTION_LOG_GROUP="/aws/lambda/${CALC_LAMBDA_NAME}"
  elif [[ ${#FUNCTION_LOG_GROUP_JSON} -le 514 && "${FUNCTION_LOG_GROUP_JSON}" =~ ${LOG_GROUP_JSON_PATTERN} ]]; then
    ARC_FUNCTION_LOG_GROUP="${BASH_REMATCH[1]}"
  else
    echo "EXISTING_LOG_GROUP_INVALID" >&2; exit 1
  fi
  unset FUNCTION_LOG_GROUP_JSON LOG_GROUP_JSON_PATTERN
fi
lambda_retry lambda update-function-code --function-name "${CALC_LAMBDA_NAME}" \
  --zip-file "fileb://${ZIP_PATH}" --region "${AWS_REGION}"
aws_quiet lambda wait function-updated --function-name "${CALC_LAMBDA_NAME}" --region "${AWS_REGION}"
lambda_retry lambda update-function-configuration --function-name "${CALC_LAMBDA_NAME}" \
  --handler lambda_handler.run --runtime python3.12 \
  --memory-size "${ARC_LAMBDA_MEMORY_MB}" --timeout "${ARC_LAMBDA_TIMEOUT_SEC}" \
  --environment "file://${ENV_JSON}" --region "${AWS_REGION}"
aws_quiet lambda wait function-updated --function-name "${CALC_LAMBDA_NAME}" --region "${AWS_REGION}"
# Null leaves the existing retention untouched; it does not disable expiration.
# Failed explicitly requested retention is not a successful deployment.
if [[ -n "${ARC_LOG_RETENTION_DAYS}" ]]; then
  aws_quiet logs put-retention-policy --log-group-name="${ARC_FUNCTION_LOG_GROUP}" \
    --retention-in-days "${ARC_LOG_RETENTION_DAYS}" --region "${AWS_REGION}"
fi
if [[ -n "${ARC_LAMBDA_RESERVED_CONCURRENCY}" ]]; then
  aws_quiet lambda put-function-concurrency --function-name "${CALC_LAMBDA_NAME}" \
    --reserved-concurrent-executions "${ARC_LAMBDA_RESERVED_CONCURRENCY}" --region "${AWS_REGION}"
fi
aws_quiet lambda publish-version --function-name "${CALC_LAMBDA_NAME}" \
  --description "explicit ${ENVIRONMENT} deployment" --region "${AWS_REGION}"
echo "CALCULATOR_DEPLOYMENT_FINISHED"

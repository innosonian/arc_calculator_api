#!/usr/bin/env bash
# Reconnect an explicitly configured existing calculator Gateway method.
# Usage: deploy_arc_api_gateway.sh <environment> [env-file] [bindings-json]
# Preflight performs no AWS calls and grants no deployment approval.
set -euo pipefail
umask 077
if [[ $# -lt 1 || $# -gt 3 ]]; then
  echo "Usage: deploy_arc_api_gateway.sh <environment> [env-file] [bindings-json]" >&2; exit 1
fi
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="$(mktemp -d)"
cleanup() { rm -rf -- "${BUILD_DIR}"; }
trap cleanup EXIT
# Resolve only this script-owned directory, including macOS /var ancestors.
if ! BUILD_PHYSICAL="$(cd -- "${BUILD_DIR}" 2>/dev/null && pwd -P)"; then
  echo "BUILD_DIRECTORY_UNAVAILABLE" >&2; exit 1
fi
BUILD_DIR="${BUILD_PHYSICAL}"
unset BUILD_PHYSICAL
python3 "${SCRIPT_DIR}/deployment_preflight.py" \
  --environment "$1" --component gateway \
  --bindings "${3:-${ARC_DEPLOYMENT_BINDINGS:-}}" --env-file "${2:-}" \
  --output-dir "${BUILD_DIR}/preflight"
while IFS=$'\t' read -r key value; do
  case "${key}" in
    ENVIRONMENT) ENVIRONMENT="${value}" ;;
    AWS_REGION) AWS_REGION="${value}" ;;
    ARC_EXPECTED_ACCOUNT_ID) ARC_EXPECTED_ACCOUNT_ID="${value}" ;;
    CALC_LAMBDA_NAME) CALC_LAMBDA_NAME="${value}" ;;
    API_ID) API_ID="${value}" ;;
    API_STAGE) API_STAGE="${value}" ;;
    API_ROUTE) API_ROUTE="${value}" ;;
    ARC_API_RATE_LIMIT) ARC_API_RATE_LIMIT="${value}" ;;
    ARC_API_BURST_LIMIT) ARC_API_BURST_LIMIT="${value}" ;;
    LAMBDA_ROLE_NAME|ARC_EXPECTED_ROLE_ARN|ARC_LAMBDA_MEMORY_MB|ARC_LAMBDA_TIMEOUT_SEC|ARC_LOG_RETENTION_DAYS|ARC_LAMBDA_RESERVED_CONCURRENCY|ARC_STORAGE_BUCKET|ARC_STORAGE_PREFIX) ;;
    *) echo "PREFLIGHT_OUTPUT_INVALID" >&2; exit 1 ;;
  esac
done < "${BUILD_DIR}/preflight/bindings.tsv"
unset key value
aws_value() {
  local result
  if ! result=$(aws "$@" 2>/dev/null); then echo "AWS_READ_FAILED" >&2; return 1; fi
  printf '%s' "${result}"
}
aws_quiet() {
  if ! aws "$@" >/dev/null 2>&1; then echo "AWS_COMMAND_FAILED" >&2; return 1; fi
}
ACCOUNT_ID="$(aws_value sts get-caller-identity --query Account --output text)"
if [[ "${ACCOUNT_ID}" != "${ARC_EXPECTED_ACCOUNT_ID}" ]]; then echo "AWS_ACCOUNT_MISMATCH" >&2; exit 1; fi
# Never create unknown APIs/resources/methods/stages or replace access control
# with NONE authorization. Operational access-control adequacy is a separate review.
aws_quiet apigateway get-rest-api --rest-api-id "${API_ID}" --region "${AWS_REGION}"
RESOURCE_ID="$(aws_value apigateway get-resources --rest-api-id "${API_ID}" --region "${AWS_REGION}" \
  --query "items[?path=='${API_ROUTE}'].id | [0]" --output text)"
if [[ ! "${RESOURCE_ID}" =~ ^[a-z0-9]+$ || "${RESOURCE_ID}" == None ]]; then
  echo "EXISTING_GATEWAY_RESOURCE_REQUIRED" >&2; exit 1
fi
aws_quiet apigateway get-method --rest-api-id "${API_ID}" --resource-id "${RESOURCE_ID}" \
  --http-method POST --region "${AWS_REGION}"
aws_quiet apigateway get-stage --rest-api-id "${API_ID}" --stage-name "${API_STAGE}" --region "${AWS_REGION}"
FUNCTION_HANDLER="$(aws_value lambda get-function --function-name "${CALC_LAMBDA_NAME}" --region "${AWS_REGION}" \
  --query Configuration.Handler --output text)"
if [[ "${FUNCTION_HANDLER}" != lambda_handler.run ]]; then
  echo "EXISTING_PUBLIC_HANDLER_REQUIRED" >&2; exit 1
fi
aws_quiet apigateway put-integration --rest-api-id "${API_ID}" --resource-id "${RESOURCE_ID}" \
  --http-method POST --type AWS_PROXY --integration-http-method POST \
  --uri "arn:aws:apigateway:${AWS_REGION}:lambda:path/2015-03-31/functions/arn:aws:lambda:${AWS_REGION}:${ACCOUNT_ID}:function:${CALC_LAMBDA_NAME}/invocations" \
  --region "${AWS_REGION}"
BINARY_TYPES="$(aws_value apigateway get-rest-api --rest-api-id "${API_ID}" --region "${AWS_REGION}" \
  --query binaryMediaTypes --output text)"
if [[ " ${BINARY_TYPES//$'\t'/ } " != *" multipart/form-data "* ]]; then
  aws_quiet apigateway update-rest-api --rest-api-id "${API_ID}" --region "${AWS_REGION}" \
    --patch-operations op=add,path=/binaryMediaTypes/multipart~1form-data
fi
if [[ -n "${ARC_API_RATE_LIMIT}" ]]; then
  aws_quiet apigateway update-stage --rest-api-id "${API_ID}" --stage-name "${API_STAGE}" --region "${AWS_REGION}" \
    --patch-operations "op=replace,path=/~1cpr-analysis/POST/throttling/rateLimit,value=${ARC_API_RATE_LIMIT}" \
    "op=replace,path=/~1cpr-analysis/POST/throttling/burstLimit,value=${ARC_API_BURST_LIMIT}"
fi
# Invoke permissions must already cover this API/stage. No wildcard grants.
aws_quiet apigateway create-deployment --rest-api-id "${API_ID}" --stage-name "${API_STAGE}" --region "${AWS_REGION}"
echo "GATEWAY_DEPLOYMENT_FINISHED"

"""Offline deployment syntax/composition checks; never imports an SDK.

No resource values or runtime settings are inferred from environment names.
A successful check does not authorize deployment or verify AWS resources.
"""

import argparse
import ast
import json
import math
import os
from pathlib import Path
import re
import sys


_SELECTORS = {"local": "local", "dev": "development", "development": "development",
              "beta": "beta", "prod": "production", "production": "production"}
_KEY = re.compile(r"[A-Za-z][A-Za-z0-9_]+\Z")
_SEGMENT = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
# Standard Lambda and CloudWatch Logs API constraints, checked 2026-09-11.
# These are service limits, not selected ARC operating values.
_LOG_RETENTION_DAYS = frozenset((1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180,
                               365, 400, 545, 731, 1096, 1827, 2192, 2557, 2922, 3288, 3653))
_RESERVED = {
    "AWS_REGION", "AWS_DEFAULT_REGION", "AWS_ACCESS_KEY", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
    "AWS_SECRET_KEY", "AWS_SESSION_TOKEN", "AWS_SECURITY_TOKEN", "AWS_EXECUTION_ENV", "_HANDLER",
    "LAMBDA_TASK_ROOT", "LAMBDA_RUNTIME_DIR", "_AWS_XRAY_DAEMON_ADDRESS", "_AWS_XRAY_DAEMON_PORT",
    "AWS_XRAY_CONTEXT_MISSING", "AWS_XRAY_DAEMON_ADDRESS", "TZ",
}


class PreflightError(ValueError):
    """A fixed non-secret diagnostic code."""


def _fail(code):
    raise PreflightError(code)


def normalize_selector(value):
    if type(value) is not str or value not in _SELECTORS:
        _fail("ENVIRONMENT_UNSUPPORTED")
    return _SELECTORS[value]


def _pairs(pairs):
    values = {}
    for key, value in pairs:
        if key in values:
            _fail("DUPLICATE_BINDING_FIELD")
        values[key] = value
    return values


def _object(value, required, optional=()):
    if (type(value) is not dict or not set(required) <= set(value)
            or set(value) - set(required) - set(optional)):
        _fail("BINDING_SHAPE_INVALID")


def _text(value, pattern):
    if type(value) is not str or re.fullmatch(pattern, value) is None:
        _fail("BINDING_VALUE_INVALID")
    return value


def _integer(value, *, zero=False):
    if type(value) is not int or value < (0 if zero else 1):
        _fail("BINDING_VALUE_INVALID")
    return value


def _regular_text(path, missing_code):
    try:
        selected = Path(path)
        if not path or selected.is_symlink() or not selected.is_file():
            _fail(missing_code)
        # Decode exact bytes: text mode would turn bare CR into LF before the
        # dotenv parser can reject unsupported record separators.
        return selected.read_bytes().decode("utf-8")
    except (OSError, UnicodeError, TypeError):
        _fail(missing_code)


def read_bindings(path):
    try:
        return json.loads(_regular_text(path, "DEPLOYMENT_BINDINGS_REQUIRED"), object_pairs_hook=_pairs,
                          parse_constant=lambda _: _fail("BINDING_VALUE_INVALID"))
    except (ValueError, TypeError, RecursionError) as error:
        if isinstance(error, PreflightError):
            raise
        _fail("BINDING_JSON_INVALID")


def parse_env_file(path):
    variables = {}
    text = _regular_text(path, "ENV_FILE_REQUIRED")
    # splitlines() would reinterpret VT/FF/NEL/U+2028 inside values as new
    # settings. Only LF/CRLF are supported; never normalize the value itself.
    if (any(char in text for char in ("\v", "\f", "\x85", "\u2028", "\u2029"))
            or re.search(r"\r(?!\n)", text)):
        _fail("ENV_FILE_INVALID")
    for line in text.split("\n"):
        if line.endswith("\r"):
            line = line[:-1]
        if "\r" in line:
            _fail("ENV_FILE_INVALID")
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in line:
            _fail("ENV_FILE_INVALID")
        key, value = line.split("=", 1)
        key = key.strip()
        if (not _KEY.fullmatch(key) or key in variables or key in _RESERVED
                or key.startswith("AWS_LAMBDA_")
                or (value and (value[0] in "'\"" or value[-1] in "'\""))
                or any(ord(char) < 32 or ord(char) == 127 for char in value)):
            _fail("ENV_FILE_INVALID")
        variables[key] = value
    return variables


def _validate_environment(variables):
    # Recheck callers of validate_configuration, not only dotenv input.
    if (type(variables) is not dict or any(
            type(key) is not str or not _KEY.fullmatch(key) or key in _RESERVED
            or key.startswith("AWS_LAMBDA_") or type(value) is not str
            for key, value in variables.items())):
        _fail("ENV_FILE_INVALID")
    try:
        size = sum(len(key.encode("utf-8")) + len(value.encode("utf-8"))
                   for key, value in variables.items())
    except UnicodeError:
        _fail("ENV_FILE_INVALID")
    if size > 4096:
        _fail("LAMBDA_ENVIRONMENT_TOO_LARGE")


def read_storage_contract():
    """Read the preserved source constants without importing runtime/SDK code."""
    path = Path(__file__).resolve().parents[1] / "util" / "uploader.py"
    try:
        tree = ast.parse(_regular_text(path, "STORAGE_SOURCE_UNAVAILABLE"))
        found = {}
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id in ("BUCKET", "RTDATA_DIRECTORY"):
                        if target.id in found:
                            _fail("STORAGE_SOURCE_CONTRACT_INVALID")
                        found[target.id] = ast.literal_eval(node.value)
        if set(found) != {"BUCKET", "RTDATA_DIRECTORY"} or any(type(value) is not str for value in found.values()):
            _fail("STORAGE_SOURCE_CONTRACT_INVALID")
        return found["BUCKET"], found["RTDATA_DIRECTORY"]
    except (OSError, SyntaxError, ValueError, TypeError, RecursionError):
        _fail("STORAGE_SOURCE_CONTRACT_INVALID")


def validate_configuration(selector, component, document, variables, *, storage_contract):
    """Validate only the explicitly selected deployment binding.

    Selector aliases do not modify runtime_stage, STAGE, tables, buckets,
    prefixes, or ARC_MOCK_ENVIRONMENT. Null optional controls mean unchanged.
    """
    selected = normalize_selector(selector)
    if selected == "local":
        _fail("LOCAL_AWS_DEPLOYMENT_FORBIDDEN")
    if component not in ("calculator", "gateway"):
        _fail("COMPONENT_UNSUPPORTED")
    _object(document, ("schema_version", "environments"))
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        _fail("BINDING_SCHEMA_UNSUPPORTED")
    environments = document["environments"]
    if (type(environments) is not dict
            or any(key not in ("local", "development", "beta", "production") for key in environments)):
        _fail("BINDING_SHAPE_INVALID")
    if selected not in environments:
        _fail("ENVIRONMENT_NOT_CONFIGURED")
    binding = environments[selected]
    _object(binding, ("account_id", "region", "runtime_stage", "calculator"), ("gateway",))
    _text(binding["account_id"], r"[0-9]{12}")
    _text(binding["region"], r"[a-z]{2}(?:-[a-z]+)+-[0-9]+")
    _text(binding["runtime_stage"], _SEGMENT.pattern)
    _validate_environment(variables)
    if variables.get("STAGE") != binding["runtime_stage"]:
        _fail("RUNTIME_STAGE_MISMATCH")
    calc = binding["calculator"]
    _object(calc, ("function_name", "role_name", "memory_mb", "timeout_seconds", "log_retention_days",
                   "reserved_concurrency", "storage_bucket", "storage_prefix", "storage_region"), ("role_arn",))
    _text(calc["function_name"], r"[A-Za-z0-9_-]{1,64}")
    _text(calc["role_name"], r"[A-Za-z0-9_+=,.@-]{1,64}")
    if "role_arn" in calc:
        # An explicit path-qualified role still belongs to the selected account
        # and must end in the exact get-role name. Never discover a substitute.
        _text(calc["role_arn"], rf"arn:aws:iam::{binding['account_id']}:role/(?:[A-Za-z0-9_+=,.@-]+/)*{re.escape(calc['role_name'])}")
    for key in ("memory_mb", "timeout_seconds"):
        _integer(calc[key])
    if not 128 <= calc["memory_mb"] <= 10240 or calc["timeout_seconds"] > 900:
        _fail("LAMBDA_LIMIT_INVALID")
    retention = calc["log_retention_days"]
    if retention is not None and (type(retention) is not int or retention not in _LOG_RETENTION_DAYS):
        _fail("LOG_RETENTION_INVALID")
    if calc["reserved_concurrency"] is not None:
        _integer(calc["reserved_concurrency"], zero=True)
    _text(calc["storage_bucket"], r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]")
    if (".." in calc["storage_bucket"] or ".-" in calc["storage_bucket"]
            or "-." in calc["storage_bucket"]):
        _fail("BINDING_VALUE_INVALID")
    _text(calc["storage_prefix"], r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*")
    _text(calc["storage_region"], r"[a-z]{2}(?:-[a-z]+)+-[0-9]+")
    if (type(storage_contract) is not tuple or len(storage_contract) != 2
            or any(type(value) is not str for value in storage_contract)
            or (calc["storage_bucket"], calc["storage_prefix"]) != storage_contract):
        _fail("STORAGE_SOURCE_BINDING_MISMATCH")
    if variables.get("ARC_STORAGE_REGION") != calc["storage_region"]:
        _fail("STORAGE_REGION_MISMATCH")
    gateway = binding.get("gateway")
    if component == "gateway" and gateway is None:
        _fail("GATEWAY_NOT_CONFIGURED")
    if gateway is not None:
        _object(gateway, ("api_id", "stage", "route", "rate_limit", "burst_limit"))
        _text(gateway["api_id"], r"[a-z0-9]{1,128}")
        _text(gateway["stage"], _SEGMENT.pattern)
        # The existing script deploys this one calculator alias. Other routes
        # need their own explicit router/integration plan, not path inference.
        if gateway["route"] != "/cpr-analysis":
            _fail("GATEWAY_ROUTE_UNSUPPORTED")
        rate, burst = gateway["rate_limit"], gateway["burst_limit"]
        if (rate is None) != (burst is None):
            _fail("GATEWAY_THROTTLE_INCOMPLETE")
        if rate is not None:
            if type(rate) not in (int, float) or not math.isfinite(rate) or rate < 0:
                _fail("BINDING_VALUE_INVALID")
            _integer(burst, zero=True)
        for name, key in (("ARC_API_GATEWAY_ID", "api_id"), ("ARC_API_STAGE", "stage"), ("ARC_API_ROUTE", "route")):
            if name in variables and variables[name] != gateway[key]:
                _fail("GATEWAY_ENV_CONFLICT")
    # A detached JSON value, not caller-mutable configuration authority.
    return json.loads(json.dumps({"environment": selected, "component": component, "binding": binding}, allow_nan=False))


def _shell_values(checked):
    binding, values = checked["binding"], {}
    calc = binding["calculator"]
    values.update(ENVIRONMENT=checked["environment"], AWS_REGION=binding["region"],
                  ARC_EXPECTED_ACCOUNT_ID=binding["account_id"], CALC_LAMBDA_NAME=calc["function_name"],
                  LAMBDA_ROLE_NAME=calc["role_name"], ARC_LAMBDA_MEMORY_MB=calc["memory_mb"],
                  ARC_LAMBDA_TIMEOUT_SEC=calc["timeout_seconds"], ARC_LOG_RETENTION_DAYS=calc["log_retention_days"],
                  ARC_LAMBDA_RESERVED_CONCURRENCY=calc["reserved_concurrency"],
                  ARC_STORAGE_BUCKET=calc["storage_bucket"], ARC_STORAGE_PREFIX=calc["storage_prefix"])
    values["ARC_EXPECTED_ROLE_ARN"] = calc.get(
        "role_arn", f"arn:aws:iam::{binding['account_id']}:role/{calc['role_name']}")
    if checked["component"] == "gateway":
        gateway = binding["gateway"]
        values.update(API_ID=gateway["api_id"], API_STAGE=gateway["stage"], API_ROUTE=gateway["route"],
                      ARC_API_RATE_LIMIT=gateway["rate_limit"], ARC_API_BURST_LIMIT=gateway["burst_limit"])
    return "".join(f"{key}\t{'' if value is None else value}\n" for key, value in values.items())


def _write_private(path, body):
    # The output directory must be caller-owned/private. Never overwrite a
    # pre-existing file or follow a symlink while exporting env secrets.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(body)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--component", required=True)
    parser.add_argument("--bindings", required=True)
    parser.add_argument("--env-file", default="")
    parser.add_argument("--output-dir")
    parser.add_argument("--github-output")
    args = parser.parse_args(argv)
    try:
        selected = normalize_selector(args.environment)
        if selected == "local":
            _fail("LOCAL_AWS_DEPLOYMENT_FORBIDDEN")
        # No beta default env file, binding, resource, branch or secret exists.
        env_file = args.env_file or {"development": ".env.dev", "production": ".env"}.get(selected)
        document, variables = read_bindings(args.bindings), parse_env_file(env_file)
        checked = validate_configuration(selected, args.component, document, variables,
                                         storage_contract=read_storage_contract())
        if args.output_dir:
            directory = Path(args.output_dir)
            if directory.is_symlink() or directory.exists():
                _fail("OUTPUT_DIRECTORY_CONFLICT")
            directory.mkdir(mode=0o700)
            _write_private(directory / "environment.json", json.dumps({"Variables": variables}, ensure_ascii=True) + "\n")
            _write_private(directory / "bindings.tsv", _shell_values(checked))
            _write_private(directory / "checked.json", json.dumps(checked, sort_keys=True) + "\n")
        if args.github_output:
            with open(args.github_output, "a", encoding="utf-8") as stream:
                stream.write(f"aws_region={checked['binding']['region']}\n")
        print(json.dumps({"status": "syntax_and_composition_valid", "environment": selected,
                          "component": args.component, "aws_resources_verified": False, "runtime_verified": False}))
        return 0
    except PreflightError as error:
        print(str(error), file=sys.stderr)
    except (OSError, ValueError, TypeError, OverflowError, RecursionError):
        print("PREFLIGHT_FAILED", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

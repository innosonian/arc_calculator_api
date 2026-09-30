"""Dev Lambda code deployment steps of .github/workflows/deploy_dev.yml (D137).

Standard library plus boto3 (imported only when a client is created). Four
subcommands, each a separate workflow step:

* ``check-config``: the ARC_JOURNEY_CONFIG of each named function must carry the
  requested role and the code registry's execution versions, and must pass
  ``AwsSettings.parse`` offline. Nothing is written. A difference exits 2
  (CONFIG_DRIFT) and names only the role, the field and the code's expected
  value; the configuration JSON, account, resource names and other environment
  variables are never printed.
* ``deploy``: ``update_function_code`` (ZIP bytes, no version publish) in the
  given order, wait for the update, then compare the function's CodeSha256
  with the ZIP's own digest (exit 4, CODE_SHA_MISMATCH). The first failure
  stops the run; functions already updated stay in the report.
* ``smoke``: public Dummy login and course list through the deployed API. The
  Dummy logout is never called (it resets the shared Dummy progress). Only HTTP
  statuses, counts and fixed error codes are printed, never a token or a body.
* ``summary``: Markdown for $GITHUB_STEP_SUMMARY from the report files.

No output line ever contains a token, a signed URL, an environment variable
value or the configuration document. Exit codes: 0 ok, 2 CONFIG_DRIFT,
3 other fixed codes, 4 CODE_SHA_MISMATCH, 5 SMOKE_FAILED.
"""

import argparse
import base64
import hashlib
import io
import json
from pathlib import Path
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile


ROLES = ("api", "worker", "relay")
EXECUTION_ROLES = ("api", "worker")
CONFIG_VARIABLE = "ARC_JOURNEY_CONFIG"
FUNCTION_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
REGION = re.compile(r"[a-z]{2}(?:-[a-z]+)+-[0-9]+\Z")
ERROR_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
MAX_ZIP_BYTES = 50 * 1024 * 1024
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
HTTP_TIMEOUT_SECONDS = 20
# Each function: poll every 2 s for at most 90 s (the workflow timeout bounds the job).
WAITER_CONFIG = {"Delay": 2, "MaxAttempts": 45}
# APP_API §2.1: the public Dummy account of the shared Dev.
DUMMY_LOGIN = {"loginId": "test@test.com", "password": "2222"}
LOGIN_PATH = "/api/v2/sessions/"
PROGRESS_PATH = "/api/v2/courses/progress/?page=1&pageSize=100"
EXIT_CODES = {"CONFIG_DRIFT": 2, "CODE_SHA_MISMATCH": 4, "SMOKE_FAILED": 5}


class DeployError(Exception):
    """A fixed code plus safe detail fields (roles, field names, code expectations, statuses)."""

    def __init__(self, code, **detail):
        super().__init__(code)
        self.code = code
        self.detail = detail

    @property
    def exit_code(self):
        return EXIT_CODES.get(self.code, 3)


def emit(**fields):
    print(json.dumps(fields, sort_keys=True))


def expected_execution():
    from mock_journey.contracts import CURRENT_ADAPTER_VERSION, RETAINED_ADAPTER_VERSIONS
    from mock_journey.execution_definitions import PROJECTION_VERSION

    return {"current_adapter_version": CURRENT_ADAPTER_VERSION,
            "retained_adapter_versions": list(RETAINED_ADAPTER_VERSIONS),
            "projection_version": PROJECTION_VERSION}


def parse_functions(values, *, roles=ROLES):
    """``role=name`` arguments in their given order; every role once, names validated."""
    result = []
    for value in values or ():
        role, separator, name = value.partition("=")
        if not separator or role not in roles or not FUNCTION_NAME.fullmatch(name):
            raise DeployError("ARGUMENT_INVALID", argument="function")
        if any(role == existing for existing, _ in result):
            raise DeployError("ARGUMENT_INVALID", argument="function")
        result.append((role, name))
    if not result:
        raise DeployError("ARGUMENT_INVALID", argument="function")
    return result


def parse_region(value):
    if type(value) is not str or not REGION.fullmatch(value):
        raise DeployError("ARGUMENT_INVALID", argument="region")
    return value


def lambda_client(region):
    """The only place a client is created (tests replace this function)."""
    import boto3
    from botocore.config import Config

    return boto3.client("lambda", region_name=region,
                        config=Config(connect_timeout=5, read_timeout=60, proxies={},
                                      retries={"total_max_attempts": 3, "mode": "standard"}))


def open_url(request, timeout):
    """The only HTTP seam (tests replace this function)."""
    return urllib.request.urlopen(request, timeout=timeout)


# --- check-config ---------------------------------------------------------

def check_role_configuration(client, role, name):
    try:
        configuration = client.get_function_configuration(FunctionName=name)
    except Exception:
        raise DeployError("AWS_CALL_FAILED", role=role, call="GetFunctionConfiguration") from None
    environment = configuration.get("Environment") if isinstance(configuration, dict) else None
    variables = environment.get("Variables") if isinstance(environment, dict) else None
    raw = variables.get(CONFIG_VARIABLE) if isinstance(variables, dict) else None
    if type(raw) is not str or not raw:
        raise DeployError("CONFIG_DRIFT", role=role, field=CONFIG_VARIABLE, expected="present")
    try:
        document = json.loads(raw)
    except ValueError:
        document = None
    if type(document) is not dict:
        raise DeployError("CONFIG_DRIFT", role=role, field=CONFIG_VARIABLE, expected="json_object")
    if document.get("role") != role:
        raise DeployError("CONFIG_DRIFT", role=role, field="role", expected=role)
    if role in EXECUTION_ROLES:
        execution = document.get("execution")
        if type(execution) is not dict:
            raise DeployError("CONFIG_DRIFT", role=role, field="execution", expected="object")
        for field, value in expected_execution().items():
            if execution.get(field) != value:
                raise DeployError("CONFIG_DRIFT", role=role, field="execution." + field, expected=value)
    from mock_journey.aws_settings import AwsSettings

    try:
        AwsSettings.parse(raw, role)
    except Exception:
        raise DeployError("CONFIG_DRIFT", role=role, field="AwsSettings.parse", expected="valid") from None
    return {"role": role, "check": "config_matches_code_registry"}


def check_config(args):
    region = parse_region(args.region)
    functions = parse_functions(args.function)
    client = lambda_client(region)
    for role, name in functions:
        emit(**check_role_configuration(client, role, name))
    return 0


# --- deploy ---------------------------------------------------------------

def read_artifact(zip_path, manifest_path):
    """The ZIP bytes and both digests; the manifest (when given) must describe this ZIP."""
    try:
        data = Path(zip_path).read_bytes()
    except OSError:
        raise DeployError("ARTIFACT_MISSING") from None
    if not data or len(data) > MAX_ZIP_BYTES:
        raise DeployError("ARTIFACT_INVALID")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if archive.testzip() is not None or not archive.namelist():
                raise DeployError("ARTIFACT_INVALID")
    except zipfile.BadZipFile:
        raise DeployError("ARTIFACT_INVALID") from None
    digest = hashlib.sha256(data).digest()
    hex_digest = digest.hex()
    if manifest_path:
        try:
            manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise DeployError("ARTIFACT_MANIFEST_INVALID") from None
        recorded = manifest.get("zip", {}).get("sha256") if isinstance(manifest, dict) else None
        if recorded != hex_digest:
            raise DeployError("ARTIFACT_MANIFEST_MISMATCH")
    return data, hex_digest, base64.b64encode(digest).decode("ascii")


def write_report(path, report):
    if path:
        Path(path).write_text(json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def function_entry(role, name, configuration):
    entry = {"role": role, "FunctionName": name}
    for key in ("CodeSha256", "LastModified", "LastUpdateStatus"):
        value = configuration.get(key) if isinstance(configuration, dict) else None
        entry[key] = value if type(value) is str else None
    return entry


def deploy(args):
    region = parse_region(args.region)
    functions = parse_functions(args.function)
    data, hex_digest, code_sha = read_artifact(args.zip, args.manifest)
    client = lambda_client(region)
    report = {"zip_sha256": hex_digest, "expected_code_sha256": code_sha, "functions": [], "status": "started"}
    write_report(args.report, report)
    # Every target must be a ZIP-packaged python3.12 function before the first upload.
    targets = []
    for role, name in functions:
        try:
            configuration = client.get_function_configuration(FunctionName=name)
        except Exception:
            raise DeployError("AWS_CALL_FAILED", role=role, call="GetFunctionConfiguration") from None
        if (not isinstance(configuration, dict) or configuration.get("PackageType") != "Zip"
                or configuration.get("Runtime") != "python3.12"):
            raise DeployError("FUNCTION_UNSUPPORTED", role=role)
        targets.append((role, name))
    for role, name in targets:
        try:
            client.update_function_code(FunctionName=name, ZipFile=data, Publish=False)
            client.get_waiter("function_updated_v2").wait(FunctionName=name, WaiterConfig=dict(WAITER_CONFIG))
            configuration = client.get_function_configuration(FunctionName=name)
        except Exception:
            report.update(status="failed", failed_role=role, code="AWS_CALL_FAILED")
            write_report(args.report, report)
            raise DeployError("AWS_CALL_FAILED", role=role, call="UpdateFunctionCode",
                              updated=[entry["role"] for entry in report["functions"]]) from None
        entry = function_entry(role, name, configuration)
        report["functions"].append(entry)
        if entry["CodeSha256"] != code_sha:
            entry["code_sha256_matches"] = False
            report.update(status="failed", failed_role=role, code="CODE_SHA_MISMATCH")
            write_report(args.report, report)
            raise DeployError("CODE_SHA_MISMATCH", role=role,
                              updated=[item["role"] for item in report["functions"][:-1]])
        entry["code_sha256_matches"] = True
        write_report(args.report, report)
        emit(FunctionName=name, CodeSha256=entry["CodeSha256"], LastModified=entry["LastModified"])
    report["status"] = "completed"
    write_report(args.report, report)
    return 0


# --- smoke ----------------------------------------------------------------

def parse_base_url(value):
    parts = urllib.parse.urlsplit(value if type(value) is str else "")
    if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
            or parts.query or parts.fragment):
        raise DeployError("ARGUMENT_INVALID", argument="base-url")
    return value.rstrip("/")


def request_json(method, url, *, body=None, token=None):
    """Return (status, parsed JSON or None, fixed error code or None); never the raw body."""
    headers = {"Accept": "application/json"}
    payload = None
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(url, data=payload, headers=headers, method=method)
    try:
        with open_url(request, HTTP_TIMEOUT_SECONDS) as response:
            status, raw = response.status, response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        status = error.code
        try:
            raw = error.read(MAX_RESPONSE_BYTES + 1)
        except Exception:
            raw = b""
    except (OSError, urllib.error.URLError, ValueError):
        return None, None, "REQUEST_FAILED"
    if len(raw) > MAX_RESPONSE_BYTES:
        return status, None, "RESPONSE_TOO_LARGE"
    try:
        document = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return status, None, "RESPONSE_NOT_JSON"
    code = None
    if isinstance(document, dict) and isinstance(document.get("error"), dict):
        candidate = document["error"].get("code")
        code = candidate if type(candidate) is str and ERROR_CODE.fullmatch(candidate) else "ERROR_CODE_INVALID"
    return status, document, code


def smoke_failure(step, status, reason, report_path, report):
    report.update(status="failed", failed_step=step)
    write_report(report_path, report)
    raise DeployError("SMOKE_FAILED", step=step, status=status, reason=reason)


def smoke(args):
    base = parse_base_url(args.base_url)
    report = {"steps": [], "status": "started"}
    write_report(args.report, report)
    status, document, code = request_json("POST", base + LOGIN_PATH, body=DUMMY_LOGIN)
    report["steps"].append({"step": "login", "status": status, "error_code": code})
    data = document.get("data") if isinstance(document, dict) else None
    if status != 201 or not isinstance(data, dict):
        smoke_failure("login", status, code or "UNEXPECTED_STATUS", args.report, report)
    token = data.get("accessToken")
    if data.get("tokenType") != "Bearer":
        smoke_failure("login", status, "TOKEN_TYPE_INVALID", args.report, report)
    if type(data.get("userName")) is not str or not data["userName"]:
        smoke_failure("login", status, "USER_NAME_MISSING", args.report, report)
    if type(token) is not str or not token:
        smoke_failure("login", status, "ACCESS_TOKEN_MISSING", args.report, report)
    emit(step="login", status=status)
    status, document, code = request_json("GET", base + PROGRESS_PATH, token=token)
    data = document.get("data") if isinstance(document, dict) else None
    count = data.get("count") if isinstance(data, dict) else None
    results = data.get("results") if isinstance(data, dict) else None
    entry = {"step": "progress", "status": status, "error_code": code,
             "count": count if type(count) is int else None}
    report["steps"].append(entry)
    if status != 200 or not isinstance(data, dict):
        smoke_failure("progress", status, code or "UNEXPECTED_STATUS", args.report, report)
    if not ((type(count) is int and count >= 1) or (isinstance(results, list) and results)):
        smoke_failure("progress", status, "COURSE_LIST_EMPTY", args.report, report)
    emit(step="progress", status=status, count=entry["count"])
    # No logout on purpose: DELETE /api/v2/session/ resets the shared Dummy progress.
    report["status"] = "completed"
    write_report(args.report, report)
    del token
    return 0


# --- summary --------------------------------------------------------------

def load_report(path):
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8")) if path else None
    except (OSError, ValueError):
        return None
    return document if isinstance(document, dict) else None


def cell(value):
    return str(value) if value is not None else "-"


def summary_markdown(commit, zip_path, deploy_report, smoke_report):
    lines = ["## Dev deployment (D137)", ""]
    lines.append(f"- commit: `{commit}`")
    try:
        zip_digest = hashlib.sha256(Path(zip_path).read_bytes()).hexdigest()
    except OSError:
        zip_digest = None
    lines.append(f"- artifact sha256: `{zip_digest}`" if zip_digest else "- artifact: not built or not downloaded")
    if deploy_report is None:
        lines.append("- deploy: not run")
    else:
        lines.append(f"- deploy: {cell(deploy_report.get('status'))}"
                     + (f" (stopped at role `{deploy_report['failed_role']}`, `{cell(deploy_report.get('code'))}`)"
                        if deploy_report.get("failed_role") else ""))
        lines.extend(["", "| role | function | CodeSha256 | LastModified | matches ZIP |", "|---|---|---|---|---|"])
        for entry in deploy_report.get("functions") or ():
            if not isinstance(entry, dict):
                continue
            lines.append("| " + " | ".join(cell(entry.get(key)) for key in
                                           ("role", "FunctionName", "CodeSha256", "LastModified",
                                            "code_sha256_matches")) + " |")
    if smoke_report is None:
        lines.append("- smoke: not run")
    else:
        lines.append(f"- smoke: {cell(smoke_report.get('status'))}"
                     + (f" (failed step `{smoke_report['failed_step']}`)" if smoke_report.get("failed_step") else ""))
        for entry in smoke_report.get("steps") or ():
            if isinstance(entry, dict):
                lines.append(f"  - {cell(entry.get('step'))}: HTTP {cell(entry.get('status'))}"
                             + (f", error `{entry['error_code']}`" if entry.get("error_code") else "")
                             + (f", count {entry['count']}" if entry.get("count") is not None else ""))
    return "\n".join(lines) + "\n"


def summary(args):
    if type(args.commit) is not str or not re.fullmatch(r"[0-9a-f]{40}", args.commit):
        raise DeployError("ARGUMENT_INVALID", argument="commit")
    text = summary_markdown(args.commit, args.zip, load_report(args.deploy_report), load_report(args.smoke_report))
    if args.output:
        with open(args.output, "a", encoding="utf-8") as stream:
            stream.write(text)
    else:
        sys.stdout.write(text)
    return 0


# --- CLI ------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    config = commands.add_parser("check-config")
    config.add_argument("--region", required=True)
    config.add_argument("--function", action="append", required=True, metavar="ROLE=NAME")
    config.set_defaults(handler=check_config)
    update = commands.add_parser("deploy")
    update.add_argument("--region", required=True)
    update.add_argument("--zip", required=True)
    update.add_argument("--function", action="append", required=True, metavar="ROLE=NAME",
                        help="deployment order is the argument order")
    update.add_argument("--manifest", default=None)
    update.add_argument("--report", default=None)
    update.set_defaults(handler=deploy)
    probe = commands.add_parser("smoke")
    probe.add_argument("--base-url", required=True)
    probe.add_argument("--report", default=None)
    probe.set_defaults(handler=smoke)
    digest = commands.add_parser("summary")
    digest.add_argument("--commit", required=True)
    digest.add_argument("--zip", required=True)
    digest.add_argument("--deploy-report", default=None)
    digest.add_argument("--smoke-report", default=None)
    digest.add_argument("--output", default=None)
    digest.set_defaults(handler=summary)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except DeployError as error:
        emit(code=error.code, **error.detail)
        return error.exit_code
    except Exception:
        # No traceback: it could carry an ARN, a response body or an environment value.
        emit(code="DEPLOY_STEP_FAILED", command=args.command)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())

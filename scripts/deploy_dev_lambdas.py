"""Dev Lambda deployment steps of .github/workflows/deploy_dev.yml (D137, D140).

Standard library plus boto3 (imported only when a client is created). Five
subcommands, each used by one workflow step:

* ``registry``: write the checked-out code's execution versions
  (``current_adapter_version``, ``retained_adapter_versions``,
  ``projection_version``) to a JSON file. The build step stores it next to the
  ZIP, so an artifact always carries the registry of the code inside it.
* ``check-config``: the ARC_JOURNEY_CONFIG of each named function must be a JSON
  object with the requested role and an ``execution`` object. Nothing is
  written. When only the execution block differs from the artifact's registry
  (``--registry``) the step passes and names the fields the deploy step will
  sync (D140). Anything else exits 2 (CONFIG_DRIFT) and names only the role,
  the field and the expectation; the configuration JSON, account, resource
  names and other environment variables are never printed. When the artifact's
  registry is the checked-out code's registry, the document (with the execution
  block already aligned) must also pass ``AwsSettings.parse`` offline; for a
  rollback artifact of another revision that parse is skipped, because the
  checked-out parser is not the one the artifact's code will run.
* ``deploy``: in the given order, per function: for API and Worker first make
  the execution block of ARC_JOURNEY_CONFIG equal the artifact's registry
  (``update_function_configuration`` with the environment map exactly as read
  and only that one block replaced, wait, read back), then
  ``update_function_code`` (ZIP bytes, no version publish), wait for the
  update and compare the function's CodeSha256 with the ZIP's own digest
  (exit 4, CODE_SHA_MISMATCH). A configuration that already matches is not
  written. The first failure stops the run; functions already updated and the
  configuration state of each role stay in the report. A missing
  ``lambda:UpdateFunctionConfiguration`` permission exits 6
  (CONFIG_SYNC_DENIED) before that function's code is replaced.
* ``smoke``: public Dummy login and course list through the deployed API. The
  Dummy logout is never called (it resets the shared Dummy progress). Only HTTP
  statuses, counts and fixed error codes are printed, never a token or a body.
* ``summary``: Markdown for $GITHUB_STEP_SUMMARY from the report files.

No output line ever contains a token, a signed URL, an environment variable
value or the configuration document. Exit codes: 0 ok, 2 CONFIG_DRIFT,
3 other fixed codes (REGISTRY_MISSING, CONFIG_SYNC_FAILED, AWS_CALL_FAILED, ...),
4 CODE_SHA_MISMATCH, 5 SMOKE_FAILED, 6 CONFIG_SYNC_DENIED.
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

# The workflow runs this file as `python scripts/deploy_dev_lambdas.py`, which puts
# scripts/ (not the repository root) first on sys.path; check-config imports
# mock_journey from the root.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

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
# D140: the execution block of ARC_JOURNEY_CONFIG is the only configuration this
# script writes; its three fields come from the artifact's registry file.
REGISTRY_FIELDS = ("current_adapter_version", "retained_adapter_versions", "projection_version")
VERSION_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
MAX_REGISTRY_BYTES = 64 * 1024
MAX_RETAINED_VERSIONS = 64
ACCESS_DENIED_CODES = ("AccessDeniedException", "AccessDenied")
CONFIG_STATES = ("synced", "unchanged", "unconfirmed", "not_applicable")
EXIT_CODES = {"CONFIG_DRIFT": 2, "REGISTRY_MISSING": 3, "CONFIG_SYNC_FAILED": 3, "CODE_SHA_MISMATCH": 4,
              "SMOKE_FAILED": 5, "CONFIG_SYNC_DENIED": 6}


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


# --- registry -------------------------------------------------------------

def write_registry(args):
    """The execution versions of the checked-out code, stored next to the ZIP built from it."""
    registry = expected_execution()
    try:
        Path(args.output).write_text(json.dumps(registry, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    except OSError:
        raise DeployError("REGISTRY_WRITE_FAILED") from None
    # Code constants only (the same values are in mock_journey/contracts.py).
    emit(registry="written", **registry)
    return 0


def load_registry(path):
    """The artifact's execution registry, or REGISTRY_MISSING (absent file, other shape, other value types).

    An artifact built before D140 has no such file, so it cannot be rolled back
    to automatically.
    """
    try:
        data = Path(path).read_bytes()
        document = json.loads(data.decode("utf-8")) if len(data) <= MAX_REGISTRY_BYTES else None
    except (OSError, ValueError, RecursionError):
        raise DeployError("REGISTRY_MISSING") from None
    if type(document) is not dict or set(document) != set(REGISTRY_FIELDS):
        raise DeployError("REGISTRY_MISSING")
    retained = document["retained_adapter_versions"]
    names = [document["current_adapter_version"], document["projection_version"],
             *(retained if type(retained) is list else [None])]
    if (type(retained) is not list or len(retained) > MAX_RETAINED_VERSIONS
            or any(type(name) is not str or not VERSION_NAME.fullmatch(name) for name in names)):
        raise DeployError("REGISTRY_MISSING")
    return {field: document[field] for field in REGISTRY_FIELDS}


# --- configuration (shared by check-config and deploy) --------------------

def strict_object(raw):
    """The JSON object of a configuration value, or None.

    A repeated key or NaN/Infinity is rejected: such a text could not be written
    back with only its execution block changed.
    """
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    def constant(_):
        raise ValueError

    try:
        document = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, RecursionError):
        return None
    return document if type(document) is dict else None


def configuration_plan(configuration, role, registry):
    """(environment variables, raw document, parsed document, execution fields to sync) of one function.

    Everything except the values inside the execution block must already be in
    place; otherwise CONFIG_DRIFT names the role, the field and the expectation.
    """
    environment = configuration.get("Environment") if isinstance(configuration, dict) else None
    variables = environment.get("Variables") if isinstance(environment, dict) else None
    raw = variables.get(CONFIG_VARIABLE) if isinstance(variables, dict) else None
    if type(raw) is not str or not raw:
        raise DeployError("CONFIG_DRIFT", role=role, field=CONFIG_VARIABLE, expected="present")
    document = strict_object(raw)
    if document is None:
        raise DeployError("CONFIG_DRIFT", role=role, field=CONFIG_VARIABLE, expected="json_object")
    if document.get("role") != role:
        raise DeployError("CONFIG_DRIFT", role=role, field="role", expected=role)
    fields = []
    if role in EXECUTION_ROLES:
        execution = document.get("execution")
        if type(execution) is not dict:
            raise DeployError("CONFIG_DRIFT", role=role, field="execution", expected="object")
        fields = ["execution." + field for field in REGISTRY_FIELDS
                  if field not in execution or execution[field] != registry[field]]
        if set(execution) - set(REGISTRY_FIELDS):
            # The block is replaced as a whole; other key names are not printed.
            fields.append("execution.extra_keys")
    return variables, raw, document, fields


def synced_document(document, registry, role):
    """The document as compact JSON text with only its execution block replaced by the registry."""
    execution = {field: registry[field] for field in document["execution"] if field in registry}
    execution.update((field, registry[field]) for field in REGISTRY_FIELDS if field not in execution)
    updated = {key: (execution if key == "execution" else value) for key, value in document.items()}
    raw = json.dumps(updated, separators=(",", ":"), ensure_ascii=False)
    # Invariant before anything is written: the new text reads back as the old
    # document in every key but `execution`, in the same key order.
    check = strict_object(raw)
    if (check is None or list(check) != list(document) or check["execution"] != registry
            or any(check[key] != document[key] for key in document if key != "execution")):
        raise DeployError("CONFIG_SYNC_FAILED", role=role, reason="document_not_preserved")
    return raw


def validate_settings(raw, role):
    from mock_journey.aws_settings import AwsSettings

    try:
        AwsSettings.parse(raw, role)
    except Exception:
        raise DeployError("CONFIG_DRIFT", role=role, field="AwsSettings.parse", expected="valid") from None


# --- check-config ---------------------------------------------------------

def check_role_configuration(client, role, name, registry, *, parse):
    try:
        configuration = client.get_function_configuration(FunctionName=name)
    except Exception:
        raise DeployError("AWS_CALL_FAILED", role=role, call="GetFunctionConfiguration") from None
    _, raw, document, fields = configuration_plan(configuration, role, registry)
    if parse:
        # The document the deployed code will read: execution already aligned.
        validate_settings(synced_document(document, registry, role) if fields else raw, role)
    if fields:
        return {"role": role, "check": "execution_will_be_synced", "fields": fields}
    return {"role": role, "check": "config_matches_code_registry"}


def check_config(args):
    region = parse_region(args.region)
    functions = parse_functions(args.function)
    registry = load_registry(args.registry)
    # AwsSettings.parse belongs to the checked-out revision. It can judge the
    # document only when the artifact was built from that same registry.
    parse = registry == expected_execution()
    client = lambda_client(region)
    if not parse:
        emit(check="settings_parse_skipped", reason="registry_differs_from_checkout")
    for role, name in functions:
        emit(**check_role_configuration(client, role, name, registry, parse=parse))
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


def error_code(error):
    """The service error code of a botocore ClientError-shaped exception; never its message."""
    response = getattr(error, "response", None)
    detail = response.get("Error") if isinstance(response, dict) else None
    code = detail.get("Code") if isinstance(detail, dict) else None
    return code if type(code) is str else None


def sync_failure(state, code, **detail):
    error = DeployError(code, **detail)
    error.config_state = state  # for the report only; never printed
    return error


def sync_role_configuration(client, role, name, registry):
    """Align the role's execution block with the registry right before its code is replaced (D140).

    Returns "synced" or "unchanged". The environment map is sent back exactly as
    read, with only the execution block of ARC_JOURNEY_CONFIG replaced; no value
    is printed or stored in the report.
    """
    try:
        configuration = client.get_function_configuration(FunctionName=name)
    except Exception:
        raise sync_failure("unchanged", "AWS_CALL_FAILED", role=role, call="GetFunctionConfiguration") from None
    try:
        variables, _, document, fields = configuration_plan(configuration, role, registry)
    except DeployError as error:
        error.config_state = "unchanged"
        raise
    if not fields:
        return "unchanged"
    if any(type(key) is not str or type(value) is not str for key, value in variables.items()):
        raise sync_failure("unchanged", "CONFIG_SYNC_FAILED", role=role, reason="environment_not_text")
    try:
        updated = dict(variables)
        updated[CONFIG_VARIABLE] = synced_document(document, registry, role)
    except DeployError as error:
        error.config_state = "unchanged"
        raise
    request = {"FunctionName": name, "Environment": {"Variables": updated}}
    revision = configuration.get("RevisionId")
    if type(revision) is str and revision:
        # Refuse the write if the function changed after it was read.
        request["RevisionId"] = revision
    try:
        client.update_function_configuration(**request)
    except Exception as error:
        if error_code(error) in ACCESS_DENIED_CODES:
            raise sync_failure("unchanged", "CONFIG_SYNC_DENIED", missing_action="lambda:UpdateFunctionConfiguration",
                               role=role) from None
        raise sync_failure("unconfirmed", "AWS_CALL_FAILED", role=role, call="UpdateFunctionConfiguration") from None
    try:
        client.get_waiter("function_updated_v2").wait(FunctionName=name, WaiterConfig=dict(WAITER_CONFIG))
        after = client.get_function_configuration(FunctionName=name)
    except Exception:
        raise sync_failure("unconfirmed", "AWS_CALL_FAILED", role=role, call="UpdateFunctionConfiguration") from None
    environment = after.get("Environment") if isinstance(after, dict) else None
    stored = environment.get("Variables") if isinstance(environment, dict) else None
    # Every variable must read back as sent: the execution block now matches
    # the registry and nothing else moved.
    if stored != updated:
        raise sync_failure("unconfirmed", "CONFIG_SYNC_FAILED", role=role, reason="read_back_differs")
    return "synced"


def progress(report):
    """Safe progress fields of a failure line: roles whose code was replaced, roles whose config was written."""
    detail = {"updated": [entry["role"] for entry in report["functions"] if entry.get("code_sha256_matches")]}
    synced = [role for role, state in report["config"].items() if state == "synced"]
    if synced:
        detail["config_synced"] = synced
    return detail


def deploy(args):
    region = parse_region(args.region)
    functions = parse_functions(args.function)
    data, hex_digest, code_sha = read_artifact(args.zip, args.manifest)
    registry = load_registry(args.registry)
    client = lambda_client(region)
    report = {"zip_sha256": hex_digest, "expected_code_sha256": code_sha, "functions": [], "config": {},
              "status": "started"}
    write_report(args.report, report)

    def stop(role, error):
        report.update(status="failed", failed_role=role, code=error.code)
        write_report(args.report, report)
        return error

    # Before the first write: every target must be a ZIP-packaged python3.12
    # function, and API/Worker must differ from the registry in nothing but the
    # execution block.
    targets = []
    for role, name in functions:
        try:
            configuration = client.get_function_configuration(FunctionName=name)
        except Exception:
            raise stop(role, DeployError("AWS_CALL_FAILED", role=role, call="GetFunctionConfiguration")) from None
        if (not isinstance(configuration, dict) or configuration.get("PackageType") != "Zip"
                or configuration.get("Runtime") != "python3.12"):
            raise stop(role, DeployError("FUNCTION_UNSUPPORTED", role=role))
        if role in EXECUTION_ROLES:
            try:
                configuration_plan(configuration, role, registry)
            except DeployError as error:
                raise stop(role, error) from None
        targets.append((role, name))
    for role, name in targets:
        state = "not_applicable"
        if role in EXECUTION_ROLES:
            try:
                state = sync_role_configuration(client, role, name, registry)
            except DeployError as error:
                report["config"][role] = getattr(error, "config_state", "unconfirmed")
                if error.code != "CONFIG_SYNC_DENIED":
                    error.detail.update(progress(report))
                raise stop(role, error) from None
            report["config"][role] = state
            write_report(args.report, report)
        try:
            client.update_function_code(FunctionName=name, ZipFile=data, Publish=False)
            client.get_waiter("function_updated_v2").wait(FunctionName=name, WaiterConfig=dict(WAITER_CONFIG))
            configuration = client.get_function_configuration(FunctionName=name)
        except Exception:
            raise stop(role, DeployError("AWS_CALL_FAILED", role=role, call="UpdateFunctionCode",
                                         **progress(report))) from None
        entry = function_entry(role, name, configuration)
        entry["config"] = state
        report["functions"].append(entry)
        if entry["CodeSha256"] != code_sha:
            entry["code_sha256_matches"] = False
            raise stop(role, DeployError("CODE_SHA_MISMATCH", role=role, **progress(report)))
        entry["code_sha256_matches"] = True
        write_report(args.report, report)
        emit(FunctionName=name, CodeSha256=entry["CodeSha256"], LastModified=entry["LastModified"], config=state)
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
    lines = ["## Dev deployment (D137, D140)", ""]
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
        states = deploy_report.get("config")
        if isinstance(states, dict) and states:
            # D140: whether the execution block of ARC_JOURNEY_CONFIG was written (never its content).
            lines.append("- ARC_JOURNEY_CONFIG execution block: " + ", ".join(
                f"{role} {states[role] if states[role] in CONFIG_STATES else '-'}"
                for role in EXECUTION_ROLES if role in states))
        lines.extend(["", "| role | function | config | CodeSha256 | LastModified | matches ZIP |",
                      "|---|---|---|---|---|---|"])
        for entry in deploy_report.get("functions") or ():
            if not isinstance(entry, dict):
                continue
            lines.append("| " + " | ".join(cell(entry.get(key)) for key in
                                           ("role", "FunctionName", "config", "CodeSha256", "LastModified",
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
    versions = commands.add_parser("registry")
    versions.add_argument("--output", required=True)
    versions.set_defaults(handler=write_registry)
    config = commands.add_parser("check-config")
    config.add_argument("--region", required=True)
    config.add_argument("--function", action="append", required=True, metavar="ROLE=NAME")
    config.add_argument("--registry", required=True, help="execution-registry.json of the artifact to deploy")
    config.set_defaults(handler=check_config)
    update = commands.add_parser("deploy")
    update.add_argument("--region", required=True)
    update.add_argument("--zip", required=True)
    update.add_argument("--function", action="append", required=True, metavar="ROLE=NAME",
                        help="deployment order is the argument order")
    update.add_argument("--registry", required=True, help="execution-registry.json of the artifact to deploy")
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

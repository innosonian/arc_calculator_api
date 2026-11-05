#!/usr/bin/env python3
"""Compare reference/current calculation results in isolated, offline workers.

Run with the project's Python interpreter. This never calls lambda_handler.run
or submission functions. It compares main.run_calculator and then the result
after _convert_result_to_legacy, including the chart upload payload and ordered
serialized JSON bytes. Certification, HTTP parsing,
authentication, and submission responses require separate contract tests.

Reference code is read only: -B/PYTHONDONTWRITEBYTECODE disable bytecode writes;
workers use STAGE=test and block AWS clients/resources and socket operations
with BaseException subclasses, which existing except Exception blocks cannot
silently swallow. Worker environment variables are an explicit allowlist and
never copied from the invoking process's credentials or submit configuration.
"""

import argparse
import ast
import base64
import contextlib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys


DEFAULT_REFERENCE = Path("/Users/mun/Innosonian/hstm_v2_calcuator_api")
REFERENCE_COMMIT = "92f56dc344917fdbd0d9205a48e12f821270c2bc"
REFERENCE_AUTHORITY = "hstm_v2 92f56dc legacy path, byte-identical"
CURRENT_ROOT = Path(__file__).resolve().parents[1]
# Deliberate literals, not config.guideline_registry: the worker below also runs
# inside the reference checkout (sys.path points there), where that module does
# not exist. The tuple order feeds case ID generation, so keep it as is;
# tests/test_guideline_registry.py checks these sets against the registry.
GUIDELINES = ("ARC2020", "ARC2025", "AHA2020", "ERC2020", "STD2015")
TARGETS = ("adult", "child", "infant")
TRAINING_TYPES = ("cpr", "compression_only", "ventilation_only")
ARC_REGIONS = ("usa", "british")


class ForbiddenSideEffect(BaseException):
    pass


class _DiscardLogs:
    def write(self, text):
        return len(text)

    def flush(self):
        pass


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=True, allow_nan=False).encode("utf-8")


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _worker(repo, guard_only=False, reference=False):
    """Only this subprocess imports calculator code; no HTTP handler is invoked."""
    import socket

    # urllib3 otherwise probes IPv6 support by binding a loopback socket during
    # import. Skip that capability probe instead of opening any socket here.
    socket.has_ipv6 = False
    sys.dont_write_bytecode = True
    os.environ["STAGE"] = "test"
    os.environ["AWS_EC2_METADATA_DISABLED"] = "true"
    for name in ("SENTRY_DSN", "ARC_SUBMIT_LAMBDA_NAME", "HSTM_SUBMIT_LAMBDA_NAME"):
        os.environ.pop(name, None)

    blocked = []

    def forbid(label):
        def reject(*args, **kwargs):
            blocked.append(label)
            raise ForbiddenSideEffect(label)
        return reject

    socket_operations = (
        "create_connection", "getaddrinfo", "gethostbyname", "gethostbyname_ex",
        "gethostbyaddr", "getnameinfo",
    )
    for name in socket_operations:
        setattr(socket, name, forbid("socket." + name))
    for name in ("connect", "connect_ex", "bind", "listen", "sendto", "sendmsg"):
        if hasattr(socket.socket, name):
            setattr(socket.socket, name, forbid("socket.socket." + name))

    # Guard lower-level network calls, subprocesses, and filesystem mutations.
    # Result artifacts are written by the parent, never from either calculator.
    def audit(event, args):
        denied = event in (
            "socket.connect", "socket.bind", "socket.getaddrinfo", "subprocess.Popen", "os.system",
            "os.mkdir", "os.remove", "os.rmdir", "os.rename", "os.chmod", "os.utime", "os.link", "os.symlink",
        )
        if event == "open":
            flags = args[2] if len(args) > 2 else 0
            denied = bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC))
        if denied:
            label = "audit." + event
            blocked.append(label)
            raise ForbiddenSideEffect(label)

    sys.addaudithook(audit)
    sys.path.insert(0, str(repo))
    reply = {"guard_self_test": {}, "cases": []}
    with contextlib.redirect_stdout(_DiscardLogs()), contextlib.redirect_stderr(_DiscardLogs()):
        try:
            import boto3

            boto3.client = forbid("boto3.client")
            boto3.resource = forbid("boto3.resource")
            # Also prevent bypassing the top-level factory through a Session.
            boto3.session.Session.client = forbid("boto3.session.Session.client")
            boto3.session.Session.resource = forbid("boto3.session.Session.resource")

            import util.uploader

            util.uploader.client = forbid("util.uploader.client")
            checks = {
                "boto3.client": lambda: boto3.client("s3"),
                "boto3.resource": lambda: boto3.resource("dynamodb"),
                "session.client": lambda: boto3.session.Session.client(None, "s3"),
                "session.resource": lambda: boto3.session.Session.resource(None, "dynamodb"),
                "uploader.client": lambda: util.uploader.client("s3"),
                "socket.connect": lambda: socket.socket.connect(None, ("example.invalid", 443)),
                "socket.connect_ex": lambda: socket.socket.connect_ex(None, ("example.invalid", 443)),
                "socket.create_connection": lambda: socket.create_connection(("example.invalid", 443)),
                "socket.dns": lambda: socket.getaddrinfo("example.invalid", 443),
                "filesystem.write_detection": lambda: audit("open", ("unused-probe-path", "w", os.O_WRONLY | os.O_CREAT)),
            }
            for name, check in checks.items():
                try:
                    check()
                except ForbiddenSideEffect:
                    reply["guard_self_test"][name] = "blocked"
                else:
                    raise RuntimeError("Safety guard failed: " + name)
            blocked.clear()

            if not guard_only:
                from main import run_calculator
                import data_handlers.chart_data as chart_data

                if reference:
                    # Compile only these two self-contained functions. Importing
                    # the reference HTTP module would initialize unrelated scoring.
                    path = repo / "lambda_handler.py"
                    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                    names = {"_convert_result_to_legacy", "_build_result_by_criteria"}
                    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                                 and node.name in names]
                    if len(functions) != 2 or {node.name for node in functions} != names:
                        raise RuntimeError("Reference compatibility functions are missing or ambiguous.")
                    namespace = {}
                    exec(compile(ast.Module(body=functions, type_ignores=[]), str(path), "exec"), namespace)
                    convert = namespace["_convert_result_to_legacy"]
                    build_criteria = namespace["_build_result_by_criteria"]
                else:
                    from services.legacy_response import _convert_result_to_legacy as convert
                    from services.legacy_document import _build_result_by_criteria as build_criteria
                # User-approved ARC null contract: exactly this one explicit
                # metric, separately from all unmodified calculation records.
                reply["compatibility_probe"] = {
                    "ResultByCriteria": build_criteria({}, {"metrics": {"VentilationSpeed": None}}),
                }
                reply["legacy_conversion_available"] = callable(convert)
                charts = []

                def capture_chart(data, *args, **kwargs):
                    # Capture the actual upload argument, retaining STAGE=test's
                    # None return so signed URLs and storage remain disabled.
                    charts.append(_json_bytes(data).decode("utf-8"))
                    return None

                chart_data.upload_json_file = capture_chart
                cases = json.load(sys.stdin)
                for case in cases:
                    charts.clear()
                    record = {"id": case["id"]}
                    stage = "main.run_calculator"
                    try:
                        result = run_calculator(
                            base64.b64decode(case["cpr_b64"]),
                            base64.b64decode(case["aed_b64"]),
                            dict(case["condition"]),
                            vp_event_list=case["vp_event_list"],
                            usage=case.get("usage"),
                            stage="test",
                        )
                        main_json = _json_bytes(result).decode("utf-8")
                        record["main_result"] = json.loads(main_json)
                        stage = "_convert_result_to_legacy"
                        legacy_json = _json_bytes(convert(result, dict(case["condition"]))).decode("utf-8")
                        record["http_calculation_result"] = json.loads(legacy_json)
                        if len(charts) != 1:
                            raise RuntimeError("Expected exactly one chart upload capture.")
                        record["chart_json"] = json.loads(charts[0])
                        # These are the serialized bytes from each output before
                        # any parse/transport step, including key order and -0.0.
                        record["serialized_json"] = {
                            "main_result": main_json,
                            "http_calculation_result": legacy_json,
                            "chart_json": charts[0],
                        }
                        record["status"] = "ok"
                    except ForbiddenSideEffect as error:
                        record.update(status="safety_blocked", stage=stage, error=str(error))
                    except Exception as error:
                        record.update(status="error", stage=stage, error_type=type(error).__name__, error=str(error))
                    reply["cases"].append(record)
                reply["unexpected_blocked_operations"] = list(blocked)
        except ForbiddenSideEffect as error:
            reply["bootstrap_error"] = {"type": "safety_blocked", "message": str(error)}
        except Exception as error:
            reply["bootstrap_error"] = {"type": type(error).__name__, "message": str(error)}
    print(json.dumps(reply, ensure_ascii=True, allow_nan=False))
    return 2 if reply.get("bootstrap_error") else 0


def _load_synth(current):
    # Import only the reviewed pure generator file, not tests/__init__/conftest.
    spec = importlib.util.spec_from_file_location("reference_parity_synth", current / "tests/_synth.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _condition(guideline, target, training_type, two_rescuers=False):
    return {
        "mode": "training", "target": target, "training_type": training_type,
        "guideline": guideline, "cpr_cycle_type": "152" if target == "infant" else "302",
        "is_2rescuers": two_rescuers,
    }


def _build_cases(current):
    synth = _load_synth(current)
    cases = []

    def add(case_id, group, condition, cpr, aed=b"", vp=None, source_files=None):
        regions = ARC_REGIONS if condition["guideline"].startswith("ARC") else ("british",)
        for region in regions:
            cases.append({
                "id": f"{case_id}/{region}", "group": group, "condition": dict(condition),
                "usage": {"Regional_Option": region},
                "cpr_b64": base64.b64encode(cpr).decode("ascii"),
                "aed_b64": base64.b64encode(aed).decode("ascii"),
                "cpr_sha256": _sha(cpr), "aed_sha256": _sha(aed),
                "cpr_bytes": len(cpr), "aed_bytes": len(aed),
                "vp_event_list": vp or [], "source_files": source_files or [],
            })

    for guideline in GUIDELINES:
        for target in TARGETS:
            comp_per_cycle = 15 if target == "infant" else 30
            data = {
                "cpr": synth.cpr_session([(comp_per_cycle, 2)] * 3),
                "compression_only": synth.comp_session(60),
                "ventilation_only": synth.vo_session(8),
            }
            for training_type in TRAINING_TYPES:
                add(f"matrix/{guideline}/{target}/{training_type}", "matrix",
                    _condition(guideline, target, training_type), data[training_type])

    # Boundary inputs use the same unmodified reference authority as all cases.
    for guideline in GUIDELINES:
        for target in TARGETS:
            count = 15 if target == "infant" else 30
            boundaries = {
                "one_compression_short": [(count - 1, 2), (count, 2), (count, 2)],
                "one_ventilation_short": [(count, 2), (count, 2), (count, 1)],
                "two_cycles": [(count, 2)] * 2,
                "sixty_compressions_twelve_ventilations": [(30, 6)] * 2,
            }
            for name, cycles in boundaries.items():
                add(f"boundary/{guideline}/{target}/{name}", "boundary",
                    _condition(guideline, target, "cpr"), synth.cpr_session(cycles))

    dataset = current / "tests/dataset"
    # Every recorded CPR input is crossed with every condition. AED companions
    # remain paired with their recording; result_*.json files are not live oracles.
    recorded = sorted(path for path in dataset.glob("*.bin") if not path.name.startswith("aed_"))
    for path in recorded:
        suffix = path.stem.removeprefix("cpr_")
        aed_path = dataset / f"aed_{suffix}.bin"
        aed = aed_path.read_bytes() if path.stem.startswith("cpr_") and aed_path.is_file() else b""
        sources = [str(path.relative_to(current))]
        if aed:
            sources.append(str(aed_path.relative_to(current)))
        cpr = path.read_bytes()
        for guideline in GUIDELINES:
            for target in TARGETS:
                for training_type in TRAINING_TYPES:
                    add(f"recorded/{path.stem}/{guideline}/{target}/{training_type}", "recorded",
                        _condition(guideline, target, training_type), cpr, aed, source_files=sources)

    for target in TARGETS:
        count = 15 if target == "infant" else 30
        data = synth.cpr_session([(count, 2)] * 3)
        # Generator starts at 100ms; each compression occupies 500ms.
        first_vent_start = 100 + count * 500
        variants = {
            "first_compression_segment_virtual": [
                {"event": 0, "timestamp": 99}, {"event": 1, "timestamp": first_vent_start},
            ],
            "all_virtual": [{"event": 0, "timestamp": 1}, {"event": 1, "timestamp": 1000000}],
        }
        for name, events in variants.items():
            add(f"vp/{target}/{name}", "vp", _condition("ARC2025", target, "cpr", True), data, vp=events)
    for target in ("child", "infant"):
        count = 15 if target == "infant" else 30
        for breaths in (4, 5):
            add(f"rescue/ERC2020/{target}/initial_{breaths}_breaths", "erc_rescue",
                _condition("ERC2020", target, "cpr"),
                synth.cpr_session([(0, breaths), (count, 2), (count, 2), (count, 2)]))
    return cases


def _source_manifest(repo):
    files = [repo / "main.py", repo / "lambda_handler.py"]
    for directory in ("calculators", "config", "data_handlers", "models", "services", "transformers", "util"):
        files.extend((repo / directory).rglob("*.py"))
    files.extend((repo / "resources/prompt_books").glob("*.json"))
    rows = {str(path.relative_to(repo)): _sha(path.read_bytes()) for path in sorted(set(files)) if path.is_file()}
    return {"aggregate_sha256": _sha(_json_bytes(rows)), "files": rows}


def _describe(value):
    name = "null" if value is None else type(value).__name__
    if isinstance(value, (list, dict)):
        return {"type": name, "length": len(value), "sha256": _sha(_json_bytes(value))}
    return {"type": name, "value": value}


def _differences(reference, current, path=""):
    """JSON Pointer paths; bool/int/float/null and signed float zero stay distinct."""
    def pointer(key):
        return path + "/" + str(key).replace("~", "~0").replace("/", "~1")

    if type(reference) is not type(current):
        return [{"path": path or "/", "kind": "type", "reference": _describe(reference), "current": _describe(current)}]
    diffs = []
    if isinstance(reference, dict):
        if list(reference) != list(current) and reference.keys() == current.keys():
            diffs.append({"path": path or "/", "kind": "key_order",
                          "reference": list(reference), "current": list(current)})
        for key in dict.fromkeys((*reference, *current)):
            if key not in reference:
                diffs.append({"path": pointer(key), "kind": "extra_key", "current": _describe(current[key])})
            elif key not in current:
                diffs.append({"path": pointer(key), "kind": "missing_key", "reference": _describe(reference[key])})
            else:
                diffs.extend(_differences(reference[key], current[key], pointer(key)))
    elif isinstance(reference, list):
        for index in range(max(len(reference), len(current))):
            if index >= len(reference):
                diffs.append({"path": pointer(index), "kind": "extra_item", "current": _describe(current[index])})
            elif index >= len(current):
                diffs.append({"path": pointer(index), "kind": "missing_item", "reference": _describe(reference[index])})
            else:
                diffs.extend(_differences(reference[index], current[index], pointer(index)))
    else:
        different = reference.hex() != current.hex() if isinstance(reference, float) else reference != current
        if different:
            diffs.append({"path": path or "/", "kind": "value", "reference": _describe(reference), "current": _describe(current)})
    return diffs


def _compatibility_comparison(reference, current):
    expected_reference = {"ResultByCriteria": {"VentilationSpeed": {}}}
    expected_current = {"ResultByCriteria": {"VentilationSpeed": None}}
    unexpected = (_differences(expected_reference, reference, "/reference")
                  + _differences(expected_current, current, "/current"))
    return {
        "authority": "User-approved ARC explicit-null contract retained for this one probe",
        "input": {"base": {}, "result": {"metrics": {"VentilationSpeed": None}}},
        "reference": reference, "current": current,
        "allowed_differences": [] if unexpected else _differences(reference, current),
        "unexpected_differences": unexpected,
    }


def _check_comparator():
    for reference, current in ((1, True), (1, 1.0), (None, 0), (-0.0, 0.0), ({}, {"x": None}), ([1], [1, 2]),
                               ({"a": 1, "b": 2}, {"b": 2, "a": 1})):
        if not _differences(reference, current):
            raise RuntimeError("Typed JSON comparison failed its self-test.")
    if _differences({"x": [1, 1.0, True, None]}, {"x": [1, 1.0, True, None]}):
        raise RuntimeError("Typed JSON comparison rejects equal values.")

    reference = {"ResultByCriteria": {"VentilationSpeed": {}}}
    current = {"ResultByCriteria": {"VentilationSpeed": None}}
    valid = _compatibility_comparison(reference, current)
    if len(valid["allowed_differences"]) != 1 or valid["unexpected_differences"]:
        raise RuntimeError("The approved null compatibility probe failed its self-test.")
    for bad in ({}, {"ResultByCriteria": {}}, {"ResultByCriteria": {"VentilationSpeed": {}}},
                {"ResultByCriteria": {"VentilationSpeed": False}},
                {"ResultByCriteria": {"VentilationSpeed": None, "extra": None}}):
        refused = _compatibility_comparison(reference, bad)
        if refused["allowed_differences"] or not refused["unexpected_differences"]:
            raise RuntimeError("Compatibility allowance accepted an unapproved difference.")


def _run_worker(repo, cases, guard_only=False, reference=False):
    env = {
        "PYTHONDONTWRITEBYTECODE": "1", "STAGE": "test",
        "AWS_EC2_METADATA_DISABLED": "true", "PYTHONIOENCODING": "utf-8",
    }
    command = [sys.executable, "-B", "-I", str(Path(__file__).resolve()), "--worker", str(repo)]
    if guard_only:
        command.append("--guard-check-only")
    if reference:
        command.append("--reference-worker")
    completed = subprocess.run(command, input=json.dumps(cases), capture_output=True,
                               text=True, encoding="utf-8", cwd=repo, env=env, timeout=600)
    try:
        result = json.loads(completed.stdout)
    except ValueError as error:
        raise RuntimeError(f"Worker returned no JSON: {repo}; exit={completed.returncode}") from error
    if completed.returncode or result.get("bootstrap_error"):
        raise RuntimeError(f"Worker initialization failed: {repo}: {result.get('bootstrap_error')}")
    return result


def _reference_commit(repo):
    command = ["git", "--no-optional-locks", "-C", str(repo)]
    head = subprocess.run([*command, "rev-parse", "HEAD"], check=True, capture_output=True,
                          text=True, timeout=30).stdout.strip()
    if head != REFERENCE_COMMIT:
        raise ValueError("Reference HEAD does not match the approved commit: " + REFERENCE_COMMIT)
    # HEAD alone does not authenticate modified or untracked calculation sources.
    dirty = subprocess.run([*command, "status", "--porcelain", "--untracked-files=all"],
                           check=True, capture_output=True, text=True, timeout=30).stdout
    if dirty:
        raise ValueError("Reference working tree must be clean at the approved commit.")
    return head


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference-dir", type=Path, default=DEFAULT_REFERENCE)
    parser.add_argument("--current-dir", type=Path, default=CURRENT_ROOT)
    # 결정 Q15=A(2026-09-28): 기본값으로 tests/fixtures/reference_parity 골든을 덮어쓰지 않도록 필수로 받는다.
    # 내부 --worker 하위 프로세스(_run_worker)는 파일을 쓰지 않고 이 인자도 받지 않으므로 파싱 뒤에 검사한다.
    parser.add_argument("--output-dir", type=Path,
                        help="Required. Write reports to a separate folder so golden fixtures are not overwritten "
                             "(골든 fixture 를 덮어쓰지 않도록 별도 폴더 지정).")
    parser.add_argument("--case", action="append", help="Case ID substring; repeat to select a union.")
    parser.add_argument("--guard-check-only", action="store_true")
    parser.add_argument("--no-oracle", action="store_true", help="Write report/manifest without full result fixtures.")
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--reference-worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    sys.dont_write_bytecode = True
    if args.worker:
        return _worker(args.worker.resolve(), args.guard_check_only, args.reference_worker)
    if args.output_dir is None:
        parser.error("the following arguments are required: --output-dir")

    reference = args.reference_dir.resolve()
    current = args.current_dir.resolve()
    output = args.output_dir.resolve()
    if output == reference or reference in output.parents:
        parser.error("Output must not be inside the read-only reference repository.")
    fixtures = current / "tests/fixtures"
    if output == fixtures or fixtures in output.parents:
        parser.error("Output must not overwrite checked-in golden fixtures; use a scratch folder.")
    if reference == current:
        parser.error("Reference and current repositories must be different.")
    try:
        head = _reference_commit(reference)
    except ValueError as error:
        parser.error(str(error))
    _check_comparator()
    before = {"reference": _source_manifest(reference), "current": _source_manifest(current)}
    generator_before = _sha((current / "tests/_synth.py").read_bytes())
    cases = [] if args.guard_check_only else _build_cases(current)
    if args.case:
        cases = [case for case in cases if any(value in case["id"] for value in args.case)]
        if not cases:
            parser.error("No cases matched --case.")

    # Sequential workers guarantee module isolation without sharing imported state.
    ref_results = _run_worker(reference, cases, args.guard_check_only, reference=True)
    cur_results = _run_worker(current, cases, args.guard_check_only)
    try:
        _reference_commit(reference)
    except ValueError as error:
        parser.error(str(error))
    after = {"reference": _source_manifest(reference), "current": _source_manifest(current)}
    stable = {name: before[name] == after[name] for name in before}
    generator_after = _sha((current / "tests/_synth.py").read_bytes())
    stable["generator"] = generator_before == generator_after
    comparisons = []
    for ref_case, cur_case in zip(ref_results["cases"], cur_results["cases"], strict=True):
        diffs = _differences(ref_case, cur_case)
        comparisons.append({"id": ref_case["id"], "reference_status": ref_case["status"],
                            "current_status": cur_case["status"],
                            "serialized_bytes_equal": _json_bytes(ref_case) == _json_bytes(cur_case),
                            "differences": diffs})
    compatibility = (None if args.guard_check_only else _compatibility_comparison(
        ref_results.get("compatibility_probe"), cur_results.get("compatibility_probe")))
    compatibility_differences = [] if compatibility is None else compatibility["unexpected_differences"]
    core_differences = sum(len(case["differences"]) for case in comparisons)
    errors = sum(case["reference_status"] != "ok" or case["current_status"] != "ok" for case in comparisons)
    different_cases = sum(bool(case["differences"]) for case in comparisons)
    blocked = ref_results.get("unexpected_blocked_operations", []) + cur_results.get("unexpected_blocked_operations", [])
    report = {
        "scope": "main.run_calculator, captured chart JSON, legacy conversion, and ordered serialized bytes; no HTTP handler, certification, or submission execution",
        "approved_exclusions": [], "guard_check_only": args.guard_check_only,
        "compatibility_probe": compatibility,
        "allowed_difference_count": 0 if compatibility is None else len(compatibility["allowed_differences"]),
        "core_difference_count": core_differences,
        "compatibility_difference_count": len(compatibility_differences),
        "source_stable_during_run": stable,
        "guard_self_tests": {"reference": ref_results["guard_self_test"], "current": cur_results["guard_self_test"]},
        "unexpected_blocked_operations": blocked,
        "legacy_conversion_available": {"reference": ref_results.get("legacy_conversion_available"),
                                        "current": cur_results.get("legacy_conversion_available")},
        "case_count": len(cases), "different_cases": different_cases, "execution_error_cases": errors,
        "raw_reference_difference_count": core_differences,
        "difference_count": core_differences + len(compatibility_differences), "cases": comparisons,
    }
    manifest = {
        "reference_root": str(reference), "reference_commit": head,
        "authority": REFERENCE_AUTHORITY, "current_root": str(current),
        "compatibility_allowance": None if args.guard_check_only else {
            "path": "/ResultByCriteria/VentilationSpeed", "reference": {}, "current": None,
            "authority": compatibility["authority"], "scope": "explicit-null compatibility probe only",
        },
        "source_before": before, "source_after": after,
        "generator_sha256": {"before": generator_before, "after": generator_after},
        "verifier_sha256": _sha(Path(__file__).read_bytes()),
        "cases": [{key: value for key, value in case.items() if key not in ("cpr_b64", "aed_b64")} for case in cases],
    }
    output.mkdir(parents=True, exist_ok=True)
    suffix = "guards" if args.guard_check_only else "parity"
    for name, data in ((f"{suffix}_report.json", report), (f"{suffix}_manifest.json", manifest)):
        (output / name).write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    if not args.guard_check_only and not args.no_oracle:
        for name, data in (("reference_results.json", ref_results), ("current_results.json", cur_results)):
            (output / name).write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("case_count", "different_cases", "execution_error_cases", "difference_count", "allowed_difference_count", "core_difference_count", "source_stable_during_run")}, ensure_ascii=False))
    print("Report: " + str(output / f"{suffix}_report.json"))
    if errors or blocked or not all(stable.values()):
        return 2
    return 1 if different_cases or compatibility_differences else 0


if __name__ == "__main__":
    raise SystemExit(main())

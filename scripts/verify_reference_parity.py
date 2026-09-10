#!/usr/bin/env python3
"""Compare reference/current calculation results in isolated, offline workers.

Run with the project's Python interpreter. This never calls lambda_handler.run
or submission functions. It compares main.run_calculator and then the result
after _convert_result_to_legacy, when available. Certification, HTTP parsing,
authentication, and submission responses require separate contract tests.

Reference code is read only: -B/PYTHONDONTWRITEBYTECODE disable bytecode writes;
workers use STAGE=test and block AWS clients/resources and socket operations
with BaseException subclasses, which existing except Exception blocks cannot
silently swallow. Worker environment variables are an explicit allowlist and
never copied from the invoking process's credentials or submit configuration.
"""

import argparse
import base64
import contextlib
from fractions import Fraction
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys


DEFAULT_REFERENCE = Path(
    "/Users/mac/Library/Mobile Documents/com~apple~CloudDocs/Innosonian/hstm_v2_calcuator_api"
)
CURRENT_ROOT = Path(__file__).resolve().parents[1]
GUIDELINES = ("ARC2020", "ARC2025", "AHA2020", "ERC2020", "STD2015")
TARGETS = ("adult", "child", "infant")
TRAINING_TYPES = ("cpr", "compression_only", "ventilation_only")
CHEST_NULL_KEYS = ("score_comp_depth", "score_recoil", "score_comp_no", "score_comp_count", "score_hand_position")
VENT_NULL_KEYS = ("score_vent_vol", "score_vent_rate", "score_vent_count", "score_vent_speed")


class ForbiddenSideEffect(BaseException):
    pass


class _DiscardLogs:
    def write(self, text):
        return len(text)

    def flush(self):
        pass


def _json_bytes(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False).encode("utf-8")


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _worker(repo, guard_only=False, reference_oracle=False):
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
                import lambda_handler
                import services.serializers

                convert = getattr(lambda_handler, "_convert_result_to_legacy", None)
                reply["legacy_conversion_available"] = callable(convert)
                original_coaching = services.serializers.build_guide_prompts
                observed_coaching = {}

                def observe_coaching(calculation_result, condition, usage):
                    # Observe the reference-only internal signal before the
                    # serializer removes it; do not modify the calculation.
                    observed_coaching["vent_rate_measured"] = (
                        calculation_result.get("cpr_total_scores") or {}
                    ).get("score_vent_rate_measured")
                    return original_coaching(calculation_result, condition, usage)

                services.serializers.build_guide_prompts = observe_coaching
                reply["approved_expectations"] = []
                cases = json.load(sys.stdin)
                for case in cases:
                    observed_coaching.clear()
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
                        record["main_result"] = json.loads(_json_bytes(result))
                        stage = "_convert_result_to_legacy"
                        if callable(convert):
                            result = convert(result, dict(case["condition"]))
                        record["http_calculation_result"] = json.loads(_json_bytes(result))
                        record["status"] = "ok"
                        if reference_oracle:
                            stage = "independent ARC exception oracle"
                            approved = _approved_exception(record, case, original_coaching,
                                                           observed_coaching.get("vent_rate_measured"))
                            if approved is not None:
                                reply["approved_expectations"].append(approved)
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
        cases.append({
            "id": case_id, "group": group, "condition": condition,
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

    # The same shortages must change ARC only, while non-ARC stays exact.
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
    recorded = [
        *[(f"cpr_{i}", f"cpr_{i}.bin", f"aed_{i}.bin", "adult", "cpr") for i in range(1, 6)],
        ("cco_1", "cco_1.bin", None, "adult", "compression_only"),
        ("vo_1", "vo_1.bin", None, "infant", "ventilation_only"),
        ("adult_vo_1", "adult_vo_1.bin", None, "adult", "ventilation_only"),
    ]
    for name, cpr_file, aed_file, target, training_type in recorded:
        condition = _condition("ARC2025", target, training_type)
        # Preserve the checked-in dataset's existing invocation conditions.
        condition["cpr_cycle_type"] = "302"
        sources = [f"tests/dataset/{cpr_file}"]
        if aed_file:
            sources.append(f"tests/dataset/{aed_file}")
        add(f"recorded/{name}", "recorded", condition, (dataset / cpr_file).read_bytes(),
            (dataset / aed_file).read_bytes() if aed_file else b"", source_files=sources)

    for target in TARGETS:
        count = 15 if target == "infant" else 30
        data = synth.cpr_session([(count, 2)] * 3)
        # Generator starts at 100ms; each compression occupies 100ms.
        first_vent_start = 100 + count * 100
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


def _rounded(value):
    return int(value + Fraction(1, 2))


def _arc_weights(target):
    # Independent rational transcription of the reference ARC CPR weights.
    values = ((12, 12, 12, 6, 3, 10, 26, 10, 9) if target == "infant"
              else (20, 15, 15, 5, 5, 25, 10, 5, 0))
    fields = ("score_comp_depth", "score_comp_rate", "score_recoil", "score_hand_position",
              "score_comp_count", "score_ccf", "score_vent_vol", "score_vent_count", "score_vent_speed")
    return {key: Fraction(value, 100) for key, value in zip(fields, values, strict=True)}


def _derive_arc_minimum_result(reference_result, case):
    """Construct the approved exception from reference numbers, never current output.

    Only null-group scores and their reweighted overall change here. Every
    metric, action, count, AED value, key, and non-group score remains a copied
    reference value. Coaching is derived separately by the guarded reference.
    """
    condition = case["condition"]
    if condition["guideline"] not in ("ARC2020", "ARC2025") or condition["training_type"] != "cpr":
        return None
    counts = reference_result["action_count"]
    minimum = 45 if condition["target"] == "infant" else 90
    chest_null = counts["comp"] < minimum
    vent_null = counts["vent"] < 6
    if not (chest_null or vent_null):
        return None
    if case.get("vp_event_list"):
        raise ValueError("An active ARC/VP exception needs an explicit independent actor-aware oracle.")
    result = json.loads(_json_bytes(reference_result))
    weights = _arc_weights(condition["target"])
    nulled_keys = (*CHEST_NULL_KEYS,) if chest_null else ()
    if vent_null:
        nulled_keys += VENT_NULL_KEYS
    retained_weights = {key: weight for key, weight in weights.items() if key not in nulled_keys}
    denominator = sum(retained_weights.values())

    def null_scores(score):
        for key in nulled_keys:
            if key in score:
                score[key] = None

    all_overalls = []
    audit_parts = []
    for part in result["cpr_score"]["part_scores"]:
        overalls = []
        cycle_audit = []
        for cycle in part["cycle_with_score_list"]:
            calc_case = cycle["calc_case"]
            if calc_case not in ("cpr", "only_comp", "only_vent", "not_calc"):
                raise ValueError("Unexpected ARC cycle kind: " + str(calc_case))
            numerator = Fraction(0)
            if calc_case != "not_calc":
                for key, weight in retained_weights.items():
                    comp_field = key in CHEST_NULL_KEYS or key == "score_comp_rate"
                    vent_field = key in VENT_NULL_KEYS
                    if comp_field and calc_case not in ("cpr", "only_comp"):
                        continue
                    if vent_field and calc_case not in ("cpr", "only_vent"):
                        continue
                    if key == "score_vent_count" and calc_case != "cpr":
                        continue
                    numerator += Fraction(str(cycle.get(key) or 0)) * weight
                overall = None if chest_null and vent_null else _rounded(numerator / denominator)
                cycle["overall"] = overall
                overalls.append(overall)
                all_overalls.append(overall)
                cycle_audit.append({"calc_case": calc_case, "numerator": str(numerator),
                                    "denominator": str(denominator), "expected_overall": overall})
            null_scores(cycle)
        null_scores(part["score"])
        if chest_null and vent_null:
            part["score"]["overall"] = None
        elif overalls:
            part["score"]["overall"] = _rounded(sum(overalls) / Fraction(len(overalls)))
        audit_parts.append({"part_num": part["part_num"], "cycles": cycle_audit,
                            "expected_part_overall": part["score"]["overall"]})
    total = result["cpr_score"]["total_score"]
    # The reference's total early return has precedence if no cycle is calculable.
    if all_overalls:
        null_scores(total)
        total["overall"] = None if chest_null and vent_null else _rounded(sum(all_overalls) / Fraction(len(all_overalls)))
    audit = {
        "policy": "ARC2020/ARC2025 CPR: adult/child 90, infant 45 compressions; 6 ventilations",
        "action_count": counts, "chest_null": chest_null, "vent_null": vent_null,
        "nulled_keys": list(nulled_keys), "parts": audit_parts,
        "expected_total_overall": total["overall"],
        "total_reference_early_return": not all_overalls,
    }
    return result, audit


def _approved_exception(reference_record, case, reference_coaching, measured_signal):
    main_expected = _derive_arc_minimum_result(reference_record["main_result"], case)
    if main_expected is None:
        return None
    expected = json.loads(_json_bytes(reference_record))
    audit = main_expected[1]
    for key in ("main_result", "http_calculation_result"):
        result, _ = _derive_arc_minimum_result(reference_record[key], case)
        coaching_total = dict(result["cpr_score"]["total_score"])
        coaching_total["score_vent_rate_measured"] = measured_signal
        result["guide_prompts"] = reference_coaching(
            {"cpr_total_scores": coaching_total, "cpr_metrics": result["metrics"]},
            case["condition"], case.get("usage"),
        )
        expected[key] = result
    return {"id": case["id"], "arithmetic": audit, "expected_record": expected,
            "coaching_source": "reference generator with independently reweighted total and unchanged measured signal"}


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
        for key in sorted(reference.keys() | current.keys()):
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


def _check_comparator():
    for reference, current in ((1, True), (1, 1.0), (None, 0), (-0.0, 0.0), ({}, {"x": None}), ([1], [1, 2])):
        if not _differences(reference, current):
            raise RuntimeError("Typed JSON comparison failed its self-test.")
    if _differences({"x": [1, 1.0, True, None]}, {"x": [1, 1.0, True, None]}):
        raise RuntimeError("Typed JSON comparison rejects equal values.")


def _run_worker(repo, cases, guard_only=False, reference_oracle=False):
    env = {
        "PYTHONDONTWRITEBYTECODE": "1", "STAGE": "test",
        "AWS_EC2_METADATA_DISABLED": "true", "PYTHONIOENCODING": "utf-8",
    }
    command = [sys.executable, "-B", "-I", str(Path(__file__).resolve()), "--worker", str(repo)]
    if guard_only:
        command.append("--guard-check-only")
    if reference_oracle:
        command.append("--reference-oracle")
    completed = subprocess.run(command, input=json.dumps(cases), capture_output=True,
                               text=True, encoding="utf-8", cwd=repo, env=env, timeout=180)
    try:
        result = json.loads(completed.stdout)
    except ValueError as error:
        raise RuntimeError(f"Worker returned no JSON: {repo}; exit={completed.returncode}") from error
    if completed.returncode or result.get("bootstrap_error"):
        raise RuntimeError(f"Worker initialization failed: {repo}: {result.get('bootstrap_error')}")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference-dir", type=Path, default=DEFAULT_REFERENCE)
    parser.add_argument("--current-dir", type=Path, default=CURRENT_ROOT)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--case", action="append", help="Case ID substring; repeat to select a union.")
    parser.add_argument("--guard-check-only", action="store_true")
    parser.add_argument("--no-oracle", action="store_true", help="Write report/manifest without full result fixtures.")
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--reference-oracle", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    sys.dont_write_bytecode = True
    if args.worker:
        return _worker(args.worker.resolve(), args.guard_check_only, args.reference_oracle)

    reference = args.reference_dir.resolve()
    current = args.current_dir.resolve()
    output = (args.output_dir or current / "tests/fixtures/reference_parity").resolve()
    if output == reference or reference in output.parents:
        parser.error("Output must not be inside the read-only reference repository.")
    if reference == current:
        parser.error("Reference and current repositories must be different.")
    _check_comparator()
    before = {"reference": _source_manifest(reference), "current": _source_manifest(current)}
    generator_before = _sha((current / "tests/_synth.py").read_bytes())
    cases = [] if args.guard_check_only else _build_cases(current)
    if args.case:
        cases = [case for case in cases if any(value in case["id"] for value in args.case)]
        if not cases:
            parser.error("No cases matched --case.")

    # Sequential workers guarantee module isolation without sharing imported state.
    ref_results = _run_worker(reference, cases, args.guard_check_only, reference_oracle=True)
    cur_results = _run_worker(current, cases, args.guard_check_only)
    after = {"reference": _source_manifest(reference), "current": _source_manifest(current)}
    stable = {name: before[name] == after[name] for name in before}
    generator_after = _sha((current / "tests/_synth.py").read_bytes())
    stable["generator"] = generator_before == generator_after
    comparisons = []
    approved = {row["id"]: row for row in ref_results.get("approved_expectations", [])}
    for ref_case, cur_case in zip(ref_results["cases"], cur_results["cases"], strict=True):
        expected = approved.get(ref_case["id"], {}).get("expected_record", ref_case)
        diffs = _differences(expected, cur_case)
        comparisons.append({"id": ref_case["id"], "reference_status": ref_case["status"],
                            "current_status": cur_case["status"], "approved_arc_minimum_exception": ref_case["id"] in approved,
                            "raw_reference_differences": _differences(ref_case, cur_case), "differences": diffs})
    errors = sum(case["reference_status"] != "ok" or case["current_status"] != "ok" for case in comparisons)
    different_cases = sum(bool(case["differences"]) for case in comparisons)
    blocked = ref_results.get("unexpected_blocked_operations", []) + cur_results.get("unexpected_blocked_operations", [])
    report = {
        "scope": "main.run_calculator plus legacy conversion only; no HTTP handler, certification, or submission execution",
        "approved_exclusions": [], "guard_check_only": args.guard_check_only,
        "approved_exception_case_count": len(approved),
        "approved_exception_arithmetic": {key: value["arithmetic"] for key, value in approved.items()},
        "source_stable_during_run": stable,
        "guard_self_tests": {"reference": ref_results["guard_self_test"], "current": cur_results["guard_self_test"]},
        "unexpected_blocked_operations": blocked,
        "legacy_conversion_available": {"reference": ref_results.get("legacy_conversion_available"),
                                        "current": cur_results.get("legacy_conversion_available")},
        "case_count": len(cases), "different_cases": different_cases, "execution_error_cases": errors,
        "raw_reference_difference_count": sum(len(case["raw_reference_differences"]) for case in comparisons),
        "difference_count": sum(len(case["differences"]) for case in comparisons), "cases": comparisons,
    }
    manifest = {
        "reference_root": str(reference), "current_root": str(current),
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
        (output / "approved_expectations.json").write_text(
            json.dumps({"cases": list(approved.values())}, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n",
            encoding="utf-8",
        )
        ref_results.pop("approved_expectations", None)
        cur_results.pop("approved_expectations", None)
        for name, data in (("reference_results.json", ref_results), ("current_results.json", cur_results)):
            (output / name).write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("case_count", "different_cases", "execution_error_cases", "difference_count", "source_stable_during_run")}, ensure_ascii=False))
    print("Report: " + str(output / f"{suffix}_report.json"))
    if errors or blocked or not all(stable.values()):
        return 2
    return 1 if different_cases else 0


if __name__ == "__main__":
    raise SystemExit(main())

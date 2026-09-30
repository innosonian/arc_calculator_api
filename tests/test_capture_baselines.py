"""scripts/capture_baselines.py (D125): placeholder rules, file formats, fixture-path refusal and a sample capture.

The expected templates below are written by hand from the documented rules,
not derived from the tool's output. The sample capture runs the tool as a
command on one golden (the v2 golden flow) with ``--check`` against the
repository fixtures; the whole capture is run by hand after an approved
change, not in the test suite.
"""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "capture_baselines.py"
FIXTURES = ROOT / "tests" / "fixtures"

U1, U2, U3 = ("11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222",
              "33333333-3333-4333-8333-333333333333")
V1, V2, V3 = ("aaaaaaaa-1111-4111-8111-111111111111", "bbbbbbbb-2222-4222-8222-222222222222",
              "cccccccc-3333-4333-8333-333333333333")
FIXED_HASH = "f" * 64          # a deterministic digest: equal in both runs, stays literal
DIGEST_A, DIGEST_B = "a" * 64, "b" * 64
HEX_A, HEX_B = "1" * 32, "2" * 32
SEEDED_STEM = "CPR-ACTION-1790563509"
SEEDED_NONCE = "seeded-nonce-value-kept-literal"


@pytest.fixture(scope="module")
def tool():
    spec = importlib.util.spec_from_file_location("capture_baselines_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _v2_observation(u1, u2, u3, digest, hexdigest, tail, nonce, stem, date, ms):
    return {
        "responses": [{"method": "POST", "path": f"/api/v2/attempts/{u1}/", "status": 201,
                       "body": {"accessToken": f"s1.{u2}.{tail}", "definitionHash": FIXED_HASH}}],
        "events": [{"role": "api", "category": "diagnostic", "event": "calc_complete",
                    "fields": {"elapsed_ms": ms, "job_id": u3, "level": "info"}}],
        "rows": [
            {"PK": f"JOB#{u3}", "SK": "STATE", "resume_nonce": nonce, "input_digest": digest,
             "key": f"arc/development/_no_org/{date}/{stem}-{u1}.json",
             "artifact": f"arc/_artifacts/{u3}-{u1}-final-1-{hexdigest}.bin", "scope_key": FIXED_HASH},
            {"PK": f"ATTEMPT#{u1}", "SK": "META", "attempt_id": u1, "created_at": 1800000000},
        ],
        "user_slots": {"USER#dummy-tester/STATE": {"slots_present": False}},
    }


def test_v2_placeholders_follow_the_documented_rules(tool):
    first = _v2_observation(U1, U2, U3, DIGEST_A, HEX_A, "tokenTailAAAAAAAAAAAAAAA", "nonceAAAAAAAAAAAAAAAA",
                            "CPR-ACTION-1800000000", "2027-01-15", 3)
    second = _v2_observation(V1, V2, V3, DIGEST_B, HEX_B, "tokenTailBBBBBBBBBBBBBBB", "nonceBBBBBBBBBBBBBBBB",
                             "CPR-ACTION-1800259200", "2027-01-18", 0)
    template = tool.template_of_sections(first, second, tool.V2_FLOW_SECTIONS)
    assert template == {
        "responses": [{"method": "POST", "path": "/api/v2/attempts/<uuid:1>/", "status": 201,
                       "body": {"accessToken": "s1.<uuid:2>.<secret>", "definitionHash": FIXED_HASH}}],
        "events": [{"role": "api", "category": "diagnostic", "event": "calc_complete",
                    "fields": {"elapsed_ms": "<ms>", "job_id": "<uuid:3>", "level": "info"}}],
        # Rows: placeholder key order (ATTEMPT before JOB), sorted keys, labels bound in the sections above.
        "rows": [
            {"PK": "ATTEMPT#<uuid:1>", "SK": "META", "attempt_id": "<uuid:1>", "created_at": 1800000000},
            {"PK": "JOB#<uuid:3>", "SK": "STATE",
             "artifact": "arc/_artifacts/<uuid:3>-<uuid:1>-final-1-<hex:1>.bin",
             "input_digest": "<h:1>", "key": "arc/development/_no_org/<date>/CPR-ACTION-<wallclock>-<uuid:1>.json",
             "resume_nonce": "<secret>", "scope_key": FIXED_HASH},
        ],
        "user_slots": {"USER#dummy-tester/STATE": {"slots_present": False}},
    }
    # Both runs yield this same template; a value the placeholders do not cover is reported with its path.
    assert tool.templatize_sections(second, tool.V2_FLOW_SECTIONS, tool.run_dependent_tokens(first, second)) == template
    second["events"][0]["fields"]["level"] = "warning"
    with pytest.raises(tool.CaptureError, match=r"\$\.events\[0\]\.fields\.level"):
        tool.template_of_sections(first, second, tool.V2_FLOW_SECTIONS)


def _writes(uuid, digest, stem, date, nonce, cursor):
    return [
        {"operation": "PutItem", "request": {"TableName": "<table>", "Item": {
            "PK": {"S": f"JOB#{uuid}"}, "SK": {"S": "STATE"}, "input_digest": {"S": digest},
            "key": {"S": f"arc/development/_no_org/{date}/{stem}-{uuid}.request.json"},
            "seeded_key": {"S": f"arc/development/_no_org/2026-09-28/{SEEDED_STEM}-{uuid}.json"},
            "resume_nonce": {"S": nonce}, "seeded_nonce_row": {"M": {"resume_nonce": {"S": SEEDED_NONCE}}},
            "scans": {"M": {"cursor": {"M": cursor}}}}}},
    ]


def test_write_request_placeholders_keep_seeded_values_and_ignore_cursor_key_order(tool):
    cursor_a = {"PK": {"S": "x"}, "SK": {"S": "y"}, "GSI1PK": {"S": "z"}, "GSI1SK": {"N": "1"}}
    cursor_b = {"GSI1PK": {"S": "z"}, "PK": {"S": "x"}, "GSI1SK": {"N": "1"}, "SK": {"S": "y"}}
    first = _writes(U1, DIGEST_A, "CPR-ACTION-1800000000", "2027-01-15", "nonceAAAAAAAAAAAAAAAA", cursor_a)
    second = _writes(V1, DIGEST_B, "CPR-ACTION-1800259200", "2027-01-18", "nonceBBBBBBBBBBBBBBBB", cursor_b)
    template = tool.template_of_list(first, second)
    item = template[0]["request"]["Item"]
    assert item["PK"] == {"S": "JOB#<uuid:1>"} and item["input_digest"] == {"S": "<h:1>"}
    assert item["key"] == {"S": "arc/development/_no_org/<date>/CPR-ACTION-<wallclock>-<uuid:1>.request.json"}
    # Seeded fixture values are the same in both runs and stay literal in the write template.
    assert item["seeded_key"] == {"S": f"arc/development/_no_org/2026-09-28/{SEEDED_STEM}-<uuid:1>.json"}
    assert item["resume_nonce"] == {"S": "<secret>"}
    assert item["seeded_nonce_row"] == {"M": {"resume_nonce": {"S": SEEDED_NONCE}}}
    assert item["scans"]["M"]["cursor"]["M"] == cursor_a  # the first run's key order is kept
    # Every other key-order difference is a real difference.
    second[0]["request"]["Item"] = dict(reversed(list(second[0]["request"]["Item"].items())))
    with pytest.raises(tool.CaptureError, match="keys"):
        tool.template_of_list(first, second)


@pytest.mark.parametrize("relative", [
    "v2_baseline/dummy_dev_definitions.json", "v2_baseline/flow_events.json", "v2_baseline/user_slots.json",
    "v2_baseline/legacy_compat.json", "v2_baseline/write_requests.json",
    "worker_call_order/spy_traces.json", "worker_call_order/memory_traces.json",
])
def test_file_formats_reproduce_the_fixture_bytes(tool, relative):
    path = FIXTURES / relative
    assert tool.render_file(relative, json.loads(path.read_text())) == path.read_text()


def test_output_dir_inside_the_repository_fixtures_is_refused(tool, tmp_path):
    for inside in (FIXTURES, FIXTURES / "v2_baseline", FIXTURES / "worker_call_order" / "new"):
        with pytest.raises(ValueError, match="must not be inside"):
            tool.refuse_fixture_path(inside)
        with pytest.raises(SystemExit) as raised:  # argparse error: nothing is captured or written
            tool.main(["--output-dir", str(inside), "--only", "worker_call_order:spy"])
        assert raised.value.code == 2
    assert tool.refuse_fixture_path(tmp_path / "captured") == (tmp_path / "captured").resolve()
    assert not (FIXTURES / "worker_call_order" / "new").exists()
    with pytest.raises(SystemExit) as raised:  # --output-dir is required
        tool.main(["--only", "worker_call_order:spy"])
    assert raised.value.code == 2


def test_check_tells_identical_equivalent_and_different_apart(tool, tmp_path):
    spy = json.loads((FIXTURES / "worker_call_order" / "spy_traces.json").read_text())
    rows = json.loads((FIXTURES / "v2_baseline" / "flow_rows.json").read_text())
    responses = json.loads((FIXTURES / "v2_baseline" / "flow_responses.json").read_text())
    events = json.loads((FIXTURES / "v2_baseline" / "flow_events.json").read_text())
    files = {"worker_call_order/spy_traces.json": spy, "v2_baseline/flow_responses.json": responses,
             "v2_baseline/flow_events.json": events, "v2_baseline/flow_rows.json": rows}
    report = {relative: verdict for relative, verdict, _ in tool.compare_with_fixtures(files, FIXTURES)}
    assert report == {relative: "identical" for relative in files}
    # Rows in another order with every label renumbered (consistently across the flow's three files,
    # which share one label space) are the same template.
    def renumbered(value):
        text = json.dumps(value)
        for number in range(1, 80):
            text = text.replace(f"<uuid:{number}>", f"<uuid:x{number + 500}>").replace(f"<h:{number}>", f"<h:x{number + 500}>")
        return json.loads(text.replace("<uuid:x", "<uuid:").replace("<h:x", "<h:"))

    files.update({"v2_baseline/flow_responses.json": renumbered(responses),
                  "v2_baseline/flow_events.json": renumbered(events),
                  "v2_baseline/flow_rows.json": renumbered(list(reversed(rows)))})
    assert files["v2_baseline/flow_rows.json"] != rows
    report = {relative: verdict for relative, verdict, _ in tool.compare_with_fixtures(files, FIXTURES)}
    assert report == {"worker_call_order/spy_traces.json": "identical", "v2_baseline/flow_responses.json": "equivalent",
                      "v2_baseline/flow_events.json": "equivalent", "v2_baseline/flow_rows.json": "equivalent"}
    # Renumbering the rows alone breaks the binding with the responses: that is a difference.
    files["v2_baseline/flow_responses.json"] = responses
    report = {relative: verdict for relative, verdict, _ in tool.compare_with_fixtures(files, FIXTURES)}
    assert report["v2_baseline/flow_rows.json"] == "different"
    # A changed literal, and a missing fixture file, are reported.
    changed = json.loads(json.dumps(spy))
    changed["legacy_execute_no_chart"]["trace"][0]["args"] = ["changed"]
    files = {"worker_call_order/spy_traces.json": changed, "worker_call_order/other_traces.json": spy}
    report = {relative: (verdict, detail) for relative, verdict, detail in tool.compare_with_fixtures(files, FIXTURES)}
    assert report["worker_call_order/spy_traces.json"][0] == "different"
    assert "legacy_execute_no_chart" in report["worker_call_order/spy_traces.json"][1]
    assert report["worker_call_order/other_traces.json"][0] == "missing"
    # --only subsets compare the captured entries only; an entry the fixture lacks is missing.
    slots = json.loads((FIXTURES / "v2_baseline" / "user_slots.json").read_text())
    report = tool.compare_with_fixtures({"v2_baseline/user_slots.json": {"flow": slots["flow"]}}, FIXTURES)
    assert report[0][1] == "equivalent"
    report = tool.compare_with_fixtures({"v2_baseline/user_slots.json": {"unknown": {}}}, FIXTURES)
    assert report[0][1] == "missing"


def test_sample_capture_of_the_v2_golden_flow_matches_the_fixture(tmp_path):
    """The tool as a command: two worker processes, the template and --check against the fixtures."""
    output = tmp_path / "captured"
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--output-dir", str(output), "--only", "v2_baseline:flow", "--check"],
        cwd=ROOT, capture_output=True, text=True, timeout=300,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    verdicts = {line.split()[1]: line.split()[0] for line in completed.stdout.splitlines() if not line.startswith("wrote")}
    assert verdicts == {"v2_baseline/flow_responses.json": "identical", "v2_baseline/flow_events.json": "identical",
                        "v2_baseline/flow_rows.json": "equivalent", "v2_baseline/user_slots.json": "equivalent"}
    written = sorted(path.relative_to(output).as_posix() for path in output.rglob("*.json"))
    assert written == ["v2_baseline/flow_events.json", "v2_baseline/flow_responses.json", "v2_baseline/flow_rows.json",
                       "v2_baseline/user_slots.json"]
    assert json.loads((output / "v2_baseline/user_slots.json").read_text()) == {
        "flow": {"USER#dummy-tester/STATE": {"slots_present": False}}}
    assert (output / "v2_baseline/flow_responses.json").read_bytes() == (FIXTURES / "v2_baseline/flow_responses.json").read_bytes()
    # The repository fixtures are untouched by a capture.
    assert not list(FIXTURES.rglob("run_*.json"))

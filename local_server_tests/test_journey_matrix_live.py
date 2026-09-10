"""All 15 product selections through the real default CLI and internal core."""

from copy import deepcopy
import hashlib
import json

import pytest

from local_server_tests.test_journey_runtime import JourneyServer
from local_server_tests.test_live_server import ROOT, require
from mock_journey.catalog import PROGRAMS, TARGETS
from mock_journey import typed
from tests._synth import multipart_event


@pytest.fixture(scope="module")
def matrix(tmp_path_factory):
    server = JourneyServer(tmp_path_factory.mktemp("arc-full-journey"))
    try:
        server.start()
        token = server.login()["session_token"]
        yield server, token
        server.assert_private_logs()
    finally:
        server.stop()


def reference(attempt, data, aed):
    """Unchanged core + response helper; only output capture is injected here.

    The child under test receives no hooks, adapters, fixture definitions or
    storage objects from this reference path.
    """
    from main import run_calculator
    from mock_journey.legacy_bridge import parse_measurement
    from services.calculation_context import AcceptedRaw, CalculationExecutionContext
    from services.legacy_response import DocumentSelection, finalize_legacy_response
    from services.submission_response import compose_calculation_snapshot
    from util.uploader import build_key_stem

    parts = {"rawHexBPfile": data, "condition": json.dumps(attempt["condition"])}
    if aed:
        parts["aedHexBPfile"] = aed
    parsed = parse_measurement(multipart_event(parts))
    charts = []
    context = CalculationExecutionContext(
        accepted_raw=AcceptedRaw(hashlib.sha256(data).hexdigest(), len(data),
                                 hashlib.sha256(aed).hexdigest(), len(aed), build_key_stem(), "_no_org"),
        publish_chart=lambda chart: charts.append(deepcopy(chart)), observe=lambda _: None,
    )
    core = run_calculator(data, aed, deepcopy(parsed["condition"]), parsed.get("vp_event_list") or [],
                          usage=parsed.get("Usage"), organization=parsed.get("Organization"),
                          stage="local", execution_context=context)
    final = finalize_legacy_response(parsed, core, document_selection=DocumentSelection("none"))
    require(len(charts) == 1, "Reference core must produce exactly one chart capture.")
    return json.loads(compose_calculation_snapshot(json.dumps(final).encode())), charts[0]


@pytest.mark.parametrize("program,target", [(p, t) for p in PROGRAMS for t in TARGETS],
                         ids=[f"{p[0]}-{t}" for p in PROGRAMS for t in TARGETS])
def test_every_program_target_returns_unchanged_core_and_real_chart(matrix, program, target):
    server, token = matrix
    ident, _, kind, required = program
    attempt = server.attempt(token, program=ident, target=target)
    condition = attempt["condition"]
    require(condition["guideline"] == "ARC2025" and condition["target"] == target,
            "Product selection must carry its approved target and guideline.")
    require(condition["cpr_cycle_type"] == ("152" if target == "infant" else "302"),
            "Approved age-specific CPR ratio must remain a string.")
    require(attempt["goal"] == {"kind": kind, "required": required}, "Program goal must remain the approved mock value.")
    filename = ("cco_1.bin" if kind == "compressions" else
                "vo_1.bin" if kind == "ventilations" and target == "infant" else
                "adult_vo_1.bin" if kind == "ventilations" else "cpr_1.bin")
    data = (ROOT / "tests/dataset" / filename).read_bytes()
    aed = (ROOT / "tests/dataset/aed_1.bin").read_bytes() if ident == "mock-two-rescuer-aed" else b""
    require(server.upload(token, attempt, data=data, aed=aed or None).status in (200, 202),
            "Every product selection must accept its real recorded measurement.")
    response = server.result(token, attempt)
    actual = response.json()
    expected, expected_chart = reference(attempt, data, aed)
    url = actual.pop("chart_dataset_url")
    expected.pop("chart_dataset_url")
    require(typed.canonical_bytes(actual) == typed.canonical_bytes(expected),
            "Default CLI changed a calculator value, JSON type, null, coaching or response field.")
    chart = server.chart(url)
    require(chart.status == 200, "Every returned chart must be retrievable over real HTTP.")
    require(typed.canonical_bytes(chart.json()) == typed.canonical_bytes(expected_chart),
            "Downloaded chart differs from the original core output.")
    require(hashlib.sha256(chart.body).hexdigest() == url.rsplit("/", 1)[-1].split(".")[4],
            "Downloaded bytes must match the signed content hash.")
    view = server.request("GET", f'/mock/v1/attempts/{attempt["attempt_id"]}', token=token)
    require(view.status == 200, "Program evaluation must have its own successful response.")
    assessment = view.json()["evaluation"]
    require(assessment["score"]["decision"] in ("pass", "fail"), "Score decision must remain independent of goal readiness.")
    if kind == "cycles":
        require(assessment["goal"] == {"kind": kind, "required": required, "observed": None,
                                       "met": None, "status": "pending_policy"},
                "Unresolved full-cycle policy must not prevent results or fabricate completion.")
        require(assessment["program_completed"] is False
                and "GOAL_POLICY_UNRESOLVED" in assessment["reason_codes"], "Pending CPR must not mark shared completion.")
    else:
        count = actual["action_count"]["comp" if kind == "compressions" else "vent"]
        require(assessment["goal"]["observed"] == count and assessment["goal"]["status"] == "evaluated",
                "Only goal assessment must use the actual measured action count.")
        require(assessment["program_completed"] is (count >= required and assessment["score"]["decision"] == "pass"),
                "Only completion must require both the goal and the existing tester Pass.")
    require(server.request("GET", attempt["calculation_path"], token=token).body == response.body,
            "Reading separate evaluation must not rewrite the stored calculation.")

"""All 15 Dummy Dev courses through the real default CLI, /api/v2 and internal core."""

from copy import deepcopy
import hashlib
import json

import pytest

from local_server_tests.test_journey_runtime import DUMMY_EXCLUDED, JourneyServer
from local_server_tests.test_live_server import ROOT, dummy_courses, require
from mock_journey import typed
from tests._synth import multipart_event

# Real loopback sockets: opted out of the directory network guard (conftest.py).
pytestmark = pytest.mark.loopback


COURSES = dummy_courses()


@pytest.fixture(scope="module")
def matrix(tmp_path_factory):
    server = JourneyServer(tmp_path_factory.mktemp("arc-full-journey"))
    try:
        server.start()
        token = server.token()
        yield server, token
        server.assert_private_logs()
    finally:
        server.stop()


def reference(condition, data, aed):
    """Unchanged core + legacy response helper; only output capture is injected here.

    The child under test receives no hooks, adapters, fixture definitions or
    storage objects from this reference path.
    """
    from main import run_calculator
    from mock_journey.legacy_bridge import parse_measurement
    from services.calculation_context import AcceptedRaw, CalculationExecutionContext
    from services.legacy_response import DocumentSelection, finalize_legacy_response
    from util.uploader import build_key_stem

    parts = {"rawHexBPfile": data, "condition": json.dumps(condition)}
    if aed:
        parts["aedHexBPfile"] = aed
    parsed = parse_measurement(multipart_event(parts))
    charts, evidence = [], []
    context = CalculationExecutionContext(
        accepted_raw=AcceptedRaw(hashlib.sha256(data).hexdigest(), len(data),
                                 hashlib.sha256(aed).hexdigest(), len(aed), build_key_stem(), "_no_org"),
        publish_chart=lambda chart: charts.append(deepcopy(chart)), observe=evidence.append,
    )
    core = run_calculator(data, aed, deepcopy(parsed["condition"]), parsed.get("vp_event_list") or [],
                          usage=parsed.get("Usage"), organization=parsed.get("Organization"),
                          stage="local", execution_context=context)
    final = finalize_legacy_response(parsed, core, document_selection=DocumentSelection("none"))
    require(len(charts) == 1, "Reference core must produce exactly one chart capture.")
    # D136: the closed ``cpr`` cycle count of the same run (mock_journey.cycle_goal rule, applied here
    # independently so the child's goal is compared with the evidence, not with itself).
    closed_cycles = sum(1 for cycle in evidence[0].cycles if cycle.calc_case == "cpr")
    return json.loads(json.dumps(final)), charts[0], closed_cycles


@pytest.mark.parametrize("course", COURSES, ids=[f"{c.program}-{c.target}" for c in COURSES])
def test_every_course_practice_returns_unchanged_core_and_real_chart(matrix, course):
    server, token = matrix
    attempt = server.start_attempt(token, course)
    condition = attempt["condition"]
    require(attempt["courseId"] == course.course_id and attempt["courseItemLinkId"] == course.practice_link_id
            and attempt["role"] == "training", "The start must pin the requested Dummy course practice.")
    require(condition["guideline"] == "ARC2025" and condition["target"] == course.target,
            "Course definition must carry its approved target and guideline.")
    require(condition["cpr_cycle_type"] == ("152" if course.target == "infant" else "302"),
            "Approved age-specific CPR ratio must remain a string.")
    filename = ("cco_1.bin" if course.kind == "compressions" else
                "vo_1.bin" if course.kind == "ventilations" and course.target == "infant" else
                "adult_vo_1.bin" if course.kind == "ventilations" else "cpr_1.bin")
    data = (ROOT / "tests/dataset" / filename).read_bytes()
    aed = (ROOT / "tests/dataset/aed_1.bin").read_bytes() if course.program == "mock-two-rescuer-aed" else b""
    require(server.upload(token, attempt, data=data, aed=aed or None).status in (200, 202),
            "Every Dummy course must accept its real recorded measurement.")
    response = server.result(token, attempt)
    result = response.data()
    actual = deepcopy(result["calculation"])
    expected, expected_chart, closed_cycles = reference(condition, data, aed)
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
    require(result["submit_arc"] == DUMMY_EXCLUDED, "Dummy results must stay excluded from ARC submission.")
    assessment = result["evaluation"]
    require(assessment["score"]["decision"] in ("pass", "fail"), "Score decision must remain independent of goal readiness.")
    if course.kind == "cycles":
        require(assessment["goal"] == {"kind": course.kind, "required": course.required, "observed": closed_cycles,
                                       "met": closed_cycles >= course.required, "status": "evaluated"},
                "CPR goal must count the calculator's closed cpr cycles (D136).")
        require(assessment["program_completed"] is (assessment["goal"]["met"]
                                                    and assessment["score"]["decision"] == "pass"),
                "CPR completion is cycle goal met AND score pass (D136).")
        require(("GOAL_NOT_MET" in assessment["reason_codes"]) is (not assessment["goal"]["met"])
                and "GOAL_POLICY_UNRESOLVED" not in assessment["reason_codes"],
                "Current CPR results carry no pending-policy reason (D136).")
    else:
        count = actual["action_count"]["comp" if course.kind == "compressions" else "vent"]
        require(assessment["goal"]["kind"] == course.kind and assessment["goal"]["required"] == course.required,
                "The approved goal must stay pinned to its Dummy course.")
        require(assessment["goal"]["observed"] == count and assessment["goal"]["status"] == "evaluated",
                "Only goal assessment must use the actual measured action count.")
        require(assessment["program_completed"] is (count >= course.required and assessment["score"]["decision"] == "pass"),
                "Only completion must require both the goal and the existing tester Pass.")
    require(server.item(token, course)["isCompleted"] is assessment["program_completed"],
            "Course progress must follow exactly the committed completion decision.")
    require(server.calculation(token, attempt).stable_body() == response.stable_body(),
            "Reading course progress must not rewrite the stored calculation bytes.")

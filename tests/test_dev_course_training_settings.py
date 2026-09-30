"""D132: Dummy Dev item details show the values the server really applies."""

import json

import pytest

from mock_journey.catalog import PROGRAMS, TARGETS
from mock_journey.course_schema import validate_training_detail
from mock_journey.course_settings import fixture_course_settings
from mock_journey.dev_course import DummyDevCourseProvider
from mock_journey.execution_definitions import execution_catalog

EXPECTED_LIMITS = {
    "mock-cpr": {"cycleLimit": 3},
    "mock-compression-only": {"compressionLimit": 60},
    "mock-ventilation-only": {"ventilationLimit": 8},
    "mock-two-rescuer-cpr": {"cycleLimit": 8},
    "mock-two-rescuer-aed": {"cycleLimit": 10},
}
EXPECTED_GUIDELINE = {
    "adult": (50, 60, 400, 600, 8, 12, 1.97, 2.36),
    "child": (50, 60, 400, 600, 20, 30, 1.97, 2.36),
    "infant": (33, 40, 20, 40, 20, 30, 1.3, 1.57),
}


def _details():
    provider = DummyDevCourseProvider(settings=fixture_course_settings(), execution=execution_catalog())
    for bundle in provider._bundles.values():
        first = bundle.placements[0]
        program = json.loads(first.content_identity_json)["training_program_id"]
        target = json.loads(first.execution_json)["condition"]["target"]
        yield program, target, [json.loads(placement.detail_json) for placement in bundle.placements]


@pytest.mark.parametrize("program,target,details", list(_details()),
                         ids=[f"{p[0]}:{t}" for p in PROGRAMS for t in TARGETS])
def test_training_settings_are_the_applied_limits_ratio_guideline_and_threshold(program, target, details):
    practice, final = details
    for item in (practice, final):
        validate_training_detail(item["detail"])
        training = item["detail"]["training"]
        limits = {key: training[key] for key in ("duration", "compressionLimit", "ventilationLimit", "cycleLimit")}
        assert limits == {"duration": None, "compressionLimit": None, "ventilationLimit": None, "cycleLimit": None,
                          **EXPECTED_LIMITS[program]}
        assert training["manikinType"] == target and training["aed"] is None
        if program == "mock-compression-only" or program == "mock-ventilation-only":
            assert training["compressionVentilationRatio"] is None
        else:
            compressions = 15 if target == "infant" else 30
            assert training["compressionVentilationRatio"] == {
                "title": f"{compressions}:2", "cvrVentilation": 2, "cvrCompression": compressions}
        depth_min, depth_max, vol_min, vol_max, rate_min, rate_max, inch_min, inch_max = EXPECTED_GUIDELINE[target]
        assert training["cprGuideline"] == {
            "title": "ARC 2025", "manikinType": target, "name": "ARC2025",
            "compressionDepthMax": depth_max, "compressionDepthMin": depth_min,
            "compressionRateMax": 120, "compressionRateMin": 100,
            "ventilationVolumeMax": vol_max, "ventilationVolumeMin": vol_min,
            "ventilationRateMax": rate_max, "ventilationRateMin": rate_min,
            "compressionDepthMaxInch": inch_max, "compressionDepthMinInch": inch_min,
        }
        two_rescuers = {"cycleChangeCount": 2} if program.startswith("mock-two-rescuer") else None
        assert training["twoRescuers"] == two_rescuers
        assert item["detail"]["assessment"] == {"passThreshold": {"cpr": 80, "aed": None}}
    assert practice["detail"]["trainingMode"] == "practice"
    assert final["detail"]["trainingMode"] == "assessment"
    assert practice["itemType"] == "training" and final["itemType"] == "assessment"

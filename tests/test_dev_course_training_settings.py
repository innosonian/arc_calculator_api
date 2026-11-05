"""Round 2 Dummy Dev course layout, training settings and frozen definitions."""

from copy import deepcopy
import json

import pytest

from mock_journey.course_schema import validate_training_detail
from mock_journey.course_settings import fixture_course_settings
from mock_journey.dev_course import DummyDevCourseProvider
from mock_journey.execution_definitions import execution_catalog
from tests.journey_support import JourneyStore, V2Journey


BLS = ("bls-compressions-60", "bls-ventilations-12", "bls-cpr-3", "bls-2r-cpr-8", "bls-2r-cpr-aed-8")
CPR_AED = ("lay-compressions-30", "lay-breaths-5x2", "lay-cpr-3", "lay-aed-cpr-1")
LIMITS = {
    "bls-compressions-60": ("compressionLimit", 60, (60, 0)),
    "bls-ventilations-12": ("ventilationLimit", 12, (0, 12)),
    "bls-cpr-3": ("cycleLimit", 3, (90, 6)),
    "bls-2r-cpr-8": ("cycleLimit", 8, (120, 8)),
    "bls-2r-cpr-aed-8": ("cycleLimit", 8, (120, 8)),
    "lay-compressions-30": ("compressionLimit", 30, (30, 0)),
    "lay-breaths-5x2": ("ventilationLimit", 10, (0, 10)),
    "lay-cpr-3": ("cycleLimit", 3, (90, 6)),
    "lay-aed-cpr-1": ("cycleLimit", 1, (30, 2)),
}
GUIDELINE = {"adult": (50, 60, 400, 600, 8, 12, 1.97, 2.36),
             "infant": (33, 40, 20, 40, 20, 30, 1.3, 1.57)}


@pytest.fixture
def source():
    return DummyDevCourseProvider(settings=fixture_course_settings(), execution=execution_catalog())


@pytest.mark.parametrize("course_index,course_type,target,skills", [
    (0, "BLS", "adult", BLS), (1, "BLS", "infant", BLS),
    (2, "CPR_AED", "adult", CPR_AED), (3, "CPR_AED", "infant", CPR_AED),
])
def test_skills_and_final_assessment_have_round_two_settings(source, course_index, course_type, target, skills):
    binding = source.list_assignments(source.learner)[course_index]
    bundle = source.fetch_bundle(binding)
    assert binding.public_ids.course_id == 911001 + course_index
    assert json.loads(bundle.course_json)["courseType"] == course_type
    assert [json.loads(item.content_identity_json)["training_program_id"] for item in bundle.placements] == [
        *skills, skills[-1]]
    assert [item.kind for item in bundle.placements] == ["training"] * len(skills) + ["assessment"]
    for placement, program in zip(bundle.placements, (*skills, skills[-1])):
        detail = validate_training_detail(json.loads(placement.detail_json)["detail"])
        training = detail["training"]
        definition = json.loads(placement.execution_json)
        condition = definition["condition"]
        limit, amount, minimum = LIMITS[program]
        two_rescuers = program in BLS[-2:]
        compressions = 15 if two_rescuers and target == "infant" else 30
        assert condition["cpr_cycle_type"] == f"{compressions}2"
        assert condition["is_2rescuers"] is two_rescuers
        assert training["duration"] is None and training["manikinType"] == target
        assert {k: training[k] for k in ("compressionLimit", "ventilationLimit", "cycleLimit")} == {
            "compressionLimit": None, "ventilationLimit": None, "cycleLimit": None, limit: amount,
        }
        expected_quantity = {"compressions": minimum[0] // (2 if compressions == 15 else 1),
                             "ventilations": minimum[1]}
        assert training["minimumQuantity"] == definition["minimumQuantity"] == expected_quantity
        assert training["compressionVentilationRatio"] == (
            {"title": f"{compressions}:2", "cvrVentilation": 2, "cvrCompression": compressions}
            if limit == "cycleLimit" else None)
        assert training["twoRescuers"] == ({"cycleChangeCount": 2} if two_rescuers else None)
        assert training["prompts"][0]["trigger"] == "drill_start"
        if program == "lay-aed-cpr-1":
            assert training["prompts"][0]["text"] == "The AED has arrived. Turn on the AED and attach the pads"
        else:
            assert training["prompts"][0]["text"].startswith("Perform ")
        prompts = training["prompts"]
        assert len({prompt["id"] for prompt in prompts}) == len(prompts)
        for prompt in prompts:
            reviewed = not (prompt["trigger"].startswith("set_start(")
                            or (prompt["trigger"] == "cpr_resume" and two_rescuers))
            assert prompt["arcReviewed"] is reviewed
        assert "drill_complete" not in {prompt["trigger"] for prompt in prompts}
        aed = training["aed"]
        if program in (BLS[-1], CPR_AED[-1]):
            assert (aed["arrivalMode"], aed["arrivalAfterCycle"], aed["cyclesAfterShock"]) == (
                ("after_cycle", 6, 2) if two_rescuers else ("at_start", None, 1))
            assert aed["arrivalSeconds"] is None and aed["padsDetection"] is False
            assert aed["padsAttachedDetection"] is True
            assert "aed_arrived" in {p["trigger"] for p in training["prompts"]}
            assert next(p for p in prompts if p["trigger"] == "analysis_stand_clear")["text"] == "Analyzing - stand clear"
            assert next(p for p in prompts if p["trigger"] == "cpr_resume")["text"] == (
                "Perform 2 cycles of CPR" if two_rescuers else "Perform 1 cycle of CPR")
        else:
            assert aed is None
        if program == "lay-breaths-5x2":
            assert [p["trigger"] for p in training["prompts"][1:]] == [
                f"set_start({i})" for i in range(1, 6)]
        depth_min, depth_max, vol_min, vol_max, rate_min, rate_max, inch_min, inch_max = GUIDELINE[target]
        assert training["cprGuideline"] == {
            "title": "ARC 2025", "manikinType": target, "name": "ARC2025",
            "compressionDepthMax": depth_max, "compressionDepthMin": depth_min,
            "compressionRateMax": 120, "compressionRateMin": 100,
            "ventilationVolumeMax": vol_max, "ventilationVolumeMin": vol_min,
            "ventilationRateMax": rate_max, "ventilationRateMin": rate_min,
            "compressionDepthMaxInch": inch_max, "compressionDepthMinInch": inch_min,
        }
        assert detail["assessment"] == {
            "passThreshold": {"cpr": 80, "aed": None}, "passRule": "composite", "gates": {},
        }
        assert detail["trainingMode"] == ("assessment" if placement.kind == "assessment" else "practice")


@pytest.mark.parametrize("reviewed", [True, False])
def test_prompt_review_annotation_is_boolean_and_legacy_omission_is_preserved(source, reviewed):
    binding = source.list_assignments(source.learner)[0]
    detail = json.loads(source.fetch_bundle(binding).placements[0].detail_json)["detail"]
    detail["training"]["prompts"][0]["arcReviewed"] = reviewed
    assert validate_training_detail(detail)["training"]["prompts"][0]["arcReviewed"] is reviewed
    legacy = deepcopy(detail)
    del legacy["training"]["prompts"][0]["arcReviewed"]
    assert validate_training_detail(legacy) == legacy


@pytest.mark.parametrize("reviewed", [None, 1, "true"])
def test_prompt_review_annotation_rejects_non_booleans(source, reviewed):
    from mock_journey.course_errors import CourseError

    binding = source.list_assignments(source.learner)[0]
    detail = json.loads(source.fetch_bundle(binding).placements[0].detail_json)["detail"]
    detail["training"]["prompts"][0]["arcReviewed"] = reviewed
    with pytest.raises(CourseError, match="UPSTREAM_CONTRACT_MISMATCH"):
        validate_training_detail(detail)


def test_course_type_and_frozen_minimum_quantity_reach_normal_http_entrypoints(source):
    from types import SimpleNamespace

    h = V2Journey(JourneyStore.memory(), provider=source, execution=execution_catalog())
    token = h.login().token
    assert [row["courseType"] for row in h.courses(token)["results"]] == ["BLS", "BLS", "CPR_AED", "CPR_AED"]
    course = SimpleNamespace(course_id=911003, enrollment_id=921003)
    detail = h.course(token, course)
    assert detail["courseType"] == "CPR_AED"
    started = h.start(token, course, detail["courseItems"][0]["courseItemLinkId"])
    attempt = h.store.row(f"ATTEMPT#{started['attemptId']}", "META")
    definition = json.loads(attempt["definition_json"])
    assert definition["courseType"] == "CPR_AED"
    assert definition["minimumQuantity"] == {"compressions": 30, "ventilations": 0}
    assert definition["assessment"] == {"passRule": "composite", "gates": {}}
    h.upload(token, started["attemptId"], started["condition"])
    assert h.work(started["attemptId"])


def test_existing_v6_attempt_finishes_after_refresh_switches_to_new_catalog(source):
    from tests.journey_support import dummy_course

    h = V2Journey(JourneyStore.memory())
    token = h.login().token
    previous = dummy_course("mock-compression-only", "adult")
    started = h.start(token, previous, previous.practice_link_id)
    attempt_id = started["attemptId"]
    key = f"ATTEMPT#{attempt_id}"
    old_definition = h.store.row(key, "META")["definition_json"]
    assert json.loads(old_definition)["adapter_version"] == "arc-internal-detection-v6"

    h.provider, h.mapping_document, h.execution = source, source.mapping_document, execution_catalog()
    h.restart()
    h.refresh(token)
    assert h.courses(token)["count"] == 4
    h.course(token, previous, expected=404)
    h.upload(token, attempt_id, started["condition"])
    assert h.work(attempt_id)
    result = h.result(token, attempt_id)
    assert result["evaluation"]["program_completed"] is True
    assert "gates" in result["evaluation"]
    assert h.store.row(key, "META")["definition_json"] == old_definition

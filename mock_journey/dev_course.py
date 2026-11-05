"""Opt-in, Dummy-only Dev courses from the approved internal training catalog.

These temporary courses are not ARC assignments, content, or certificates. All
scores still come from uploaded binary measurements. No external I/O or test
fixture is needed by the deployed package, and no operating quotas live here.
"""

from dataclasses import replace
import json
import uuid

from config.borders import AdultBorder, ChildBorder, InfantBorder
from mock_journey.auth import PRINCIPAL
from mock_journey.catalog import Catalog, PROGRAMS
from mock_journey.course_contracts import (
    AssignmentBinding, CourseBundle, CourseScope, LearnerContext, Placement, PublicIds,
)
from mock_journey.course_errors import CourseError
from mock_journey.course_provider import validate_assignments, validate_bundle
from mock_journey.models import AuthContext


MODE = "course_v2_dummy"
# The deployment selector and learner tenant keep their existing identity.
CATALOG_VERSION = "arc-dummy-dev-v1"
_REVISION = "arc-dummy-dev-round2"
COURSE_IDS = {("BLS", "adult"): 911001, ("BLS", "infant"): 911002,
              ("CPR_AED", "adult"): 911003, ("CPR_AED", "infant"): 911004}
_COURSE_LABELS = {"BLS": "BLS", "CPR_AED": "CPR-AED"}
_DESCRIPTION = "Temporary Dummy Dev test only. Not an ARC course or certification."
# The largest course write-set is a final assessment start (8 transaction actions).
MIN_TRANSACTION_ACTIONS = 8
_CAPACITY_ERROR = "The explicit course limits cannot hold the Dummy Dev catalog."
# D132: the synthetic item detail shows the values the server really applies. They
# are display settings only; the app never rebuilds the upload ``condition`` from them.
_GUIDELINE = "ARC2025"
_BORDERS = {"adult": AdultBorder, "child": ChildBorder, "infant": InfantBorder}
_PASS_THRESHOLD = 80  # services.legacy_document._get_pass_threshold default
_TWO_RESCUER_CYCLE_CHANGE = 2


def _inch(millimetres):
    return round(millimetres / 25.4, 2)


def _guideline(target):
    border = _BORDERS[target].BORDER[_GUIDELINE]
    depth, rate, volume, vent_rate = (border[key] for key in ("comp_depth", "comp_rate", "vent_vol", "vent_only_rate"))
    return {
        "title": "ARC 2025", "manikinType": target, "name": _GUIDELINE,
        "compressionDepthMax": depth[2], "compressionDepthMin": depth[1],
        "compressionRateMax": rate[2], "compressionRateMin": rate[1],
        "ventilationVolumeMax": volume[2], "ventilationVolumeMin": volume[1],
        "ventilationRateMax": vent_rate[2], "ventilationRateMin": vent_rate[1],
        "compressionDepthMaxInch": _inch(depth[2]), "compressionDepthMinInch": _inch(depth[1]),
    }


def _aed_settings(program, public_id):
    if program.aed_arrival is None:
        return None
    after_cycle = program.aed_arrival == "after_cycle"
    # Reuse the app's Demo IT23 settings for this synthetic catalog only.
    return {
        "id": public_id, "cprGuide": _GUIDELINE, "shockMode": "semi_automatic",
        "language": "en", "scenarioNo": 1, "volume": 5, "arrivalSeconds": None,
        "padsDetection": False, "arrivalMode": program.aed_arrival,
        "arrivalAfterCycle": 6 if after_cycle else None,
        "cyclesAfterShock": 2 if after_cycle else 1, "padsAttachedDetection": True,
    }


def _prompts(program, target):
    def prompt(id_, text, *, trigger=None, voice=True, screen=True, arc_reviewed=True):
        return {"id": id_, "trigger": trigger or id_, "text": text, "voice": voice, "screen": screen,
                "arcReviewed": arc_reviewed}

    start = {
        "bls-compressions-60": "Perform 60 chest compressions",
        "bls-ventilations-12": "Perform 12 ventilations, one every " + (
            "2 to 3 seconds" if target == "infant" else "6 seconds"),
        "bls-cpr-3": "Perform 3 cycles of CPR",
        "bls-2r-cpr-8": "Perform 8 cycles of CPR, switching roles every 2 cycles",
        "bls-2r-cpr-aed-8": "Perform 8 cycles of CPR, switching roles every 2 cycles",
        "lay-compressions-30": "Perform 30 chest compressions",
        "lay-breaths-5x2": "Perform 5 sets of 2 breaths",
        "lay-cpr-3": "Perform 3 cycles of CPR",
        "lay-aed-cpr-1": "The AED has arrived. Turn on the AED and attach the pads",
    }[program.id]
    prompts = [prompt("drill_start", start)]
    if program.id == "lay-breaths-5x2":
        for number in range(1, 6):
            trigger = f"set_start({number})"
            prompts.append(prompt(trigger, "Perform 2 breaths", arc_reviewed=False))
    if program.two_rescuers:
        prompts.extend((
            prompt("role_switch(toCompressions)", "Switch roles. Perform chest compressions"),
            prompt("role_switch(toVentilations)", "Switch roles. Perform ventilations"),
        ))
    if program.aed_arrival is not None:
        prompts.append(prompt("aed_arrived", "The AED has arrived. Turn on the AED and attach the pads"))
        if target == "infant":
            prompts.append(prompt("aed_infant_pads", "Place one pad on the chest and one on the back"))
        prompts.append(prompt("analysis_stand_clear", "Analyzing - stand clear", voice=False))
        prompts.append(prompt("cpr_resume", "Perform 2 cycles of CPR" if program.two_rescuers
                              else "Perform 1 cycle of CPR", arc_reviewed=not program.two_rescuers))
    return prompts


def training_settings(program, target, definition, public_id):
    """The training settings attached to one Dummy Dev skill."""
    kind, required = definition["goal"]["kind"], definition["goal"]["required"]
    condition = definition["condition"]
    ratio = None
    if kind == "cycles":
        compressions = 15 if condition["cpr_cycle_type"] == "152" else 30
        ratio = {"title": f"{compressions}:2", "cvrVentilation": 2, "cvrCompression": compressions}
    return {
        "manikinType": target, "duration": None,
        "compressionLimit": required if kind == "compressions" else None,
        "ventilationLimit": required if kind == "ventilations" else None,
        "cycleLimit": required if kind == "cycles" else None,
        "compressionVentilationRatio": ratio, "aed": _aed_settings(program, public_id),
        "cprGuideline": _guideline(target),
        "twoRescuers": {"cycleChangeCount": _TWO_RESCUER_CYCLE_CHANGE} if program.two_rescuers else None,
        "minimumQuantity": definition["minimumQuantity"],
        "prompts": _prompts(program, target),
    }


class DummyDevCourseProvider:
    def __init__(self, *, settings, execution):
        self.learner = LearnerContext(MODE, CATALOG_VERSION, "dummy", PRINCIPAL, True)
        self._bundles = {}
        self.mapping_document = {"mapping_version": _REVISION, "mappings": {}}
        catalog = Catalog(execution)
        assignments = []
        for (course_type, target), course_id in COURSE_IDS.items():
            # Stable public IDs identify synthetic Dev entities only.
            number = course_id - 910000
            enrollment_id, progress_id = 920000 + number, 930000 + number
            scope = CourseScope(self.learner, f"{_REVISION}-enrollment-{number}", f"{_REVISION}-course-{number}")
            public = PublicIds(course_id, enrollment_id, progress_id)
            binding = AssignmentBinding(scope, public)
            title = f"[Dummy Dev] {_COURSE_LABELS[course_type]} ({target})"
            skills = [program for program in PROGRAMS if program.course_type == course_type]
            placements = []
            for position, program in enumerate((*skills, skills[-1]), start=1):
                assessment = position > len(skills)
                kind = "assessment" if assessment else "training"
                source = f"{_REVISION}-placement-{number}-{position}"
                link_id = 940000 + number * 10 + position
                item_id = 950000 + number * 10 + position
                program_public_id = 960000 + number * 10 + min(position, len(skills))
                item_title = f"{program.name} - Final assessment" if assessment else program.name
                definition = json.loads(catalog.definition(program.id, target))
                detail = {
                    "id": item_id, "title": item_title, "itemType": kind,
                    "displayOrder": position, "courseItemLinkId": link_id, "usage": "OWNED",
                    "logicalId": str(uuid.uuid5(uuid.NAMESPACE_URL, source)),
                    "description": _DESCRIPTION,
                    "detail": {
                        "id": program_public_id, "title": program.name,
                        "trainingType": {
                            "compression_only": "chest compression only", "ventilation_only": "ventilation only",
                            "cpr": "cpr",
                        }[definition["condition"]["training_type"]],
                        "feedbackType": "standard",
                        "trainingMode": "assessment" if assessment else "practice",
                        "training": training_settings(program, target, definition, program_public_id),
                        "assessment": {"passThreshold": {"cpr": _PASS_THRESHOLD, "aed": None},
                                       **definition["assessment"]},
                        "content": [],
                    },
                }
                placements.append(Placement(
                    source, link_id, item_id, position, kind, _REVISION,
                    {"source_item_id": source, "training_program_id": program.id, "asset_ids": []},
                    detail, definition, "ready", None,
                ))
                self.mapping_document["mappings"][source] = {
                    "program_id": program.id, "target": target, "execution": definition,
                }
            metadata = {
                "courseId": course_id, "courseName": title, "certificationType": None, "courseType": course_type,
                "enrollment_metadata": {str(enrollment_id): {
                    "id": enrollment_id, "status": "ENROLLED", "courseTitle": title,
                    "courseId": course_id, "loginAt": None, "finishedAt": None,
                    "elapsedSeconds": None, "centerName": None, "enrollStatusCode": None,
                }},
            }
            bundle = validate_bundle(CourseBundle(
                scope, public, _REVISION, _REVISION, "0" * 64, tuple(placements), metadata,
                {"synthetic": True, "progress": None},
            ), settings)
            assignments.append(binding)
            self._bundles[public] = bundle
        self._assignments = validate_assignments(assignments, settings)

    def resolve_learner(self, auth):
        if type(auth) is not AuthContext or auth.principal != PRINCIPAL:
            raise CourseError("NOT_FOUND")
        return replace(self.learner)

    def list_assignments(self, learner):
        if learner != self.learner:
            raise CourseError("NOT_FOUND")
        return tuple(replace(binding) for binding in self._assignments)

    def fetch_bundle(self, binding):
        if type(binding) is not AssignmentBinding or binding not in self._assignments:
            raise CourseError("NOT_FOUND")
        return replace(self._bundles[binding.public_ids])


def validate_dummy_catalog(settings, *, execution, artifact_bytes):
    """Prove offline that explicit limits can hold the complete Dummy Dev catalog.

    Same meaning as the AWS course configuration check: the transaction action
    limit must admit the largest course write-set, and every serialized bundle
    snapshot (including its scope/IDs/hash) must fit the private storage
    artifact quota. No SDK, file or network I/O. Returns the validated provider;
    any failure is a fixed ValueError without nested details.
    """
    from mock_journey.course_settings import CourseSettings
    from mock_journey.course_records import bundle_record
    from mock_journey.typed import json_bytes

    try:
        if (type(settings) is not CourseSettings or type(artifact_bytes) is not int or artifact_bytes <= 0
                or settings.max_transaction_actions < MIN_TRANSACTION_ACTIONS):
            raise ValueError(_CAPACITY_ERROR)
        provider = DummyDevCourseProvider(settings=settings, execution=execution)
        if any(len(json_bytes(bundle_record(provider.fetch_bundle(binding)))) > artifact_bytes
               for binding in provider.list_assignments(provider.learner)):
            raise ValueError(_CAPACITY_ERROR)
        return provider
    except Exception:
        raise ValueError(_CAPACITY_ERROR) from None

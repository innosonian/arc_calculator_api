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
from mock_journey.catalog import Catalog, PROGRAMS, TARGETS
from mock_journey.course_contracts import (
    AssignmentBinding, CourseBundle, CourseScope, LearnerContext, Placement, PublicIds,
)
from mock_journey.course_errors import CourseError
from mock_journey.course_provider import validate_assignments, validate_bundle
from mock_journey.models import AuthContext


MODE = "course_v2_dummy"
CATALOG_VERSION = "arc-dummy-dev-v1"
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
_TWO_RESCUER_PROGRAMS = ("mock-two-rescuer-cpr", "mock-two-rescuer-aed")
# App automatic-stop limits in seconds, shared by all ages and both item modes.
_DURATION_SECONDS = {
    "mock-cpr": 300,
    "mock-compression-only": 120,
    "mock-ventilation-only": 120,
    "mock-two-rescuer-cpr": 720,
    "mock-two-rescuer-aed": 900,
}


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


def training_settings(program, target, definition):
    """The ``training`` block of a Dummy Dev item detail (D132)."""
    kind, required = definition["goal"]["kind"], definition["goal"]["required"]
    cycle_type = definition["condition"]["cpr_cycle_type"]
    ratio = None
    if kind == "cycles":
        compressions = 15 if cycle_type == "152" else 30
        ratio = {"title": f"{compressions}:2", "cvrVentilation": 2, "cvrCompression": compressions}
    return {
        "manikinType": target, "duration": _DURATION_SECONDS[program],
        "compressionLimit": required if kind == "compressions" else None,
        "ventilationLimit": required if kind == "ventilations" else None,
        "cycleLimit": required if kind == "cycles" else None,
        "compressionVentilationRatio": ratio, "aed": None,
        "cprGuideline": _guideline(target),
        "twoRescuers": {"cycleChangeCount": _TWO_RESCUER_CYCLE_CHANGE} if program in _TWO_RESCUER_PROGRAMS else None,
    }


class DummyDevCourseProvider:
    def __init__(self, *, settings, execution):
        self.learner = LearnerContext(MODE, CATALOG_VERSION, "dummy", PRINCIPAL, True)
        self._bundles = {}
        self.mapping_document = {"mapping_version": CATALOG_VERSION, "mappings": {}}
        catalog = Catalog(execution)
        assignments = []
        for program_index, (program, name, _, _) in enumerate(PROGRAMS):
            for target_index, target in enumerate(TARGETS):
                # Stable public IDs identify synthetic Dev entities only.
                number = program_index * len(TARGETS) + target_index + 1
                course_id, enrollment_id, progress_id = 910000 + number, 920000 + number, 930000 + number
                scope = CourseScope(self.learner, f"{CATALOG_VERSION}-enrollment-{number}",
                                    f"{CATALOG_VERSION}-course-{number}")
                public = PublicIds(course_id, enrollment_id, progress_id)
                binding = AssignmentBinding(scope, public)
                title = f"[Dummy Dev] {name} ({target})"
                definition = json.loads(catalog.definition(program, target))
                placements = []
                for position, kind in enumerate(("training", "assessment"), start=1):
                    source = f"{CATALOG_VERSION}-placement-{number}-{position}"
                    link_id = 940000 + number * 10 + position
                    item_id = 950000 + number * 10 + position
                    item_title = f"{title} - {'Practice' if kind == 'training' else 'Final assessment'}"
                    detail = {
                        "id": item_id, "title": item_title, "itemType": kind,
                        "displayOrder": position, "courseItemLinkId": link_id, "usage": "OWNED",
                        "logicalId": str(uuid.uuid5(uuid.NAMESPACE_URL, source)),
                        "description": _DESCRIPTION,
                        "detail": {
                            "id": 960000 + number, "title": title,
                            "trainingType": {
                                "compression_only": "chest compression only", "ventilation_only": "ventilation only",
                                "cpr": "cpr",
                            }[definition["condition"]["training_type"]],
                            "feedbackType": "standard",
                            "trainingMode": "practice" if kind == "training" else "assessment",
                            "training": training_settings(program, target, definition),
                            "assessment": {"passThreshold": {"cpr": _PASS_THRESHOLD, "aed": None}}, "content": [],
                        },
                    }
                    placements.append(Placement(
                        source, link_id, item_id, position, kind, CATALOG_VERSION,
                        {"source_item_id": source, "training_program_id": program, "asset_ids": []},
                        detail, definition, "ready", None,
                    ))
                    self.mapping_document["mappings"][source] = {
                        "program_id": program, "target": target, "execution": definition,
                    }
                metadata = {
                    "courseId": course_id, "courseName": title, "certificationType": None,
                    "enrollment_metadata": {str(enrollment_id): {
                        "id": enrollment_id, "status": "ENROLLED", "courseTitle": title,
                        "courseId": course_id, "loginAt": None, "finishedAt": None,
                        "elapsedSeconds": None, "centerName": None, "enrollStatusCode": None,
                    }},
                }
                bundle = validate_bundle(CourseBundle(
                    scope, public, CATALOG_VERSION, CATALOG_VERSION, "0" * 64, tuple(placements), metadata,
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

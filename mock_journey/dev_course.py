"""Opt-in, Dummy-only Dev courses from the approved internal training catalog.

These temporary courses are not ARC assignments, content, or certificates. All
scores still come from uploaded binary measurements. No external I/O or test
fixture is needed by the deployed package, and no operating quotas live here.
"""

from dataclasses import replace
import json
import uuid

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
                            "feedbackType": "standard", "trainingMode": "practice",
                            "training": {
                                "manikinType": target, "duration": None, "compressionLimit": None,
                                "ventilationLimit": None, "cycleLimit": None,
                                "compressionVentilationRatio": None, "aed": None,
                                "cprGuideline": None, "twoRescuers": None,
                            },
                            "assessment": {"passThreshold": {"cpr": None, "aed": None}}, "content": [],
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

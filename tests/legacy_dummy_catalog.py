"""Historical Dummy public IDs with current execution definitions for journey tests.

Runtime only assembles the four Round 2 courses. This fixture keeps the old
15-course public IDs and DTOs; new test calculations use the current adapter.
An explicit version still reconstructs a historical definition for read/verify tests.
"""

from copy import deepcopy
from dataclasses import replace
import json
import uuid

from mock_journey.auth import PRINCIPAL
from mock_journey.catalog import TARGETS, definition_key
from mock_journey.contracts import CURRENT_ADAPTER_VERSION, expected_profile_version, uses_round2_gates
from mock_journey.course_contracts import AssignmentBinding, CourseBundle, CourseScope, LearnerContext, Placement, PublicIds
from mock_journey.course_errors import CourseError
from mock_journey.course_provider import validate_assignments, validate_bundle
from mock_journey.dev_course import CATALOG_VERSION, MODE, _DESCRIPTION, _guideline
from mock_journey.errors import JourneyError
from mock_journey.models import AuthContext
from mock_journey.projection import DEFINITION_CORE_KEYS, DEFINITION_SETTING_KEYS


LEGACY_PROGRAMS = (
    ("mock-cpr", "CPR Training", "cycles", 3),
    ("mock-compression-only", "Chest Compression Only", "compressions", 60),
    ("mock-ventilation-only", "Ventilation Only", "ventilations", 8),
    ("mock-two-rescuer-cpr", "2-Rescuer CPR", "cycles", 8),
    ("mock-two-rescuer-aed", "2-Rescuer CPR with AED-T", "cycles", 10),
)

def legacy_execution_catalog(version=CURRENT_ADAPTER_VERSION):
    """Keep historical IDs while selecting the explicit current or retained binding."""
    from mock_journey.assembly import ExecutionCatalog
    from mock_journey.catalog import program_targets
    from mock_journey.execution_definitions import execution_catalog

    current = execution_catalog()
    definitions = {}
    for program, target in program_targets():
        value = current.get_definition(program.id, target)
        value.update(adapter_version=version, profile_version=expected_profile_version(version))
        definitions[definition_key(program.id, target)] = value
    return ExecutionCatalog(definitions, current.schemas)


class LegacyCatalog:
    version = "mock-catalog-v1"

    def __init__(self, execution_definitions=None):
        # No runtime sample definition: unavailable contracts fail at creation.
        self.execution_definitions = execution_definitions

    def definition(self, program_id, target):
        if self.execution_definitions is None:
            raise JourneyError("CALCULATOR_CONTRACT_MISMATCH")
        new_id = {
            "mock-cpr": "bls-cpr-3", "mock-compression-only": "bls-compressions-60",
            "mock-ventilation-only": "bls-ventilations-12", "mock-two-rescuer-cpr": "bls-2r-cpr-8",
            "mock-two-rescuer-aed": "bls-2r-cpr-aed-8",
        }[program_id]
        source_target = "adult" if target == "child" else target
        current = self.execution_definitions.get_definition(new_id, source_target)
        keys = DEFINITION_CORE_KEYS + (DEFINITION_SETTING_KEYS if uses_round2_gates(current["adapter_version"]) else ())
        value = {key: deepcopy(current[key]) for key in keys}
        value["condition"].update(target=target, cpr_cycle_type="152" if target == "infant" else "302")
        required = set(keys)
        if type(value) is not dict or set(value) != required:
            raise JourneyError("CALCULATOR_CONTRACT_MISMATCH")
        if type(value["condition"]) is not dict or type(value["calculation_profile"]) is not dict:
            raise JourneyError("CALCULATOR_CONTRACT_MISMATCH")
        if value["condition"].get("target") != target or any(
            type(value[key]) is not str or not value[key] for key in DEFINITION_CORE_KEYS if key not in {"condition", "calculation_profile"}
        ):
            raise JourneyError("CALCULATOR_CONTRACT_MISMATCH")
        program = next(p for p in LEGACY_PROGRAMS if p[0] == program_id)
        if "minimumQuantity" in value:
            # These historical test IDs keep their pre-Round-2 minimums under the current adapter.
            value["minimumQuantity"] = {
                "compressions": {"compressions": 60, "ventilations": 0},
                "ventilations": {"compressions": 0, "ventilations": 8},
                "cycles": {"compressions": 45 if target == "infant" else 90, "ventilations": 6},
            }[program[2]]
        value.update(catalog_version=self.version, goal={"kind": program[2], "required": program[3]})
        try:
            return json.dumps(value, allow_nan=False)
        except (TypeError, ValueError):
            raise JourneyError("CALCULATOR_CONTRACT_MISMATCH") from None


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


class LegacyDummyCourseProvider:
    def __init__(self, *, settings, execution):
        self.learner = LearnerContext(MODE, CATALOG_VERSION, "dummy", PRINCIPAL, True)
        self._bundles = {}
        self.mapping_document = {"mapping_version": CATALOG_VERSION, "mappings": {}}
        catalog = LegacyCatalog(execution)
        assignments = []
        for program_index, (program, name, _, _) in enumerate(LEGACY_PROGRAMS):
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


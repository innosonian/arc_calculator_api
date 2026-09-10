"""Confirmed Mock policy, separate from explicitly supplied execution definitions."""

from copy import deepcopy
import json

from mock_journey.errors import JourneyError


TARGETS = ("adult", "child", "infant")
PROGRAMS = (
    ("mock-cpr", "CPR Training", "cycles", 3),
    ("mock-compression-only", "Chest Compression Only", "compressions", 60),
    ("mock-ventilation-only", "Ventilation Only", "ventilations", 8),
    ("mock-two-rescuer-cpr", "2-Rescuer CPR", "cycles", 8),
    ("mock-two-rescuer-aed", "2-Rescuer CPR with AED-T", "cycles", 10),
)


def slot_key(program_id, target):
    return f"{program_id}:{target}"


class Catalog:
    version = "mock-catalog-v1"

    def __init__(self, execution_definitions=None):
        # No runtime sample definition: unavailable contracts fail at creation.
        self.execution_definitions = execution_definitions

    @property
    def slot_keys(self):
        return tuple(slot_key(program[0], target) for program in PROGRAMS for target in TARGETS)

    def validate_selection(self, program_id, target, catalog_version):
        if catalog_version != self.version:
            raise JourneyError("PROFILE_MISMATCH")
        if program_id not in {p[0] for p in PROGRAMS} or target not in TARGETS:
            raise JourneyError("INVALID_REQUEST")

    def definition(self, program_id, target):
        if self.execution_definitions is None:
            raise JourneyError("CALCULATOR_CONTRACT_MISMATCH")
        value = deepcopy(self.execution_definitions.get_definition(program_id, target))
        required = {"condition", "calculation_profile", "profile_version", "adapter_version", "projection_version"}
        if type(value) is not dict or set(value) != required:
            raise JourneyError("CALCULATOR_CONTRACT_MISMATCH")
        if type(value["condition"]) is not dict or type(value["calculation_profile"]) is not dict:
            raise JourneyError("CALCULATOR_CONTRACT_MISMATCH")
        if value["condition"].get("target") != target or any(
            type(value[key]) is not str or not value[key] for key in required - {"condition", "calculation_profile"}
        ):
            raise JourneyError("CALCULATOR_CONTRACT_MISMATCH")
        program = next(p for p in PROGRAMS if p[0] == program_id)
        value.update(catalog_version=self.version, goal={"kind": program[2], "required": program[3]})
        try:
            return json.dumps(value, allow_nan=False)
        except (TypeError, ValueError):
            raise JourneyError("CALCULATOR_CONTRACT_MISMATCH") from None

    def programs_view(self, progress):
        return {
            "catalog_version": self.version, "progress_epoch": progress["epoch"],
            "progress_version": progress["revision"], "profile_name": "tester",
            "guideline": "ARC2025", "guideline_basis": "ARC2020",
            "programs": [
                {
                    "id": ident, "name": name, "is_mock": True,
                    "supported_targets": list(TARGETS), "goal": {"kind": kind, "required": required},
                    "progress_by_target": {
                        target: "completed" if progress["slots"][slot_key(ident, target)]["completed"] else "not_completed"
                        for target in TARGETS
                    },
                    "active_attempts_by_target": {
                        target: progress["slots"][slot_key(ident, target)]["open_attempts"] for target in TARGETS
                    },
                }
                for ident, name, kind, required in PROGRAMS
            ],
        }

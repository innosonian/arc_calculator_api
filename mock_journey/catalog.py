"""Confirmed training goals and the skill order of the Dummy Dev courses."""

from copy import deepcopy
from dataclasses import dataclass
import json

from mock_journey.errors import JourneyError
from mock_journey.projection import DEFINITION_CORE_KEYS, DEFINITION_SETTING_KEYS


TARGETS = ("adult", "child", "infant")
COURSE_TARGETS = {"BLS": ("adult", "infant"), "CPR_AED": ("adult", "infant"), "PALS": ("child",)}


@dataclass(frozen=True)
class Program:
    id: str
    name: str
    kind: str
    required: int
    course_type: str
    infant_cycle_type: str = "302"
    two_rescuers: bool = False
    aed_arrival: str | None = None

    def cycle_type(self, target):
        return self.infant_cycle_type if target == "infant" else "302"

    def minimum_quantity(self, target):
        if self.kind == "cycles":
            cycles = self.required // 2 if self.two_rescuers else self.required
            compressions = 15 if self.cycle_type(target) == "152" else 30
            return {"compressions": cycles * compressions, "ventilations": cycles * 2}
        return {
            "compressions": self.required if self.kind == "compressions" else 0,
            "ventilations": self.required if self.kind == "ventilations" else 0,
        }


PROGRAMS = (
    Program("bls-compressions-60", "Compression drill (60)", "compressions", 60, "BLS"),
    Program("bls-ventilations-12", "Ventilation drill (12)", "ventilations", 12, "BLS"),
    Program("bls-cpr-3", "CPR cycles (3)", "cycles", 3, "BLS"),
    Program("bls-2r-cpr-8", "2-rescuer CPR (8)", "cycles", 8, "BLS", "152", two_rescuers=True),
    Program("bls-2r-cpr-aed-8", "2-rescuer CPR + AED (8)", "cycles", 8, "BLS", "152",
            two_rescuers=True, aed_arrival="after_cycle"),
    Program("lay-compressions-30", "Compression drill (30)", "compressions", 30, "CPR_AED"),
    Program("lay-breaths-5x2", "Breath drill (5 sets of 2)", "ventilations", 10, "CPR_AED"),
    Program("lay-cpr-3", "CPR cycles (3)", "cycles", 3, "CPR_AED"),
    Program("lay-aed-cpr-1", "AED practice (AED then 1 CPR cycle)", "cycles", 1, "CPR_AED",
            aed_arrival="at_start"),
)
PROGRAM_BY_ID = {program.id: program for program in PROGRAMS}

# Existing synthetic mapping rows remain readable by the fixture provider.
LEGACY_PROGRAM_IDS = frozenset({
    "mock-cpr", "mock-compression-only", "mock-ventilation-only", "mock-two-rescuer-cpr", "mock-two-rescuer-aed",
})


def definition_key(program_id, target):
    """Execution definition key, "program:target" (the stored legacy slot key format)."""
    return f"{program_id}:{target}"


def program_targets():
    return ((program, target) for program in PROGRAMS for target in COURSE_TARGETS[program.course_type])


def definition_keys():
    return tuple(definition_key(program.id, target) for program, target in program_targets())


class Catalog:
    version = "mock-catalog-v2"

    def __init__(self, execution_definitions=None):
        # No runtime sample definition: unavailable contracts fail at creation.
        self.execution_definitions = execution_definitions

    def definition(self, program_id, target):
        if self.execution_definitions is None:
            raise JourneyError("CALCULATOR_CONTRACT_MISMATCH")
        value = deepcopy(self.execution_definitions.get_definition(program_id, target))
        required = set(DEFINITION_CORE_KEYS)
        if type(value) is not dict or not required <= set(value) <= required | set(DEFINITION_SETTING_KEYS):
            raise JourneyError("CALCULATOR_CONTRACT_MISMATCH")
        if type(value["condition"]) is not dict or type(value["calculation_profile"]) is not dict:
            raise JourneyError("CALCULATOR_CONTRACT_MISMATCH")
        if value["condition"].get("target") != target or any(
            type(value[key]) is not str or not value[key] for key in required - {"condition", "calculation_profile"}
        ):
            raise JourneyError("CALCULATOR_CONTRACT_MISMATCH")
        program = PROGRAM_BY_ID[program_id]
        value.update(catalog_version=self.version, goal={"kind": program.kind, "required": program.required})
        try:
            return json.dumps(value, allow_nan=False)
        except (TypeError, ValueError):
            raise JourneyError("CALCULATOR_CONTRACT_MISMATCH") from None

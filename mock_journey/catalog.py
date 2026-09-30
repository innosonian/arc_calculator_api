"""Confirmed Mock policy, separate from explicitly supplied execution definitions."""

from copy import deepcopy
import json

from mock_journey.errors import JourneyError
from mock_journey.projection import DEFINITION_CORE_KEYS


TARGETS = ("adult", "child", "infant")
PROGRAMS = (
    ("mock-cpr", "CPR Training", "cycles", 3),
    ("mock-compression-only", "Chest Compression Only", "compressions", 60),
    ("mock-ventilation-only", "Ventilation Only", "ventilations", 8),
    ("mock-two-rescuer-cpr", "2-Rescuer CPR", "cycles", 8),
    ("mock-two-rescuer-aed", "2-Rescuer CPR with AED-T", "cycles", 10),
)


def definition_key(program_id, target):
    """Execution definition key, "program:target" (the stored legacy slot key format)."""
    return f"{program_id}:{target}"


def definition_keys():
    """The 15 approved execution definition keys, in PROGRAMS x TARGETS order."""
    return tuple(definition_key(program[0], target) for program in PROGRAMS for target in TARGETS)


class Catalog:
    version = "mock-catalog-v1"

    def __init__(self, execution_definitions=None):
        # No runtime sample definition: unavailable contracts fail at creation.
        self.execution_definitions = execution_definitions

    def definition(self, program_id, target):
        if self.execution_definitions is None:
            raise JourneyError("CALCULATOR_CONTRACT_MISMATCH")
        value = deepcopy(self.execution_definitions.get_definition(program_id, target))
        required = set(DEFINITION_CORE_KEYS)
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

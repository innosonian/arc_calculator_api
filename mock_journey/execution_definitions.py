"""Approved training definitions shared by explicit runtime composition.

No clients, operating limits, credentials, or runtime activation live here.
New definitions select the current detector. Already accepted definitions are
immutable in storage; retained candidates keep their original version.
"""

PROJECTION_VERSION = "arc-local-projection-v1"


def execution_catalog():
    """The approved five programs/three targets, using current calculator enums."""
    from mock_journey.assembly import ExecutionCatalog
    from mock_journey.catalog import PROGRAMS, TARGETS, slot_key
    from mock_journey.contracts import PENDING_GOAL_ADAPTER_VERSION, PENDING_GOAL_PROFILE_VERSION
    from mock_journey.projection import ProjectionSchema

    definitions = {}
    for program, _, kind, _ in PROGRAMS:
        for target in TARGETS:
            definitions[slot_key(program, target)] = {
                "condition": {
                    "mode": "training", "target": target,
                    "training_type": {"compressions": "compression_only", "ventilations": "ventilation_only"}.get(kind, "cpr"),
                    "guideline": "ARC2025", "cpr_cycle_type": "152" if target == "infant" else "302",
                    "is_2rescuers": program in ("mock-two-rescuer-cpr", "mock-two-rescuer-aed"),
                },
                "calculation_profile": {}, "profile_version": PENDING_GOAL_PROFILE_VERSION,
                "adapter_version": PENDING_GOAL_ADAPTER_VERSION, "projection_version": PROJECTION_VERSION,
            }
    # Existing response/document projection remains strict. No new arbitrary
    # metric fields, completion formula, partner events or AED timing is added.
    return ExecutionCatalog(definitions, {PROJECTION_VERSION: ProjectionSchema(PROJECTION_VERSION, {})})

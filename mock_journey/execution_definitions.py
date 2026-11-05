"""Training definitions shared by local and AWS composition.

New definitions select the current adapter. Already accepted definitions are
immutable; in-flight attempts of retained adapters keep their original version.
"""

from mock_journey.contracts import PROJECTION_VERSION  # noqa: F401  (re-exported; defined with the adapter versions)


def execution_catalog():
    """The Round 2 Dummy Dev skills, using current calculator enums."""
    from mock_journey.assembly import ExecutionCatalog
    from mock_journey.catalog import definition_key, program_targets
    from mock_journey.contracts import CURRENT_ADAPTER_VERSION, CURRENT_PROFILE_VERSION
    from mock_journey.projection import ProjectionSchema

    definitions = {}
    for program, target in program_targets():
        definitions[definition_key(program.id, target)] = {
            "condition": {
                "mode": "training", "target": target,
                "training_type": {"compressions": "compression_only", "ventilations": "ventilation_only"}.get(
                    program.kind, "cpr"),
                "guideline": "ARC2025", "cpr_cycle_type": program.cycle_type(target),
                "is_2rescuers": program.two_rescuers,
            },
            "calculation_profile": {}, "profile_version": CURRENT_PROFILE_VERSION,
            "adapter_version": CURRENT_ADAPTER_VERSION, "projection_version": PROJECTION_VERSION,
            "courseType": program.course_type,
            "minimumQuantity": program.minimum_quantity(target),
            "requiresAed": program.aed_arrival is not None,
            "assessment": {"passRule": "composite", "gates": {}},
        }
    return ExecutionCatalog(definitions, {PROJECTION_VERSION: ProjectionSchema(PROJECTION_VERSION, {})})

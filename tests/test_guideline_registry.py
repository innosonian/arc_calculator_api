"""The non-pinned guideline registry agrees with every pinned guideline table.

config/borders.py, config/calculation_config.py, NullPolicy.create and
services/guide_prompts.py are hash-pinned (tests/fixtures/detection_revision/
provenance.json) and keep their own literals. This test is the only link
between them and config/guideline_registry.py; it never edits them.
"""

import pytest

from config.borders import AdultBorder, ChildBorder, InfantBorder
from config.calculation_config import (
    AdultCalculationConfig,
    CalculationConfigFactory,
    ChildCalculationConfig,
    InfantCalculationConfig,
)
from config.guideline_registry import (
    ARC_GUIDELINES,
    GUIDELINE_BASIS,
    SUPPORTED_GUIDELINES,
    TARGET_ORDER,
    TARGETS,
    TRAINING_TYPE_ORDER,
    TRAINING_TYPES,
)


EXPECTED_SUPPORTED = {"AHA2020", "ARC2020", "ARC2025", "ERC2020", "STD2015"}
# The reference CLI order (scripts/run_local.py choices before the registry existed).
EXPECTED_TARGET_ORDER = ("adult", "child", "infant")
EXPECTED_TRAINING_TYPE_ORDER = ("cpr", "compression_only", "ventilation_only")


def test_registry_values_and_immutable_types():
    assert type(SUPPORTED_GUIDELINES) is frozenset and SUPPORTED_GUIDELINES == EXPECTED_SUPPORTED
    assert type(ARC_GUIDELINES) is frozenset and ARC_GUIDELINES == {"ARC2020", "ARC2025"}
    assert ARC_GUIDELINES <= SUPPORTED_GUIDELINES
    assert dict(GUIDELINE_BASIS) == {"ARC2025": "ARC2020"}
    with pytest.raises(TypeError):
        GUIDELINE_BASIS["ARC2030"] = "ARC2020"  # type: ignore[index]


def test_target_and_training_type_registry_values_orders_and_types():
    assert type(TARGETS) is frozenset and TARGETS == {"adult", "child", "infant"}
    assert type(TRAINING_TYPES) is frozenset and TRAINING_TYPES == {"cpr", "compression_only", "ventilation_only"}
    assert type(TARGET_ORDER) is tuple and TARGET_ORDER == EXPECTED_TARGET_ORDER
    assert type(TRAINING_TYPE_ORDER) is tuple and TRAINING_TYPE_ORDER == EXPECTED_TRAINING_TYPE_ORDER
    assert len(set(TARGET_ORDER)) == len(TARGET_ORDER) and set(TARGET_ORDER) == TARGETS
    assert len(set(TRAINING_TYPE_ORDER)) == len(TRAINING_TYPE_ORDER) and set(TRAINING_TYPE_ORDER) == TRAINING_TYPES


def test_target_and_training_type_consumers_keep_values_and_container_types():
    from services.http.legacy_request import _SUPPORTED_TARGETS, _SUPPORTED_TRAINING_TYPES
    from services.observability import _ENUM_FIELDS as sanitizer_enums
    from services.operational_logs import _ENUM_FIELDS as operation_enums
    from scripts.verify_reference_parity import TARGETS as parity_targets, TRAINING_TYPES as parity_training

    # The input validator keeps sets; both log allowlists keep frozensets.
    assert type(_SUPPORTED_TARGETS) is set and _SUPPORTED_TARGETS == {"adult", "child", "infant"}
    assert type(_SUPPORTED_TRAINING_TYPES) is set
    assert _SUPPORTED_TRAINING_TYPES == {"cpr", "compression_only", "ventilation_only"}
    assert type(sanitizer_enums["condition_target"]) is frozenset
    assert sanitizer_enums["condition_target"] == {"adult", "child", "infant"}
    assert type(sanitizer_enums["condition_training_type"]) is frozenset
    assert sanitizer_enums["condition_training_type"] == {"cpr", "compression_only", "ventilation_only"}
    assert type(operation_enums["target"]) is frozenset and operation_enums["target"] == {"adult", "child", "infant"}
    # The parity script keeps literal tuples (it also runs inside the reference checkout).
    assert set(parity_targets) == TARGETS and set(parity_training) == TRAINING_TYPES


def test_run_local_choices_keep_the_reference_cli_order():
    from scripts.run_local import build_parser

    choices = {action.dest: action.choices for action in build_parser()._actions if action.choices}
    assert choices["target"] == ["adult", "child", "infant"]
    assert choices["training_type"] == ["cpr", "compression_only", "ventilation_only"]
    assert choices["guideline"] == ["ARC2020", "ARC2025"]  # the CLI subset, unchanged


@pytest.mark.parametrize("field,allowed,rejected", [
    ("condition_target", ("adult", "child", "infant"), ("Adult", "adults", "manikin", "")),
    ("condition_training_type", ("cpr", "compression_only", "ventilation_only"), ("CPR", "aed", "", "cpr ")),
])
def test_log_sanitizer_keeps_registry_targets_and_training_types_and_drops_others(field, allowed, rejected):
    from services.observability import sanitize_log_record

    for value in allowed:
        assert sanitize_log_record("info", "parse_complete", {field: value})[field] == value
    for value in rejected:
        assert sanitize_log_record("info", "parse_complete", {field: value})[field] is None


def test_operation_log_target_enum_keeps_registry_values_and_drops_others():
    from services.operational_logs import _operation_fields

    for value in ("adult", "child", "infant"):
        assert _operation_fields({"target": value}) == {"target": value}
    for value in ("Adult", "manikin", "", None, 1):
        assert _operation_fields({"target": value}) == {}


@pytest.mark.parametrize("border", [AdultBorder, ChildBorder, InfantBorder])
def test_pinned_border_tables_cover_the_registry_and_apply_the_basis(border):
    assert set(border.BORDER) == SUPPORTED_GUIDELINES
    for guideline, basis in GUIDELINE_BASIS.items():
        assert border.BORDER[guideline] == border.BORDER[basis]


@pytest.mark.parametrize("config", [AdultCalculationConfig, ChildCalculationConfig, InfantCalculationConfig])
def test_pinned_calculation_config_tables_cover_the_registry_and_apply_the_basis(config):
    assert set(config.GUIDELINES) == SUPPORTED_GUIDELINES
    for guideline, basis in GUIDELINE_BASIS.items():
        assert config.GUIDELINES[guideline] == config.GUIDELINES[basis]


@pytest.mark.parametrize("guideline", sorted(EXPECTED_SUPPORTED))
@pytest.mark.parametrize("target", ["adult", "child", "infant"])
def test_minimum_count_policy_applies_to_the_arc_family_only(guideline, target):
    from calculators.cycle_evaluator import NullPolicy

    condition = {"mode": "training", "target": target, "training_type": "cpr",
                 "guideline": guideline, "cpr_cycle_type": "302", "is_2rescuers": False}
    policy = NullPolicy.create(CalculationConfigFactory.create_calculation_config(condition), 0, 0)
    assert policy.active is (guideline in ARC_GUIDELINES)


def test_coaching_source_follows_the_basis():
    from services.guide_prompts import _resolve_prompt_source

    for guideline, basis in GUIDELINE_BASIS.items():
        assert _resolve_prompt_source(guideline) == _resolve_prompt_source(basis) == "ARC 2020"


def test_non_pinned_consumers_keep_values_and_container_types():
    from mock_journey.course_submission import ARC_GUIDELINES as submission_arc
    from services.http.legacy_request import _SUPPORTED_GUIDELINES
    from services.observability import _ENUM_FIELDS
    from scripts.verify_reference_parity import GUIDELINES as parity_guidelines

    # The input validator keeps a set, the log sanitizer and submission scope frozensets.
    assert type(_SUPPORTED_GUIDELINES) is set and _SUPPORTED_GUIDELINES == SUPPORTED_GUIDELINES
    assert type(_ENUM_FIELDS["condition_guideline"]) is frozenset
    assert _ENUM_FIELDS["condition_guideline"] == SUPPORTED_GUIDELINES
    assert type(submission_arc) is frozenset and submission_arc == ARC_GUIDELINES
    assert set(parity_guidelines) == SUPPORTED_GUIDELINES


@pytest.mark.parametrize("guideline", sorted(EXPECTED_SUPPORTED))
def test_log_sanitizer_keeps_every_supported_guideline_and_drops_others(guideline):
    from services.observability import sanitize_log_record

    assert sanitize_log_record("info", "parse_complete", {"condition_guideline": guideline})[
        "condition_guideline"] == guideline
    assert sanitize_log_record("info", "parse_complete", {"condition_guideline": guideline.lower()})[
        "condition_guideline"] is None
    assert sanitize_log_record("info", "parse_complete", {"condition_guideline": "HSTM2015"})[
        "condition_guideline"] is None

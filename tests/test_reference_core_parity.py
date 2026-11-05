"""HSTM 92f56dc legacy weights, zero/null fields, and rescue-cycle contracts."""


import pytest


from calculators.cycle_evaluator import Cycle, CycleWithScore


from calculators.merge_calculator import calculate_part_score, calculate_total_score


from config.constants import ACTION_TYPE_COMP, ACTION_TYPE_VENT


from config.enums import Actor


from config.score_weight import ScoreWeightFactory


from models.action import ActionWithScore


from services.config import Config

from services.calculators import calculate_cpr
from services.preparers import parse_data, make_pre_action_list, prepare_data
from tests._synth import cpr_session


def _config(training_type="cpr", target="adult", guideline="ARC2025"):
    return Config({
        "mode": "training", "target": target, "training_type": training_type,
        "guideline": guideline, "cpr_cycle_type": "152" if target == "infant" else "302",
        "is_2rescuers": False,
    })


def _action(action_type, rate=100, actor=Actor.REAL_PERSON):
    good = {"grade": 100, "criterion": "good"}
    data = {
        "action_type": action_type, "compression_count": 1, "ventilation_count": 2,
        "handsoff_ms": 0, "total_action_ms": 400, "compression_depth": [50] * 10,
    }
    scores = {key: dict(good) for key in (
        "comp_depth", "comp_rate", "recoil", "hand_position", "vent_vol", "vent_speed",
    )}
    scores["vent_rate"] = {"grade": rate, "criterion": "good"}
    return ActionWithScore(action_type, data, scores, actor)


def _cycle(config, actions, cycle_num=2):
    return CycleWithScore(
        Cycle(actions, cycle_num), 1,
        ScoreWeightFactory.create(
            config.condition["target"], config.condition["training_type"], config.condition["guideline"],
        ), config.calculation_config,
    )


def test_above_minimum_cpr_keeps_reference_weights_and_rescue_field_types():
    config = _config()
    cycle = _cycle(config, [_action(ACTION_TYPE_COMP), _action(ACTION_TYPE_VENT)])
    cycle.score_ccf = 62
    cycle.score_comp_depth = 80
    cycle.score_comp_rate = 70
    cycle.score_recoil = 60
    cycle.score_hand_position = 50
    cycle.score_comp_count = cycle.score_comp_no = 40
    cycle.score_vent_vol = 30
    cycle.score_vent_count = 20
    cycle.score_vent_rate = 11
    # 62*.25 + 80*.20 + 70*.15 + 60*.15 + 50*.05 + 40*.05 + 30*.10 + 20*.05 = 59.5.
    assert cycle.overall() == 60
    total = calculate_total_score([cycle], config)
    part = calculate_part_score([cycle], 1, config)
    assert total["overall"] == part["overall"] == 60
    assert total["score_rescue_vent"] == 0
    assert type(total["score_rescue_vent"]) is int
    assert part["score_rescue_vent"] is None


def test_reference_ventilation_only_part_keeps_zero_fields_and_missing_alias():
    config = _config("ventilation_only")
    cycle = _cycle(config, [_action(ACTION_TYPE_VENT), _action(ACTION_TYPE_VENT)])
    cycle.make_score(config.border)
    part = calculate_part_score([cycle], 1, config)
    assert set(part) == {
        "score_comp_depth", "score_comp_rate", "score_comp_no", "score_recoil", "score_hand_position",
        "score_vent_vol", "score_vent_rate", "score_vent_count", "score_vent_speed", "score_ccf",
        "score_rescue_vent", "overall",
    }
    assert all(type(value) is int and value == 0 for value in part.values())
    total = calculate_total_score([cycle], config)
    assert total["score_comp_depth"] is None
    assert total["score_comp_count"] is None
    assert total["score_vent_vol"] == 100
    assert total["overall"] is not None




@pytest.mark.parametrize("target", ["child", "infant"])
def test_erc_initial_rescue_retains_reference_twenty_percent_total_merge(target):
    config = _config(target=target, guideline="ERC2020")
    rescue = _cycle(config, [_action(ACTION_TYPE_VENT) for _ in range(5)], cycle_num=1)
    rescue.make_score(config.border)
    assert rescue.did_rescue_vent is True
    assert rescue.score_rescue_vent == 100
    assert rescue.overall() == 100

    regular = _cycle(config, [_action(ACTION_TYPE_COMP), _action(ACTION_TYPE_VENT)])
    # Reference ERC regular-cycle weights sum to .80; its cycle adjustment divides
    # by .80. Assigning 80 to every scored field therefore yields a cycle score of 80.
    for key in ("score_ccf", "score_comp_depth", "score_comp_rate", "score_recoil", "score_hand_position",
                "score_comp_count", "score_comp_no", "score_vent_vol", "score_vent_count", "score_vent_rate"):
        setattr(regular, key, 80)
    if target == "infant":
        regular.score_vent_speed = 80
    assert regular.overall() == 80
    total = calculate_total_score([rescue, regular], config)
    # Reference merge: regular 80 * .80 + rescue 100 * .20 = 84.
    assert total["overall"] == 84
    # Preserve the reference part formula too: rescue is added to its numerator,
    # while the denominator counts only the regular cycle: (100 + 80) / 1 = 180.
    part = calculate_part_score([rescue, regular], 1, config)
    assert part["overall"] == 180
    assert part["score_rescue_vent"] == 100


@pytest.mark.parametrize("target,comp_count", [("child", 30), ("infant", 15)])
@pytest.mark.parametrize("initial_rescue", [False, True])
def test_erc_binary_initial_rescue_and_missing_rescue_follow_reference_paths(target, comp_count, initial_rescue):
    config = _config(target=target, guideline="ERC2020")
    session = ([(0, 5)] if initial_rescue else []) + [(comp_count, 2)]
    parsed = parse_data(cpr_session(session), b"", config)
    actions, aed_parts = make_pre_action_list(config, parsed, [])
    prepared = prepare_data(actions, aed_parts, config)
    result = calculate_cpr(prepared, config)
    cycles = [cycle for part in result["part_with_scores"] for cycle in part["result"]["cycle_with_score_list"]]
    first = cycles[0]
    assert first.did_rescue_vent is initial_rescue
    assert first.score_rescue_vent == (100 if initial_rescue else 0)
    if initial_rescue:
        assert first.calc_case == "only_vent"
        assert first.cycle.get_vent_count() == 5
    else:
        assert first.calc_case == "did_not_rescue_vent"
        assert first.cycle.actions == []  # The reference inserts an empty penalty cycle.
    assert cycles[1].cycle.cycle_num == 2
    assert cycles[1].score_comp_depth is not None
    assert cycles[1].score_vent_vol is not None
    assert result["cpr_total_scores"]["overall"] is not None


# Registration is authorized by HSTM source/output proof, never by these checks.
def test_registered_sources_and_artifacts_keep_the_authorized_direct_pins():
    import ast
    import hashlib
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / "tests/fixtures/detection_revision/provenance.json").read_text())
    assert manifest["authority"] == "hstm_v2 92f56dc legacy path, byte-identical"
    assert manifest["reference_commit"] == "92f56dc344917fdbd0d9205a48e12f821270c2bc"
    assert len(manifest["repinned"]["exact_files"]) == 22
    assert len(manifest["repinned"]["allowed_deviations"]) == 4
    assert manifest["repinned"]["case_count"] == 856
    assert manifest["repinned"]["core_difference_count"] == 0
    assert set(manifest["repinned"]["round2_reregistration_completed"]) == {
        "services/guide_prompts.py", "config/constants.py",
    }
    # C-7 is closed: source bytes are hashed directly, with no compatibility substitution.
    for path, expected in manifest["preserved_sha256"].items():
        raw = (root / path).read_bytes()
        if path == "tests/fixtures/reference_parity/golden_update_report.json":
            # F-2 adds only two evidence annotations; retain the original byte pin.
            note = ("ephemeral scratch capture; not retained in Git (AGENTS.md temporary evidence rule). "
                    "Reproduce with scripts/capture_baselines.py --output-dir <scratch> --check.")
            report = json.loads(raw)
            for section in ("same_v6_harness_port_delta", "approved_v6_harness_transition"):
                assert report[section]["evidence_location"] == note
            annotation = ('    "evidence_location": ' + json.dumps(note) + ',\n').encode()
            assert raw.count(annotation) == 2
            raw = raw.replace(annotation, b"")
        assert hashlib.sha256(raw).hexdigest() == expected, path
    for path, expected in manifest["preserved_ast_sha256"].items():
        syntax = ast.dump(ast.parse((root / path).read_text()), include_attributes=False)
        assert hashlib.sha256(syntax.encode()).hexdigest() == expected, path
    for key in ("preserved_function_ast_sha256", "preserved_function_tail_ast_sha256"):
        for path, expected_functions in manifest[key].items():
            tree = ast.parse((root / path).read_text())
            functions = {f"{cls.name}.{function.name}": function for cls in tree.body if isinstance(cls, ast.ClassDef)
                         for function in cls.body if isinstance(function, ast.FunctionDef)}
            functions.update({function.name: function for function in tree.body if isinstance(function, ast.FunctionDef)})
            for name, expected in expected_functions.items():
                function = functions[name]
                if isinstance(expected, dict):
                    function = ast.Module(body=function.body[expected["skip_statements"]:], type_ignores=[])
                    expected = expected["sha256"]
                syntax = ast.dump(function, include_attributes=False)
                assert hashlib.sha256(syntax.encode()).hexdigest() == expected, (path, name)


def test_copied_result_json_core_presence_and_values_match_hstm_92f56dc():
    import hashlib
    import json
    from pathlib import Path

    # Read independently from the seven reference files at 92f56dc, not live ARC output.
    expected = {
        "result_1.json": "8b90532b13d6b4897024d2c9de803120a66a93a2c32cf6da71341451d5c179ab",
        "result_2.json": "76752a6020c6659ef627e8032d1b5ca17647bcf2ec7fbfe144b7e674248be147",
        "result_3.json": "e799cb755c45311f978f6b6f2426621b7bad8d64d13b50513659a833522c7108",
        "result_4.json": "5c3c8339470d64dfe9c5c76794f5c7f99985e6f81ed37f34778812e9dfb2012f",
        "result_5.json": "20d39d97306de51707977c29fdeeb8a7730a190d7ff93db17613b92b40f21d09",
        "result_cco_1.json": "2c1786b39781a6a51aa5fce26e540bd552b4acfcde8e12f156d6e576e09c68e6",
        "result_vo_1.json": "a46d14a9d3991932577847b235fde8216d2931293f2986d0302be07c31836d23",
    }
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / "tests/fixtures/detection_revision/provenance.json").read_text())
    assert manifest["copied_result_core"]["presence_and_value_sha256"] == expected
    keys = ("cpr_score", "metrics", "aed_score", "action_count")
    for name, digest in expected.items():
        value = json.loads((root / "tests/dataset" / name).read_text())
        assert {key for key in keys if key in value} == {"cpr_score", "metrics", "aed_score"}
        core = {key: {"present": True, "value": value[key]} if key in value else {"present": False} for key in keys}
        raw = json.dumps(core, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()
        assert hashlib.sha256(raw).hexdigest() == digest, name
    # action_count is absent in these historical JSONs; the 856 live cases cover it.
    # Their stored rate scores remain intact; test_result uses raw-derived RATE_SCORES.

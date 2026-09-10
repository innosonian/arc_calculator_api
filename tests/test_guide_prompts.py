# 원본: hstm_v2 tests/test_guide_prompts.py (ARC 각색 이식 — 판정 문자열(_build_hstream_judgement)
# 테스트와 ERC2020 한국어 케이스는 §4 제거 대상이라 미이식. 프롬프트북 불변식은 ARC 3권
# (prompt_book_2020arc_{eng_us,eng_uk,korean}.json — 스펙 §4.2) 기준으로 축소.)
from services.guide_prompts import build_guide_prompts, load_prompt_book


def _total_scores(**overrides):
    base = dict(
        score_comp_depth=100, score_recoil=100, score_comp_rate=100, score_ccf=100,
        score_hand_position=100, score_comp_no=100,
        # score_vent_rate: 총점용(그대로 유지). score_vent_rate_measured: 코칭 판단 전용(가이드가 사용).
        score_vent_rate=100, score_vent_rate_measured=100,
        score_vent_vol=100, score_vent_speed=100, score_vent_count=100, overall=85,
    )
    base.update(overrides)
    return base


def _prompts(target, cpr_cycle_type, **score_overrides):
    calc = {"cpr_total_scores": _total_scores(**score_overrides), "cpr_metrics": {}}
    condition = {
        "guideline": "ARC2020", "target": target,
        "training_type": "cpr", "cpr_cycle_type": cpr_cycle_type,
    }
    return build_guide_prompts(calc, condition, {"Regional_Option": "usa"})


# 요구1: 압박 코칭 숫자는 CompVentRatio(cpr_cycle_type)에서 유도한다(하드코딩 금지).
def test_compression_coaching_number_derived_from_ratio():
    # 압박횟수 점수를 최저로 만들어 CompressionNo 코칭이 선택되게 한다.
    for target in ("child", "infant"):
        p = _prompts(target, "152", score_comp_no=10)
        assert any("Compress the chest 15 times each cycle" in x for x in p), (target, p)
        assert not any("30 times" in x for x in p), (target, p)


def test_compression_coaching_number_stays_30_for_30_2():
    p = _prompts("adult", "302", score_comp_no=10)
    assert any("Compress the chest 30 times each cycle" in x for x in p), p


# 요구2(근본원인 수정, H2): 횟수 코칭(VentilationCount←score_vent_count)과 속도 코칭
# (VentilationRate←score_vent_rate_measured)은 독립 지표다. 각자 '가장 취약할 때'만 코칭되고,
# 만점(100) 지표는 코칭하지 않으며, 속도는 '측정 가능(measured)'할 때만 지표가 된다.
def test_count_coaching_shown_when_count_is_weakest():
    # 환기 '횟수'가 가장 취약하면 횟수 코칭이 %d로 채워져 출력된다("two"가 아닌 "2").
    p = _prompts("child", "152", score_vent_count=10)
    assert any("Perform 2 ventilations during each cycle" in x for x in p), p


def test_count_coaching_hidden_when_count_perfect():
    # 횟수가 만점이면 횟수 코칭은 출력되지 않는다(다른 지표가 최저면 그쪽이 코칭됨).
    p = _prompts("child", "152", score_vent_count=100, score_comp_depth=10)
    assert not any("ventilations during each cycle" in x for x in p), p


def test_rate_coaching_driven_by_measured_signal():
    # 속도 코칭은 '측정 가능' 신호(score_vent_rate_measured)로 구동되며 횟수 점수(만점)와 무관하다.
    p = _prompts("child", "152", score_vent_rate_measured=10, score_vent_count=100)
    assert any("per minute" in x for x in p), p
    assert not any("ventilations during each cycle" in x for x in p), p


def test_no_empty_slot_when_count_adequate_but_other_metric_deficient():
    # 환기 횟수 만점(횟수 코칭 미선택)이어도 다음으로 나쁜 지표가 코칭된다(빈 슬롯 방지).
    p = _prompts("child", "152", score_vent_count=100, score_recoil=20)
    assert any("recoil" in x.lower() for x in p), p


def test_m1_unmeasurable_rate_does_not_mask_count():
    # M1 해소: 환기 ≤1회/사이클 → 속도 측정 불가(measured=None), 횟수 미달(count=50).
    # 측정 불가 속도는 지표에서 제외되어, 정답인 횟수 코칭만 나오고 속도 코칭은 없다.
    p = _prompts("adult", "152", score_vent_rate_measured=None, score_vent_count=50)
    assert any("Perform 2 ventilations during each cycle" in x for x in p), p
    assert not any("per minute" in x for x in p), p


def test_m2_zero_ventilation_shows_count_not_rate():
    # M2 해소: 무환기 → 속도 측정 불가(measured=None), 횟수=0. 횟수 코칭만, 속도 코칭 없음.
    p = _prompts("adult", "152", overall=45, score_vent_vol=0, score_vent_rate_measured=None, score_vent_count=0)
    assert any("Perform 2 ventilations during each cycle" in x for x in p), p
    assert not any("per minute" in x for x in p), p


def test_f2_partial_short_cycle_coaches_count_not_artifact_rate():
    # F2 해소: 대부분 정확(measured=100)이지만 횟수는 살짝 미달(90=일부 사이클 짧음).
    # 아티팩트가 속도를 오염시키지 않으므로, 속도(만점) 대신 실제 결함인 횟수가 코칭된다.
    p = _prompts("adult", "152", score_vent_rate_measured=100, score_vent_count=90)
    assert any("Perform 2 ventilations during each cycle" in x for x in p), p
    assert not any("per minute" in x for x in p), p


def test_count_and_rate_independent_both_can_show_when_both_deficient():
    # 독립 지표: 둘 다 진짜 미달이면(poor 세션) 둘 다 코칭될 수 있다(오진 아님).
    p = _prompts("adult", "152", overall=45, score_vent_count=10, score_vent_rate_measured=20)
    assert any("Perform 2 ventilations during each cycle" in x for x in p), p
    assert any("per minute" in x for x in p), p


def test_perfect_metric_never_coached():
    # 완벽지표 가드: 만점(100) 지표에는 교정 문구를 붙이지 않는다.
    # 유일한 결함이 압박깊이(10)면 그것만 코칭되고, 만점 지표의 교정 문구는 없다.
    p = _prompts("adult", "302", score_comp_depth=10)
    assert any("depth" in x.lower() for x in p), p
    assert not any("recoil" in x.lower() for x in p), p  # recoil=100 → 코칭 안 됨
    assert not any("per minute" in x for x in p) and not any("during each cycle" in x for x in p), p


def test_infant_vent_count_coaching():
    # infant(Baby CPR)도 횟수 코칭이 %d로 구동된다.
    p = _prompts("infant", "152", score_vent_count=10)
    assert any("Perform 2 ventilations during each cycle" in x for x in p), p


def test_m1_unmeasurable_rate_child_and_infant():
    # M1 해소가 child/infant에서도 성립: measured=None + count=50 → 횟수 코칭, 속도 없음.
    for target in ("child", "infant"):
        p = _prompts(target, "152", score_vent_rate_measured=None, score_vent_count=50)
        assert any("Perform 2 ventilations during each cycle" in x for x in p), (target, p)
        assert not any("per minute" in x for x in p), (target, p)


def test_poor_session_emits_two_distinct_extras():
    # poor 세션(overall<60) → 가장 취약한 서로 다른 두 지표가 코칭된다.
    p = _prompts("adult", "302", overall=40, score_comp_depth=10, score_recoil=20)
    extras = [x for x in p if not x.startswith("• Overall")]
    assert len(extras) == 2, p
    assert len(set(extras)) == 2, p


def test_all_prompt_books_cpr_sections_have_count_and_rate():
    # 데이터 불변식: 참고 프로젝트의 9권 × 3 CPR 섹션 모두
    # VentilationCount(정확히 %d 1개) + VentilationRate(속도) 보유.
    import glob
    import json
    import os
    base = os.path.join(os.path.dirname(__file__), "..", "resources", "prompt_books")
    paths = sorted(glob.glob(os.path.join(base, "*.json")))
    assert len(paths) == 9, paths
    expected_names = {
        "prompt_book_2020arc_eng_uk.json",
        "prompt_book_2020arc_eng_us.json",
        "prompt_book_2020arc_korean.json",
        "prompt_book_2020erc_eng_uk.json",
        "prompt_book_2020erc_eng_us.json",
        "prompt_book_2020erc_korean.json",
        "prompt_book_eng_uk.json",
        "prompt_book_eng_us.json",
        "prompt_book_korean.json",
    }
    assert {os.path.basename(p) for p in paths} == expected_names, paths
    for path in paths:
        book = json.load(open(path, encoding="utf-8"))
        for section in ("CPR Training", "Child CPR Training", "Baby CPR Training"):
            prompt = book[section]["2nd 3rd prompt"]
            count = prompt.get("VentilationCount", "")
            rate = prompt.get("VentilationRate", "")
            assert count.count("%d") == 1, f"{path}/{section} count={count!r}"
            assert ("per minute" in rate) or ("분당" in rate), f"{path}/{section} rate={rate!r}"


def test_comp_vent_targets_matches_border_selection():
    # comp_vent_targets 값과 압박수 채점 border 선택이 동일 cpr_cycle_type 해석을 쓴다.
    from services.http.schemas import comp_vent_targets
    from services.config import Config
    for cyc, expect_comp, expect_border in (
        ("152", 15, "comp_cnt_152"), ("302", 30, "comp_cnt_302"),
        ("15:2", 30, "comp_cnt_302"), ("", 30, "comp_cnt_302"),
        # JSON 숫자 152: str 변환으로 관대하게 해석하면 문구(15)와 채점(30:2)이 갈라지므로
        # 양쪽 모두 30:2로 떨어져야 한다.
        (152, 30, "comp_cnt_302"),
    ):
        comp, vent = comp_vent_targets({"cpr_cycle_type": cyc})
        assert (comp, vent) == (expect_comp, 2), (cyc, comp, vent)
        cfg = Config({"mode": "training", "target": "adult", "training_type": "cpr",
                      "guideline": "ARC2020", "cpr_cycle_type": cyc, "is_2rescuers": False})
        border = "comp_cnt_152" if cfg.calculation_config.condition["cpr_cycle_type"] == "152" else "comp_cnt_302"
        assert border == expect_border, (cyc, border)


# 요구2(point 2): infant도 target이 아니라 cpr_cycle_type에 따라 압박 숫자가 유동적으로 대응된다.
def test_infant_compression_number_follows_cpr_cycle_type():
    p152 = _prompts("infant", "152", score_comp_no=10)
    assert any("15 times" in x for x in p152) and not any("30 times" in x for x in p152), p152
    p302 = _prompts("infant", "302", score_comp_no=10)
    assert any("30 times" in x for x in p302) and not any("15 times" in x for x in p302), p302


def test_arc2020_korean_adult_ventilation_volume_uses_600ml():
    prompt = load_prompt_book("ARC2020", "korean")
    assert prompt is not None
    adult_cpr_vv = prompt["CPR Training"]["2nd 3rd prompt"]["VentilationVolume"]
    for key in ("under", "wrong", "over"):
        assert "600" in adult_cpr_vv[key], f"missing 600 in {key}: {adult_cpr_vv[key]}"
        assert "700" not in adult_cpr_vv[key], f"700 still in {key}: {adult_cpr_vv[key]}"


def test_arc2020_korean_child_ventilation_volume_uses_600ml():
    prompt = load_prompt_book("ARC2020", "korean")
    assert prompt is not None
    child_cpr_vv = prompt["Child CPR Training"]["2nd 3rd prompt"]["VentilationVolume"]
    for key in ("under", "wrong", "over"):
        assert "600" in child_cpr_vv[key], f"missing 600 in {key}: {child_cpr_vv[key]}"
        assert "700" not in child_cpr_vv[key], f"700 still in {key}: {child_cpr_vv[key]}"


def test_arc2020_english_ventilation_volume_uses_600ml():
    expected = "Provide a ventilation volume of between 400ml and 600ml for each ventilation"
    for region in ("usa", "british"):
        prompt = load_prompt_book("ARC2020", region)
        assert prompt is not None
        for section in ("Ventilation only", "CPR Training", "Child Ventilation only", "Child CPR Training"):
            vv = prompt[section]["2nd 3rd prompt"]["VentilationVolume"]
            for key in ("under", "wrong", "over"):
                assert vv[key] == expected, f"{region}/{section}/{key}: {vv[key]}"


def test_arc2025_returns_same_prompt_as_arc2020():
    for region in ("korean", "usa", "british"):
        prompt_2020 = load_prompt_book("ARC2020", region)
        prompt_2025 = load_prompt_book("ARC2025", region)
        assert prompt_2020 is not None
        assert prompt_2025 is not None
        for section in ("CPR Training", "Ventilation only", "Child CPR Training"):
            assert (
                prompt_2020[section]["2nd 3rd prompt"]["VentilationVolume"]
                == prompt_2025[section]["2nd 3rd prompt"]["VentilationVolume"]
            ), f"{region}/{section} differs between ARC2020 and ARC2025"


def test_baby_ventilation_volume_korean_unchanged():
    prompt = load_prompt_book("ARC2020", "korean")
    assert prompt is not None
    baby_cpr_vv = prompt["Baby CPR Training"]["2nd 3rd prompt"]["VentilationVolume"]
    for key in ("under", "wrong", "over"):
        assert "20ml" in baby_cpr_vv[key] or "40ml" in baby_cpr_vv[key], (
            f"baby {key} missing 20ml/40ml reference: {baby_cpr_vv[key]}"
        )
        assert "600" not in baby_cpr_vv[key]
        assert "700" not in baby_cpr_vv[key]

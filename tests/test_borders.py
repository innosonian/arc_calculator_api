# 원본: hstm_v2 tests/test_borders.py (ARC 각색 이식 — AHA2020/ERC2020/STD2015 케이스 제거,
# ARC2020/ARC2025 케이스 유지. ARC2025는 ARC2020과 동일 값 별칭(스펙 §2).)
from config.borders import AdultBorder, ChildBorder, InfantBorder, trapezium_get_point


def test_adult_arc2020_and_arc2025_same_high_at_650ml():
    adult_2020 = AdultBorder("ARC2020")
    adult_2025 = AdultBorder("ARC2025")
    expected = {"grade": 50, "criterion": "high"}
    assert trapezium_get_point(adult_2020, "vent_vol", 650) == expected
    assert trapezium_get_point(adult_2025, "vent_vol", 650) == expected


def test_adult_arc2020_and_arc2025_same_good_at_550ml():
    adult_2020 = AdultBorder("ARC2020")
    adult_2025 = AdultBorder("ARC2025")
    expected = {"grade": 100, "criterion": "good"}
    assert trapezium_get_point(adult_2020, "vent_vol", 550) == expected
    assert trapezium_get_point(adult_2025, "vent_vol", 550) == expected


def test_child_arc2020_and_arc2025_same_high_at_650ml():
    child_2020 = ChildBorder("ARC2020")
    child_2025 = ChildBorder("ARC2025")
    expected = {"grade": 50, "criterion": "high"}
    assert trapezium_get_point(child_2020, "vent_vol", 650) == expected
    assert trapezium_get_point(child_2025, "vent_vol", 650) == expected


def test_child_arc2020_and_arc2025_same_good_at_550ml():
    child_2020 = ChildBorder("ARC2020")
    child_2025 = ChildBorder("ARC2025")
    expected = {"grade": 100, "criterion": "good"}
    assert trapezium_get_point(child_2020, "vent_vol", 550) == expected
    assert trapezium_get_point(child_2025, "vent_vol", 550) == expected


def test_infant_arc2020_vent_vol_unchanged():
    infant_2020 = InfantBorder("ARC2020")
    assert trapezium_get_point(infant_2020, "vent_vol", 30) == {"grade": 100, "criterion": "good"}


def test_infant_arc2025_same_as_arc2020():
    # ARC2025 = ARC2020 별칭(스펙 §2) — infant도 동일 값이어야 한다.
    infant_2025 = InfantBorder("ARC2025")
    assert trapezium_get_point(infant_2025, "vent_vol", 30) == {"grade": 100, "criterion": "good"}
    assert InfantBorder("ARC2020").border == infant_2025.border

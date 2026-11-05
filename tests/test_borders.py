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


def test_aha2020_adult_vent_vol_unchanged_keeps_650ml_as_good():
    adult_aha = AdultBorder("AHA2020")
    assert trapezium_get_point(adult_aha, "vent_vol", 650) == {"grade": 100, "criterion": "good"}


def test_erc2020_adult_vent_vol_unchanged_keeps_650ml_as_good():
    adult_erc = AdultBorder("ERC2020")
    assert trapezium_get_point(adult_erc, "vent_vol", 650) == {"grade": 100, "criterion": "good"}


def test_std2015_adult_vent_vol_unchanged_keeps_650ml_as_good():
    adult_std = AdultBorder("STD2015")
    assert trapezium_get_point(adult_std, "vent_vol", 650) == {"grade": 100, "criterion": "good"}


def test_infant_arc2020_vent_vol_unchanged():
    infant_2020 = InfantBorder("ARC2020")
    assert trapezium_get_point(infant_2020, "vent_vol", 30) == {"grade": 100, "criterion": "good"}

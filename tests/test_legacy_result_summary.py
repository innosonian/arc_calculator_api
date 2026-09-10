"""ResultSummary 구성(_build_result_summary)의 HstreamId 매핑 회귀 테스트.

HSTM 완료 제출(UpdateResuscitationManikinCompletion)은 학습자 ID 필드가 필수이며,
누락 시 400 "The hStream id is invalid."로 거부된다(2026-06 QA·프로덕션 전량 실패
건). 앱이 보낸 Usage.hstreamId가 확정 스펙 필드명(대문자 HstreamId)으로 매핑되고,
앱이 소문자 키로 보낸 값도 대문자 키 하나로 흡수되는지 고정한다.
"""

import pytest

from services.legacy_document import _NO_SCORE_SUFFIX, _WITHHELD_SCORE, _build_result_summary


@pytest.fixture(autouse=True)
def _isolate_withhold_flag(monkeypatch):
    # hStreamScore '--' 은닉은 HSTM_V2_WITHHOLD_SCORE로 켠다. 이 파일의 기본 케이스는
    # 플래그 OFF(숫자)를 고정하므로, 호스트/CI 환경에 값이 있어도 영향받지 않도록 매 테스트
    # 시작 시 지운다. 플래그 ON 케이스(TestHstreamScoreWithholdFlag)는 각자 monkeypatch.setenv로
    # 다시 설정한다(둘 다 monkeypatch라 테스트 후 자동 원복).
    monkeypatch.delenv("HSTM_V2_WITHHOLD_SCORE", raising=False)


RESULT = {
    "cpr_score": {"total_score": {"overall": 85}},
    "action_count": {"comp": 60, "vent": 4},
    "training_stats": {"cycle_count": 3, "elapsed_seconds": 60},
}
VENT_ONLY_CONDITION = {
    "mode": "training",
    "target": "adult",
    "training_type": "ventilation_only",
    "guideline": "AHA2020",
    "cpr_cycle_type": "302",
    "is_2rescuers": False,
}
COMPRESSION_ONLY_CONDITION = {
    "mode": "training",
    "target": "adult",
    "training_type": "compression_only",
    "guideline": "AHA2020",
    "cpr_cycle_type": "302",
    "is_2rescuers": False,
}
DOCUMENT = {
    "Usage": {"hstreamId": "4a2a1d1b-ab1c-4b4d-9e40-888b82f2d17c", "Type": "Assessment"},
    "Dummy": {"DeviceID": "Elevate SmartManikin"},
    "Organization": {"org_id": "285b5dfe-8a16-e911-b189-005056b10657"},
    "Open_Skill": {"Passing_Score": "80"},
}


class TestHstreamIdMapping:
    def test_mapped_from_usage(self):
        summary = _build_result_summary({}, RESULT, DOCUMENT, None)
        assert summary["HstreamId"] == "4a2a1d1b-ab1c-4b4d-9e40-888b82f2d17c"
        assert "hstreamId" not in summary

    def test_app_provided_lowercase_absorbed(self):
        # 앱이 구 계약(소문자 hstreamId)으로 보낸 값은 우선 사용하되 대문자 키로만 내보낸다.
        base = {"hstreamId": "app-provided-id"}
        summary = _build_result_summary(base, RESULT, DOCUMENT, None)
        assert summary["HstreamId"] == "app-provided-id"
        assert "hstreamId" not in summary

    def test_app_provided_capital_preserved(self):
        base = {"HstreamId": "app-capital-id"}
        summary = _build_result_summary(base, RESULT, DOCUMENT, None)
        assert summary["HstreamId"] == "app-capital-id"

    def test_app_null_capital_key_filled_from_usage(self):
        # 앱이 새 스펙 템플릿의 플레이스홀더처럼 키만 보내고 값이 비어 있으면 유효한 값으로 채운다.
        base = {"HstreamId": None}
        summary = _build_result_summary(base, RESULT, DOCUMENT, None)
        assert summary["HstreamId"] == "4a2a1d1b-ab1c-4b4d-9e40-888b82f2d17c"

    def test_missing_usage_keeps_field_none(self):
        summary = _build_result_summary({}, RESULT, {"Usage": {}}, None)
        assert summary["HstreamId"] is None

    def test_other_required_fields_still_set(self):
        summary = _build_result_summary({}, RESULT, DOCUMENT, None)
        assert summary["ManikinDeviceId"] == "Elevate SmartManikin"
        assert summary["OrgId"] == "285b5dfe-8a16-e911-b189-005056b10657"
        assert summary["hStreamScore"] == 85
        assert summary["CompressionNo"] == 60
        assert summary["hStreamResult"] == "Pass"


class TestMeasuredFieldMapping:
    """ResultSummary 실측 필드는 metrics의 실측 평균(V1 계산기와 동일 의미)에서 온다."""

    RESULT = {
        "cpr_score": {"total_score": {"overall": 85, "score_hand_position": 97}},
        "action_count": {"comp": 60, "vent": 4},
        "training_stats": {"cycle_count": 2, "elapsed_seconds": 60},
        "metrics": {
            "AvgCompressionRate": 108,
            "AvgCompressionDepth": 53,
            "AvgVentilationVolume": 560,
            "AvgVentilationSpeed": 820,
            "HandsOffTimeSec": 3,
            "CCF": {"%_CCF": 88},
        },
    }

    def test_measured_fields_mapped(self):
        summary = _build_result_summary({}, self.RESULT, DOCUMENT, None)
        assert summary["CompressionRate"] == 108
        assert summary["CompressionDepth"] == 53
        assert summary["VentilationVolume"] == 560
        assert summary["VentilationSpd"] == 820
        assert summary["HandsOffTime"] == 3
        assert summary["%CCF"] == 88
        assert summary["Handposition"] == 97

    def test_missing_metrics_default_to_zero(self):
        summary = _build_result_summary({}, RESULT, DOCUMENT, None)
        assert summary["CompressionRate"] == 0
        assert summary["CompressionDepth"] == 0
        assert summary["HandsOffTime"] == 0
        assert summary["%CCF"] == 0
        assert summary["Handposition"] == 0


class TestHstreamJudgement:
    """hStreamResult/hStreamReason은 V1 계산기(get_hstream_result)의 게이트 규칙을 따른다.

    총점 >= Passing_Score 외에 추가 4게이트(최소 시도량·사이클별 압박 횟수 점수 최솟값 50·
    Hand Position 80·Recoil 80)를 검사한다(INNO-4497 이식, INNO-4643에서 일시 폐기 후 복원).
    게이트 실패 시 판정(hStreamResult=Fail·사유)은 항상 적용된다. 다만 hStreamScore를 '--'로
    숨기는 것은 기본 OFF다(HSTM 완료 API가 int 필드에 문자열 '--' 수용을 확인해야 하므로).
    이 클래스는 기본(플래그 OFF) 동작을, 플래그 ON 동작은 TestHstreamScoreWithholdFlag가 고정한다.
    """

    def _result(self, overall=85, cycles=3, part_cycles=None, **total_extra):
        result = {
            "cpr_score": {"total_score": {"overall": overall, **total_extra}},
            "action_count": {"comp": 90, "vent": 6},
            "training_stats": {"cycle_count": cycles, "elapsed_seconds": 60},
        }
        if part_cycles is not None:
            result["cpr_score"]["part_scores"] = [{"cycle_with_score_list": part_cycles}]
        return result

    def _summary(self, result, condition=None):
        return _build_result_summary({}, result, DOCUMENT, condition)

    def test_pass_message(self):
        summary = self._summary(self._result())
        assert summary["hStreamResult"] == "Pass"
        assert summary["hStreamReason"] == "Well Done!\n\nYou passed!"
        # 합격이면 hStreamScore는 실제 총점(정수)을 그대로 유지한다.
        assert summary["hStreamScore"] == 85

    def test_pass_at_exact_boundary(self):
        # overall == passing → Pass ( overall < passing 이 아니므로 ).
        summary = self._summary(self._result(overall=80))
        assert summary["hStreamResult"] == "Pass"
        assert summary["hStreamScore"] == 80

    def test_not_enough_cycles_with_good_score(self):
        # 총점은 기준점 이상이지만 3사이클 미만 → Fail. 기본(플래그 OFF)에선 점수는 숫자 유지.
        summary = self._summary(self._result(cycles=2))
        assert summary["hStreamResult"] == "Fail"
        assert summary["hStreamReason"] == "You were doing well, but stopped early.\n\nNo score will be provided."
        assert summary["hStreamScore"] == 85

    def test_not_enough_cycles_with_low_score(self):
        summary = self._summary(self._result(overall=50, cycles=2))
        assert summary["hStreamResult"] == "Fail"
        assert summary["hStreamReason"] == "Not quite there yet. You stopped early.\n\nNo score will be provided."
        assert summary["hStreamScore"] == 50

    def test_no_attempt(self):
        summary = self._summary(self._result(overall=0, cycles=0))
        assert summary["hStreamResult"] == "Fail"
        assert summary["hStreamReason"] == "You did not attempt this skill.\n\nNo score will be provided."
        assert summary["hStreamScore"] == 0

    def test_below_passing_score(self):
        # 총점 미달 Fail은 게이트 실패가 아니므로 사유가 접미사로 끝나지 않고 점수를 노출한다.
        summary = self._summary(self._result(overall=70))
        assert summary["hStreamResult"] == "Fail"
        assert summary["hStreamReason"] == "You did not pass.\n\nYou must repeat the skill."
        assert summary["hStreamScore"] == 70

    def test_low_comp_no_cycle_fails(self):
        # score_comp_no가 None인 사이클(rescue vent, VP)은 최솟값 판정에서 제외된다.
        cycles = [{"score_comp_no": 100}, {"score_comp_no": 40}, {"score_comp_no": None}]
        summary = self._summary(self._result(part_cycles=cycles))
        assert summary["hStreamResult"] == "Fail"
        assert summary["hStreamReason"] == (
            "You were doing well, but compress the chest 30 times each cycle.\n\nNo score will be provided."
        )
        assert summary["hStreamScore"] == 85

    def test_low_hand_position_fails(self):
        summary = self._summary(self._result(score_hand_position=70))
        assert summary["hStreamResult"] == "Fail"
        assert summary["hStreamReason"] == (
            "You were doing well, but compress in the middle of the chest.\n\nNo score will be provided."
        )
        assert summary["hStreamScore"] == 85

    def test_low_recoil_fails(self):
        summary = self._summary(self._result(score_recoil=70))
        assert summary["hStreamResult"] == "Fail"
        assert summary["hStreamReason"] == (
            "You were doing well, but allow the chest to fully recoil between compressions.\n\nNo score will be provided."
        )
        assert summary["hStreamScore"] == 85

    def test_vent_only_skips_comp_gates(self):
        # vent only는 압박 게이트(comp_no·hand position·recoil)를 적용하지 않는다.
        result = self._result(score_hand_position=0, score_recoil=0)
        result["action_count"] = {"comp": 0, "vent": 12}
        summary = self._summary(result, VENT_ONLY_CONDITION)
        assert summary["hStreamResult"] == "Pass"
        assert summary["hStreamScore"] == 85

    def test_vent_only_not_enough_vents(self):
        result = self._result()
        result["action_count"] = {"comp": 0, "vent": 5}
        summary = self._summary(result, VENT_ONLY_CONDITION)
        assert summary["hStreamResult"] == "Fail"
        assert summary["hStreamReason"] == "You were doing well, but stopped early.\n\nNo score will be provided."
        assert summary["hStreamScore"] == 85

    # --- compression_only 최소 시도 게이트(압박 60회) 및 게이트 라우팅 ---

    def test_compression_only_not_enough_comps_fails(self):
        # compression only는 압박 60회 미만이면 최소 시도 실패 → Fail.
        result = self._result()
        result["action_count"] = {"comp": 30, "vent": 0}
        summary = self._summary(result, COMPRESSION_ONLY_CONDITION)
        assert summary["hStreamResult"] == "Fail"
        assert summary["hStreamReason"] == "You were doing well, but stopped early.\n\nNo score will be provided."
        assert summary["hStreamScore"] == 85

    def test_compression_only_enough_comps_passes(self):
        result = self._result()
        result["action_count"] = {"comp": 60, "vent": 0}
        summary = self._summary(result, COMPRESSION_ONLY_CONDITION)
        assert summary["hStreamResult"] == "Pass"
        assert summary["hStreamScore"] == 85

    def test_compression_only_skips_comp_no_gate_but_applies_hand_recoil(self):
        # comp_no 게이트는 cpr 전용이므로 compression only에서는 낮은 사이클 압박수로 실패하지 않는다.
        cycles = [{"score_comp_no": 100}, {"score_comp_no": 40}, {"score_comp_no": None}]
        result = self._result(part_cycles=cycles)
        result["action_count"] = {"comp": 90, "vent": 0}
        summary = self._summary(result, COMPRESSION_ONLY_CONDITION)
        assert summary["hStreamResult"] == "Pass"
        # 그러나 hand position/recoil 게이트는 여전히 적용된다.
        result = self._result(score_hand_position=70)
        result["action_count"] = {"comp": 90, "vent": 0}
        summary = self._summary(result, COMPRESSION_ONLY_CONDITION)
        assert summary["hStreamResult"] == "Fail"

    # --- 경계값(off-by-one) 회귀: 임계 정확히 통과하면 Pass·점수 유지 ---

    def test_hand_position_boundary_80_passes(self):
        summary = self._summary(self._result(score_hand_position=80))
        assert summary["hStreamResult"] == "Pass"
        assert summary["hStreamScore"] == 85

    def test_recoil_boundary_80_passes(self):
        summary = self._summary(self._result(score_recoil=80))
        assert summary["hStreamResult"] == "Pass"
        assert summary["hStreamScore"] == 85

    def test_comp_no_boundary_50_passes(self):
        # 사이클 최소 압박 점수가 정확히 50이면 (< 50 아님) 게이트 통과.
        cycles = [{"score_comp_no": 100}, {"score_comp_no": 50}, {"score_comp_no": None}]
        summary = self._summary(self._result(part_cycles=cycles))
        assert summary["hStreamResult"] == "Pass"
        assert summary["hStreamScore"] == 85

    def test_all_none_comp_no_passes(self):
        # rescue vent/VP처럼 모든 사이클 score_comp_no가 None이면 comp_no 게이트에서 제외된다.
        cycles = [{"score_comp_no": None}, {"score_comp_no": None}]
        summary = self._summary(self._result(part_cycles=cycles))
        assert summary["hStreamResult"] == "Pass"
        assert summary["hStreamScore"] == 85

    # --- 기본(플래그 OFF): 게이트 실패여도 hStreamScore는 항상 숫자 ---

    def test_score_not_withheld_by_default(self):
        # 모든 게이트 실패 시나리오에서 판정은 Fail이지만 기본 OFF라 점수는 숫자로 남는다.
        gate_fail_cases = [
            self._result(overall=0, cycles=0),
            self._result(cycles=2),
            self._result(overall=50, cycles=2),
            self._result(part_cycles=[{"score_comp_no": 40}]),
            self._result(score_hand_position=70),
            self._result(score_recoil=70),
        ]
        for result in gate_fail_cases:
            summary = self._summary(result)
            assert summary["hStreamResult"] == "Fail"
            assert summary["hStreamReason"].endswith(_NO_SCORE_SUFFIX)
            assert isinstance(summary["hStreamScore"], int)  # '--' 아님(기본 OFF)

    def test_no_passing_score_keeps_app_values(self):
        summary = _build_result_summary({"hStreamReason": "app-reason"}, self._result(), {"Usage": {}}, None)
        assert summary["hStreamReason"] == "app-reason"
        assert "hStreamResult" not in summary
        # Passing_Score가 없으면 게이트 판정 자체를 하지 않으므로 점수는 총점 그대로다.
        assert summary["hStreamScore"] == 85


class TestHstreamScoreWithholdFlag:
    """HSTM_V2_WITHHOLD_SCORE=ON이면 게이트 실패 시 hStreamScore를 '--'로 숨긴다.

    총점 미달 Fail("...must repeat the skill.")과 합격은 플래그와 무관하게 실제 점수를 노출한다.
    HSTM 완료 API가 문자열 '--' 수용을 확인한 뒤에만 켠다(미수용 시 500).
    """

    def _result(self, overall=85, cycles=3, part_cycles=None, **total_extra):
        result = {
            "cpr_score": {"total_score": {"overall": overall, **total_extra}},
            "action_count": {"comp": 90, "vent": 6},
            "training_stats": {"cycle_count": cycles, "elapsed_seconds": 60},
        }
        if part_cycles is not None:
            result["cpr_score"]["part_scores"] = [{"cycle_with_score_list": part_cycles}]
        return result

    def _summary(self, result, condition=None):
        return _build_result_summary({}, result, DOCUMENT, condition)

    def test_gate_failures_withhold_when_enabled(self, monkeypatch):
        monkeypatch.setenv("HSTM_V2_WITHHOLD_SCORE", "1")
        withheld_cases = [
            self._result(overall=0, cycles=0),  # no attempt
            self._result(cycles=2),  # early stop, good score
            self._result(overall=50, cycles=2),  # early stop, low score
            self._result(part_cycles=[{"score_comp_no": 40}]),  # comp_no gate
            self._result(score_hand_position=70),  # hand position gate
            self._result(score_recoil=70),  # recoil gate
        ]
        for result in withheld_cases:
            summary = self._summary(result)
            assert summary["hStreamResult"] == "Fail"
            assert summary["hStreamReason"].endswith(_NO_SCORE_SUFFIX)
            assert summary["hStreamScore"] == _WITHHELD_SCORE

    def test_below_passing_shows_score_even_when_enabled(self, monkeypatch):
        monkeypatch.setenv("HSTM_V2_WITHHOLD_SCORE", "on")
        summary = self._summary(self._result(overall=70))
        assert summary["hStreamResult"] == "Fail"
        assert summary["hStreamReason"] == "You did not pass.\n\nYou must repeat the skill."
        assert summary["hStreamScore"] == 70

    def test_pass_shows_score_even_when_enabled(self, monkeypatch):
        monkeypatch.setenv("HSTM_V2_WITHHOLD_SCORE", "true")
        summary = self._summary(self._result())
        assert summary["hStreamResult"] == "Pass"
        assert summary["hStreamScore"] == 85

    def test_off_or_unset_keeps_numeric(self, monkeypatch):
        # 명시적 off/빈값/무의미값은 OFF로 취급(숫자 유지).
        for value in ("0", "false", "off", "", "nope"):
            monkeypatch.setenv("HSTM_V2_WITHHOLD_SCORE", value)
            summary = self._summary(self._result(score_recoil=70))
            assert summary["hStreamResult"] == "Fail"
            assert summary["hStreamScore"] == 85


class TestLungGraph:
    """LungGraph는 V1 계산기(get_lung_graph_gauge)와 동일하게 평균 환기량으로 좌우 동일 생성."""

    def _result(self, avg_volume):
        return {
            "cpr_score": {"total_score": {"overall": 85}},
            "action_count": {"comp": 90, "vent": 6},
            "training_stats": {"cycle_count": 3, "elapsed_seconds": 60},
            "metrics": {"AvgVentilationVolume": avg_volume},
        }

    def test_good_volume_is_blue(self):
        # adult vent_vol 경계 [300, 400, 700, 800]: 550ml → blue, percent 550/700
        summary = _build_result_summary({}, self._result(550), DOCUMENT, None)
        assert summary["LungGraph"] == {
            "LeftPercent": 78,
            "LeftLevel": 550,
            "LeftColor": "blue",
            "RightPercent": 78,
            "RightLevel": 550,
            "RightColor": "blue",
        }

    def test_low_volume_is_grey(self):
        summary = _build_result_summary({}, self._result(300), DOCUMENT, None)
        assert summary["LungGraph"]["LeftColor"] == "grey"
        assert summary["LungGraph"]["LeftPercent"] == 42

    def test_high_volume_is_red_capped_100(self):
        summary = _build_result_summary({}, self._result(750), DOCUMENT, None)
        assert summary["LungGraph"]["LeftColor"] == "red"
        assert summary["LungGraph"]["LeftPercent"] == 100

    def test_infant_uses_infant_border(self):
        condition = {
            "mode": "training",
            "target": "infant",
            "training_type": "cpr",
            "guideline": "AHA2020",
            "cpr_cycle_type": "302",
            "is_2rescuers": False,
        }
        # infant vent_vol 경계 [10, 20, 40, 60]: 30ml → blue, percent 30/40
        summary = _build_result_summary({}, self._result(30), DOCUMENT, condition)
        assert summary["LungGraph"]["LeftColor"] == "blue"
        assert summary["LungGraph"]["LeftPercent"] == 75

    def test_compression_only_has_no_lung_graph(self):
        condition = {
            "mode": "training",
            "target": "adult",
            "training_type": "compression_only",
            "guideline": "AHA2020",
            "cpr_cycle_type": "302",
            "is_2rescuers": False,
        }
        summary = _build_result_summary({}, self._result(0), DOCUMENT, condition)
        assert "LungGraph" not in summary

    def test_compression_only_removes_app_provided_lung_graph(self):
        # base(앱 전달분)에 LungGraph가 와도 compression only에서는 남기지 않는다.
        condition = {
            "mode": "training",
            "target": "adult",
            "training_type": "compression_only",
            "guideline": "AHA2020",
            "cpr_cycle_type": "302",
            "is_2rescuers": False,
        }
        base = {"LungGraph": {"LeftPercent": 50, "LeftLevel": 300, "LeftColor": "blue"}}
        summary = _build_result_summary(base, self._result(0), DOCUMENT, condition)
        assert "LungGraph" not in summary

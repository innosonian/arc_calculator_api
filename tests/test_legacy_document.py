"""HSTM 완료 문서 조립(_build_hstm_document / _apply_calculated_fields) 회귀 테스트.

UpdateResuscitationManikinCompletion 은 ResultSummary 단독으로는 거부되고(500),
전체 문서를 요구한다. 이 테스트는 계산기가 조립하는 문서의 구조 적합성을 고정한다:
- 인식된 top-level 키만 통과시키고 알 수 없는 키는 드롭한다
- ResultSummary/ResultByCriteria/ResultByCycle/Certification/Guide_prompts 는 항상 계산값으로 채운다
- 계산기 guide_prompts(소문자)가 문서 Guide_prompts(대문자)로 매핑된다
"""

from services.legacy_document import (
    _HSTM_TOP_LEVEL_KEYS,
    _apply_calculated_fields,
    _build_hstm_document,
    _build_result_by_criteria,
    _build_result_by_cycle,
)

RESULT = {
    "cpr_score": {
        "total_score": {"overall": 88, "score_comp_depth": 100, "score_ccf": 41},
        "part_scores": [{"cycle_with_score_list": [{"overall": 86, "score_comp_depth": 100}, {"overall": 90, "score_comp_depth": 99}]}],
    },
    "metrics": {"CompressionDepth": {"%_Good": 100}, "ScoreOfCCF": 41},
    "action_count": {"comp": 90, "vent": 8},
    "training_stats": {"cycle_count": 3, "elapsed_seconds": 99},
    "guide_prompts": ["• Compression release is very good", "• Minimize hands off time"],
}

CALCULATED_SECTIONS = {"ResultSummary", "ResultByCycle", "ResultByCriteria", "Certification", "Guide_prompts"}


class TestBuildHstmDocument:
    def test_returns_none_when_no_recognized_top_level_key(self):
        # 인식된 top-level 키가 하나도 없으면 문서를 만들지 않는다(submit 은 결과로 폴백).
        assert _build_hstm_document({"Foo": 1, "bar": 2}, RESULT, None) is None

    def test_guide_prompts_in_body_alone_does_not_create_document(self):
        # Guide_prompts 는 인식된 top-level 키가 아니므로 단독으로는 문서를 만들지 못한다.
        assert _build_hstm_document({"Guide_prompts": ["x"]}, RESULT, None) is None

    def test_drops_unknown_top_level_keys(self):
        document = _build_hstm_document({"ResultSummary": {}, "Foo": 1}, RESULT, None)

        assert "Foo" not in document

    def test_preserves_recognized_passthrough_sections(self):
        body = {
            "ResultSummary": {},
            "Usage": {"hstreamId": "h-1"},
            "Dummy": {"DeviceID": "62cc"},
            "Organization": {"org_id": "o-1"},
        }

        document = _build_hstm_document(body, RESULT, None)

        # 앱이 보낸 값은 보존하고, V1 앱이 채우던 필드는 계산기가 완성한다.
        assert document["Usage"]["hstreamId"] == "h-1"
        assert document["Dummy"]["DeviceID"] == "62cc"
        assert document["Organization"] == {"org_id": "o-1"}

    def test_top_level_keys_constant_matches_conformance_set(self):
        assert _HSTM_TOP_LEVEL_KEYS == {
            "DeviceInfo", "Organization", "Dummy", "Open_Skill", "ResultSummary",
            "Custom", "ResultByCycle", "CalculationService", "Certification",
            "Usage", "ResultByCriteria", "Institution",
        }


class TestApplyCalculatedFields:
    def test_always_sets_all_calculated_sections(self):
        document = _apply_calculated_fields({"ResultSummary": {}}, RESULT, None)

        assert CALCULATED_SECTIONS.issubset(document.keys())

    def test_maps_guide_prompts_from_calculator_result(self):
        # 계산기 guide_prompts(소문자) -> 문서 Guide_prompts(대문자)
        document = _apply_calculated_fields({"ResultSummary": {}}, RESULT, None)

        assert document["Guide_prompts"] == [
            "• Compression release is very good",
            "• Minimize hands off time",
        ]

    def test_guide_prompts_defaults_to_empty_list(self):
        result = {k: v for k, v in RESULT.items() if k != "guide_prompts"}

        document = _apply_calculated_fields({"ResultSummary": {}}, result, None)

        assert document["Guide_prompts"] == []

    def test_guide_prompts_overrides_app_provided_value(self):
        # 앱이 문서에 Guide_prompts 를 넣어 보내도 계산기 값으로 덮어쓴다.
        document = _apply_calculated_fields(
            {"ResultSummary": {}, "Guide_prompts": ["stale"]}, RESULT, None
        )

        assert document["Guide_prompts"] != ["stale"]
        assert document["Guide_prompts"][0] == "• Compression release is very good"

    def test_result_summary_gets_capital_hstream_id_from_usage(self):
        # 확정 스펙 필드명은 대문자 HstreamId. 소문자 키는 문서에 남기지 않는다.
        document = _apply_calculated_fields(
            {"ResultSummary": {}, "Usage": {"hstreamId": "h-1"}}, RESULT, None
        )

        assert document["ResultSummary"]["HstreamId"] == "h-1"
        assert "hstreamId" not in document["ResultSummary"]


class TestBuildResultByCriteria:
    def test_copies_dict_criteria_and_score_of_ccf(self):
        metrics = {"CompressionDepth": {"%_Good": 100}, "ScoreOfCCF": 41}

        rbc = _build_result_by_criteria({}, {"metrics": metrics})

        assert rbc["CompressionDepth"] == {"%_Good": 100}
        assert rbc["ScoreOfCCF"] == 41

    def test_ignores_non_dict_metric_and_keeps_base(self):
        rbc = _build_result_by_criteria(
            {"VentilationSpeed": {}}, {"metrics": {"VentilationSpeed": "not-a-dict"}}
        )

        assert rbc["VentilationSpeed"] == {}

    def test_ignores_unrecognized_metric_keys(self):
        rbc = _build_result_by_criteria({}, {"metrics": {"Junk": {"a": 1}}})

        assert "Junk" not in rbc

    def test_recoil_metric_kept_as_v1_wire_key(self):
        # V1 와이어 키는 Recoil이다. HSTM QA가 V1 실물 키로 검증하므로 개명하지 않는다.
        # ("CompressionRelease"는 V1/V2 앱 모두 화면 표시명일 뿐 페이로드 키가 아님)
        rbc = _build_result_by_criteria({}, {"metrics": {"Recoil": {"%_Good": 90}}})

        assert rbc["Recoil"] == {"%_Good": 90}
        assert "CompressionRelease" not in rbc

    def test_app_provided_compression_release_key_absorbed(self):
        # base(앱 전달분)에 구 개명 키로 온 값은 Recoil로 옮기고 구 키는 남기지 않는다.
        rbc = _build_result_by_criteria({"CompressionRelease": {"%_Good": 70}}, {"metrics": {}})

        assert rbc["Recoil"] == {"%_Good": 70}
        assert "CompressionRelease" not in rbc


class TestBuildResultByCycle:
    def test_each_section_has_by_cycle_and_overall(self):
        rbc = _build_result_by_cycle({}, RESULT)

        assert rbc["ScoreByCycle"] == {"ByCycle": [86, 90], "Overall": 88}
        assert rbc["CompressionDepth"] == {"ByCycle": [100, 99], "Overall": 100}

    def test_missing_score_defaults_overall_zero(self):
        rbc = _build_result_by_cycle({}, RESULT)

        # 데이터 없는 항목도 구조는 생성된다(Overall 0).
        assert rbc["Recoil"]["Overall"] == 0

    def test_keys_match_v1_wire_keys(self):
        # V1 실물 페이로드의 ByCycle 키 구성과 정확히 일치한다(Recoil 유지, VentilationSpeed 상시 포함).
        rbc = _build_result_by_cycle({}, RESULT)

        assert set(rbc.keys()) == {
            "ScoreByCycle", "CompressionDepth", "Recoil", "CompressionRate",
            "ScoreOfCCF", "HandPosition", "CompressionNo", "VentilationVolume", "VentilationRate",
            "VentilationSpeed",
        }

    def test_vent_speed_empty_object_when_not_scored(self):
        # 환기 속도는 infant 마네킨만 측정 가능하다. 채점되지 않으면 V1과 동일하게 빈 객체로 키만 유지.
        rbc = _build_result_by_cycle({}, RESULT)

        assert rbc["VentilationSpeed"] == {}

    def test_vent_speed_filled_when_scored(self):
        result = {
            "cpr_score": {
                "total_score": {"overall": 88, "score_vent_speed": 75},
                "part_scores": [{"cycle_with_score_list": [{"score_vent_speed": 70}, {"score_vent_speed": 80}]}],
            },
        }

        rbc = _build_result_by_cycle({}, result)

        assert rbc["VentilationSpeed"] == {"ByCycle": [70, 80], "Overall": 75}

    def test_stale_base_keys_removed(self):
        # base(앱 전달분)에 구 개명 키(CompressionRelease)가 와도 문서에 남기지 않는다.
        base = {"CompressionRelease": {"ByCycle": [1], "Overall": 1}}
        rbc = _build_result_by_cycle(base, RESULT)

        assert "CompressionRelease" not in rbc
        assert rbc["Recoil"]["Overall"] == 0


class TestCompleteUsageAndDummy:
    # V1 앱이 채우던 필드를 계산기가 완성한다. Email은 access token에
    # claim이 없어 빈 문자열로 키만 보장하고 실값은 앱이 채운다.

    CONDITION_CPR = {"mode": "training", "target": "adult", "training_type": "cpr"}

    def _usage_of(self, body_usage, condition=None):
        document = _build_hstm_document({"Usage": body_usage, "ResultSummary": {}}, RESULT, condition)
        return document["Usage"]

    def test_fills_v1_app_fields_when_absent(self):
        usage = self._usage_of({"hstreamId": "h-1"}, self.CONDITION_CPR)

        assert usage["Email"] == ""
        assert usage["Comment"] == "This is comment field"
        assert usage["Regional_Option"] == "usa"
        assert usage["Type"] == "CPR Training"
        assert isinstance(usage["validNum"], int)
        assert 1000000000 <= usage["validNum"] < 9999999999

    def test_keeps_app_provided_values(self):
        usage = self._usage_of(
            {"Email": "user@example.com", "Comment": "real comment", "Regional_Option": "kor", "validNum": 123},
            self.CONDITION_CPR,
        )

        assert usage["Email"] == "user@example.com"
        assert usage["Comment"] == "real comment"
        assert usage["Regional_Option"] == "kor"
        assert usage["validNum"] == 123

    def test_type_normalized_to_v1_strings(self):
        # V2 앱은 "CPR"/"Compression only"를 보낸다. 문서에는 V1 문자열로 출력한다.
        cases = {
            "cpr": "CPR Training",
            "compression_only": "Chest compression only",
            "ventilation_only": "Ventilation only",
        }
        for training_type, expected in cases.items():
            condition = {"mode": "training", "target": "adult", "training_type": training_type}
            usage = self._usage_of({"Type": "CPR"}, condition)
            assert usage["Type"] == expected

    def test_type_assessment_when_assess_mode(self):
        condition = {"mode": "assessment", "target": "adult", "training_type": "cpr"}

        usage = self._usage_of({"Type": "CPR"}, condition)

        assert usage["Type"] == "Assessment"

    def test_type_normalization_does_not_affect_judg_result_gate(self):
        # 판정 게이트는 앱 원값 Usage.Type을 본다. training 모드에서 앱이 Type을 안 보낸
        # 경우(원값 없음 = 게이트 True) 정규화가 판정을 뒤집지 않아야 한다.
        body = {"Usage": {}, "ResultSummary": {}}
        condition = {"mode": "training", "target": "adult", "training_type": "cpr"}

        document = _build_hstm_document(body, RESULT, condition)

        # 원값 Type 없음 → 게이트 True → 점수 기반 판정(Pass/Fail), N/A 아님
        assert document["ResultSummary"]["JudgResult"] in ("Pass", "Fail")
        # 문서의 Type은 V1 문자열로 완성된다
        assert document["Usage"]["Type"] == "CPR Training"

    def test_dummy_hardware_filled_when_empty(self):
        document = _build_hstm_document(
            {"Dummy": {"Model": "Adult", "Hardware": ""}, "ResultSummary": {}}, RESULT, None
        )

        assert document["Dummy"]["Hardware"] == "1.0"
        assert document["Dummy"]["Model"] == "Adult"

    def test_dummy_hardware_kept_when_provided(self):
        document = _build_hstm_document(
            {"Dummy": {"Hardware": "2.3"}, "ResultSummary": {}}, RESULT, None
        )

        assert document["Dummy"]["Hardware"] == "2.3"


class TestEndToEndConformance:
    def test_assembled_document_top_level_keys(self):
        body = {
            "CalculationService": {"Version": "1.1.301"},
            "Dummy": {"DeviceID": "62cc"},
            "Usage": {"hstreamId": "h-1"},
            "Custom": "",
            "Institution": {"inst_id": "i-1"},
            "Open_Skill": {"Passing_Score": "84"},
            "Organization": {"org_id": "o-1"},
            "ResultSummary": {},
        }

        document = _build_hstm_document(body, RESULT, None)

        expected_top_level = {
            "CalculationService", "Dummy", "Usage", "Custom", "Institution",
            "Open_Skill", "Organization", "ResultSummary", "ResultByCriteria",
            "ResultByCycle", "Certification", "Guide_prompts",
        }
        assert set(document.keys()) == expected_top_level

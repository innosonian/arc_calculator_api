"""Reference certification object and threshold precedence at the HTTP boundary."""
import json

import pytest

import lambda_handler
from tests._synth import WEAK_RAMP, comp_session, condition_json, cpr_session, multipart_event


@pytest.fixture(autouse=True)
def _lambda_env(monkeypatch):
    # stage=test → S3 선저장/차트 업로드 skip. 제출 Lambda env 미설정 → 제출 invoke skip(스펙 §4.1).
    monkeypatch.setenv("STAGE", "test")
    monkeypatch.delenv("ARC_SUBMIT_LAMBDA_NAME", raising=False)
    monkeypatch.delenv("SENTRY_DSN", raising=False)


def _post(parts):
    response = lambda_handler._run_trusted_calculation(multipart_event(parts), None)
    return response["statusCode"], json.loads(response["body"])


class TestPassingScoreReceived:
    def test_pass_when_overall_meets_received_passing_score(self):
        status, body = _post({
            "rawHexBPfile": comp_session(60),
            "condition": condition_json(training_type="compression_only"),
            "Custom": '{"CertificateAdult": true}',
            "Open_Skill": '{"Passing_Score": "0"}',
        })
        assert status == 200
        assert body["certification"] == {"Target": "adult"}

    def test_fail_when_overall_below_received_passing_score(self):
        status, body = _post({
            "rawHexBPfile": comp_session(60),
            "condition": condition_json(training_type="compression_only"),
            "Custom": '{"CertificateAdult": true}',
            "Open_Skill": '{"Passing_Score": "101"}',
        })
        assert status == 200
        assert body["certification"] == {"Target": "N/A"}

    def test_boundary_equal_score_is_pass(self):
        # 스펙 §5.5: Pass/Fail = overall >= passing_score (동점은 Pass).
        # 합성 만점 세션(overall 100) == Passing_Score 100 → Pass.
        status, body = _post({
            "rawHexBPfile": comp_session(60),
            "condition": condition_json(training_type="compression_only"),
            "Custom": '{"CertificateAdult": true}',
            "Open_Skill": '{"Passing_Score": "100"}',
        })
        assert status == 200
        assert body["cpr_score"]["total_score"]["overall"] == 100
        assert body["certification"] == {"Target": "adult"}


class TestPassingScoreFallback:
    def test_fallback_threshold_is_80(self):
        # 원본 lambda_handler.py:727-740 로직 그대로: Open_Skill → Custom → 80.
        assert lambda_handler._get_pass_threshold(None, None, "adult") == 80
        assert lambda_handler._get_pass_threshold(None, {"Passing_Score": "84"}, "adult") == 84
        assert lambda_handler._get_pass_threshold({"PassThreshold": "70"}, None, "adult") == 70
        assert lambda_handler._get_pass_threshold({"PassThreshold": "70", "PassThresholdChild": "75"}, None, "child") == 75
        # Open_Skill 값이 Custom보다 우선한다.
        assert lambda_handler._get_pass_threshold({"PassThreshold": "70"}, {"Passing_Score": "90"}, "adult") == 90

    def test_wire_fallback_pass_at_full_score(self):
        # Open_Skill 미전송 → 폴백 80. 만점 세션(100 >= 80) → Pass.
        status, body = _post({
            "rawHexBPfile": comp_session(60),
            "condition": condition_json(training_type="compression_only"),
            "Custom": '{"CertificateAdult": true}',
        })
        assert status == 200
        assert body["cpr_score"]["total_score"]["overall"] == 100
        assert body["certification"] == {"Target": "adult"}

    def test_wire_fallback_fail_below_80(self):
        # 얕은 압박 세션: depth 0점 → overall ≤ 70 < 80(폴백) → Fail.
        status, body = _post({
            "rawHexBPfile": comp_session(60, ramp=WEAK_RAMP),
            "condition": condition_json(training_type="compression_only"),
            "Custom": '{"CertificateAdult": true}',
        })
        assert status == 200
        assert body["cpr_score"]["total_score"]["overall"] < 80
        assert body["certification"] == {"Target": "N/A"}


class TestNullOverallCertification:
    def test_null_overall_is_fail_even_with_zero_threshold(self):
        # 성인 CPR 압박<90·호흡<6으로 overall null이면 Target은 N/A.
        # Passing_Score 0을 줘도 계산 불가 결과에 인증 대상을 부여하지 않는다.
        status, body = _post({
            "rawHexBPfile": cpr_session([(30, 2)]),
            "condition": condition_json(training_type="cpr"),
            "Custom": '{"CertificateAdult": true}',
            "Open_Skill": '{"Passing_Score": "0"}',
        })
        assert status == 200
        assert body["cpr_score"]["total_score"]["overall"] is None  # JSON null
        assert body["certification"] == {"Target": "N/A"}


class TestRequiredCertificateFlags:
    def test_no_requested_target_is_na_even_when_score_passes(self):
        status, body = _post({
            "rawHexBPfile": comp_session(60),
            "condition": condition_json(training_type="compression_only"),
        })
        assert status == 200
        assert body["cpr_score"]["total_score"]["overall"] == 100
        assert body["certification"] == {"Target": "N/A"}

    @pytest.mark.parametrize("target, custom, expected", [
        ("adult", {"CertificateAdult": True}, "adult"),
        ("child", {"CertificateChild": True}, "child"),
        ("infant", {"CertificateInfant": True}, "baby"),
        # Reference distinguishes the input flag Baby from the target infant.
        ("infant", {"CertificateBaby": True}, "N/A"),
        ("adult", {"CertificateChild": True}, "N/A"),
        ("adult", {}, "N/A"),
    ])
    def test_exact_target_mapping(self, target, custom, expected):
        result = {"cpr_score": {"total_score": {"overall": 100}}}
        assert lambda_handler._build_certification(
            result, {"target": target}, custom, None, None
        ) == {"Target": expected}

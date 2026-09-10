# 기존 HTTP 오류 보호와 복원된 입력 형식의 회귀 테스트.
# | 미지원/미지 guideline           | 400 {"type":"client_error","message":"This guideline is not supported."} (문구 고정)
# | rawHexBPfile 누락·빈 바이트     | 400 client_error, 정제 메시지
# | CPR 길이 오류·파싱 오류         | 400, 내부 예외 traceback성 메시지 비노출
# | 미지 target/training_type      | 400 client_error
# | 헤더 매직바이트 불일치           | 200 전부 0점 (현행 유지)
# | 내부 예외                       | 500 {"type":"server_error"} (현행 유지)
# | Content-Type 비 multipart      | 레거시 base64 폼 해석; JSON API는 아님
# 검토 반영 2026-09-05: O8 타입 불일치 400 케이스(TestTypeMismatch)와 isBase64Encoded=false 바이너리
# 회귀(TestWireFormat) 추가. 세부 매트릭스는 tests/test_handler_robustness.py.
import base64
import json

import pytest

import lambda_handler
from tests._synth import comp_session, condition_json, cpr_session, multipart_event

GUIDELINE_ERROR_MESSAGE = "This guideline is not supported."


@pytest.fixture(autouse=True)
def _lambda_env(monkeypatch):
    monkeypatch.setenv("STAGE", "test")
    monkeypatch.delenv("ARC_SUBMIT_LAMBDA_NAME", raising=False)
    monkeypatch.delenv("SENTRY_DSN", raising=False)


def _post(parts):
    response = lambda_handler._run_trusted_calculation(multipart_event(parts), None)
    return response["statusCode"], json.loads(response["body"])


def _assert_clean_client_error(status, body):
    assert status == 400
    assert body["type"] == "client_error"
    message = body.get("message") or ""
    assert message, body  # 정제 메시지가 있어야 한다
    assert "Traceback" not in message
    assert "\n" not in message  # traceback성 다행 메시지 비노출


class TestGuidelineErrors:
    def test_reference_non_arc_guidelines_are_accepted(self):
        # 전체 호환 요구에 따라 참고 프로젝트에서 계산 가능한 guideline을 수용한다.
        for guideline in ("AHA2020", "ERC2020", "STD2015"):
            status, body = _post({
                "rawHexBPfile": cpr_session([(30, 2)]),
                "condition": condition_json(guideline=guideline),
            })
            assert status == 200, (guideline, body)
            assert isinstance(body["cpr_score"], dict)

    def test_unknown_guideline_is_400_with_fixed_message(self):
        status, body = _post({
            "rawHexBPfile": cpr_session([(30, 2)]),
            "condition": condition_json(guideline="FOO2099"),
        })
        assert status == 400
        assert body == {"type": "client_error", "message": GUIDELINE_ERROR_MESSAGE}

    def test_missing_guideline_defaults_to_aha2020(self):
        assert lambda_handler.DEFAULT_CONDITION["guideline"] == "AHA2020"
        status, body = _post({
            "rawHexBPfile": comp_session(60),
            "condition": condition_json(training_type="compression_only", guideline=None),
        })
        assert status == 200
        assert body["cpr_score"]["total_score"]["overall"] is not None


class TestCprFileErrors:
    def test_missing_raw_file_part_is_400(self):
        status, body = _post({"condition": condition_json()})
        _assert_clean_client_error(status, body)

    def test_empty_raw_file_is_400(self):
        status, body = _post({"rawHexBPfile": b"", "condition": condition_json()})
        _assert_clean_client_error(status, body)

    def test_invalid_length_is_400_with_clean_message(self):
        # 28바이트 배수(꼬리 20바이트 허용) 위반 → 400. 내부 예외 문자열 노출 금지.
        status, body = _post({"rawHexBPfile": b"\xa8" + b"\x00" * 13, "condition": condition_json()})
        _assert_clean_client_error(status, body)

    def test_corrupt_base64_body_is_400_with_clean_message(self):
        # 스펙 §5.7 'CPR 길이 오류·b64/파싱 오류' 행의 b64 디코드 오류 케이스:
        # isBase64Encoded=True인데 body가 손상 base64(패딩 불가) → binascii.Error(ValueError)
        # → 400, 내부 예외 문자열 비노출(정제 메시지).
        event = multipart_event({"rawHexBPfile": cpr_session([(30, 2)]), "condition": condition_json()})
        event["body"] = "abcde"  # 유효 b64 문자이지만 길이 4k+1 — b64decode가 예외를 던진다
        response = lambda_handler._run_trusted_calculation(event, None)
        body = json.loads(response["body"])
        _assert_clean_client_error(response["statusCode"], body)
        # 내부 예외(binascii.Error)의 원문 노출 금지 — 정제 문구 계약(lambda_handler._MSG_SANITIZED_CLIENT_ERROR).
        assert body["message"] == "Invalid request data."


class TestConditionErrors:
    def test_unknown_target_is_400(self):
        status, body = _post({
            "rawHexBPfile": cpr_session([(30, 2)]),
            "condition": json.dumps({
                "mode": "training", "target": "alien", "training_type": "cpr",
                "guideline": "ARC2025", "cpr_cycle_type": "302", "is_2rescuers": False,
            }),
        })
        _assert_clean_client_error(status, body)

    def test_unknown_training_type_is_400(self):
        status, body = _post({
            "rawHexBPfile": cpr_session([(30, 2)]),
            "condition": json.dumps({
                "mode": "training", "target": "adult", "training_type": "swimming",
                "guideline": "ARC2025", "cpr_cycle_type": "302", "is_2rescuers": False,
            }),
        })
        _assert_clean_client_error(status, body)


class TestInternalError:
    def test_internal_exception_is_500_server_error(self, monkeypatch):
        # 스펙 §5.7: 내부 예외(비 ValueError) → 500 {"type":"server_error"} (현행 유지 —
        # 원본 hstm_v2 lambda_handler.py:132-133과 동일 골격/문구).
        def _boom(*_args, **_kwargs):
            raise RuntimeError("secret internal detail")

        monkeypatch.setattr(lambda_handler, "run_calculator", _boom)
        status, body = _post({"rawHexBPfile": cpr_session([(30, 2)]), "condition": condition_json()})
        assert status == 500
        assert body == {"type": "server_error", "message": "Internal server error"}
        # 내부 예외 메시지 비노출
        assert "secret internal detail" not in json.dumps(body)


class TestMagicByteMismatch:
    def test_wrong_magic_returns_200_with_zero_scores(self):
        # 스펙 §5.7: 헤더 매직바이트(0xA8) 불일치는 현행 유지 — 200에 "전부 0점".
        # overall만이 아니라 total_score의 모든 점수 필드가 0이어야 한다(§5.7 문언 고정).
        bad = b"\x00" + b"\x00" * 27  # 길이는 유효(28), 매직만 불일치
        status, body = _post({"rawHexBPfile": bad, "condition": condition_json()})
        assert status == 200
        total = body["cpr_score"]["total_score"]
        for key, value in total.items():
            if key == "judg_result":
                assert value == "N/A"
            else:
                assert value == 0, (key, value)
        assert total["overall"] == 0

    def test_wrong_magic_certification_is_fail(self):
        # 전부 0점(overall 0) → 폴백 80 미만 → Fail.
        bad = b"\x00" + b"\x00" * 27
        status, body = _post({"rawHexBPfile": bad, "condition": condition_json()})
        assert status == 200
        assert body["certification"] == {"Target": "N/A"}


class TestWireFormat:
    def test_plain_json_is_not_a_supported_json_endpoint(self):
        # 비multipart는 base64 폼으로 해석한다. 일반 JSON 객체를 요청 본문으로 받는 API가 아니다.
        event = {
            "httpMethod": "POST",
            "path": "/cpr-analysis",
            "headers": {"Content-Type": "application/json"},
            "isBase64Encoded": False,
            "body": "{}",
        }
        response = lambda_handler._run_trusted_calculation(event, None)
        body = json.loads(response["body"])
        assert response["statusCode"] == 400
        assert body["type"] == "client_error"

    def test_empty_legacy_form_without_content_type_is_400(self):
        event = {
            "httpMethod": "POST",
            "path": "/cpr-analysis",
            "headers": {},
            "isBase64Encoded": False,
            "body": "",
        }
        response = lambda_handler._run_trusted_calculation(event, None)
        body = json.loads(response["body"])
        assert response["statusCode"] == 400
        assert body["type"] == "client_error"

    def test_binary_body_without_base64_flag_is_400_sanitized(self):
        # 검토 반영 2026-09-05: 현재 동작 고정 — isBase64Encoded=false 로 바이너리 multipart 를 문자열로
        # 보내면 utf-8 재인코딩으로 패킷 길이가 어긋나 ValueError → 400 "Invalid request data.".
        event = multipart_event({"rawHexBPfile": cpr_session([(30, 2)]), "condition": condition_json()})
        event["isBase64Encoded"] = False
        event["body"] = base64.b64decode(event["body"]).decode("latin-1")
        response = lambda_handler._run_trusted_calculation(event, None)
        body = json.loads(response["body"])
        assert response["statusCode"] == 400
        assert body == {"type": "client_error", "message": lambda_handler._MSG_SANITIZED_CLIENT_ERROR}


class TestTypeMismatch:
    # 검토 반영 2026-09-05: O8 — 수정 전 500(TypeError/AttributeError/KeyError) 이던 타입 불일치 → 400.
    def test_non_list_vp_event_list_is_400(self):
        status, body = _post({
            "rawHexBPfile": cpr_session([(30, 2)]),
            "condition": condition_json(),
            "vp_event_list": '{"a": 1}',
        })
        assert status == 400
        assert body == {"type": "client_error", "message": lambda_handler._MSG_SANITIZED_CLIENT_ERROR}

    def test_vp_event_missing_timestamp_is_400(self):
        status, body = _post({
            "rawHexBPfile": cpr_session([(30, 2)]),
            "condition": condition_json(),
            "vp_event_list": '[{"event": 0}]',
        })
        assert status == 400
        assert body["message"] == lambda_handler._MSG_SANITIZED_CLIENT_ERROR

    def test_non_dict_organization_is_400(self):
        status, body = _post({
            "rawHexBPfile": cpr_session([(30, 2)]),
            "condition": condition_json(),
            "Organization": "[1]",
        })
        assert status == 400
        assert body["message"] == lambda_handler._MSG_SANITIZED_CLIENT_ERROR

    def test_non_string_regional_option_is_400(self):
        status, body = _post({
            "rawHexBPfile": cpr_session([(30, 2)]),
            "condition": condition_json(),
            "Usage": '{"Regional_Option": 5}',
        })
        assert status == 400
        assert body["message"] == lambda_handler._MSG_SANITIZED_CLIENT_ERROR

    def test_list_guideline_uses_fixed_guideline_message(self):
        status, body = _post({
            "rawHexBPfile": cpr_session([(30, 2)]),
            "condition": json.dumps({
                "mode": "training", "target": "adult", "training_type": "cpr",
                "guideline": ["ARC2025"], "cpr_cycle_type": "302", "is_2rescuers": False,
            }),
        })
        assert status == 400
        assert body == {"type": "client_error", "message": GUIDELINE_ERROR_MESSAGE}

    def test_falsy_non_list_vp_event_list_stays_200(self):
        # 수정 전 200 입력 불변 원칙: falsy 값은 vp_action.py 가 무시하므로 거부하지 않는다.
        status, body = _post({
            "rawHexBPfile": comp_session(60),
            "condition": condition_json(training_type="compression_only"),
            "vp_event_list": "{}",
        })
        assert status == 200
        assert body["cpr_score"]["total_score"]["overall"] == 100

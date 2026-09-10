# 검토 반영 2026-09-05: 핸들러 견고성 회귀 테스트(O5/O7/O8/P3/P4/P6).
# 근거 비교표(수정 전 스냅샷 vs 수정 후, STAGE=test 프로브 — 보고서 첨부):
#   - "수정 전 500(TypeError/AttributeError/KeyError)" 이던 타입 불일치 입력 → 400 + 정확한 메시지
#   - "수정 전 200" 이던 입력(falsy 값·float timestamp·last_timestamp 대체 키·bool/0.0 동치값 등) → 200 유지
#   - 수정 전 핸들러 밖으로 새던 예외(requestContext.identity None, BadDsn, 응답 직렬화 TypeError) → 200/500 계약
# 환경(STAGE=test·SENTRY_DSN 제거·boto3 가드)은 tests/conftest.py 가 자동 적용한다.
import json

import pytest

import lambda_handler
from tests._synth import comp_session, condition_json, cpr_session, multipart_event

INVALID = "Invalid request data."
CO = condition_json(training_type="compression_only")


def _post(parts, event_patch=None):
    event = multipart_event(parts)
    if event_patch:
        event_patch(event)
    response = lambda_handler._run_trusted_calculation(event, None)
    return response["statusCode"], json.loads(response["body"])


def _full_session_parts(**extra):
    parts = {"rawHexBPfile": comp_session(60), "condition": CO}
    parts.update(extra)
    return parts


def _assert_invalid(status, body):
    assert status == 400
    assert body == {"type": "client_error", "message": INVALID}


def _assert_ok(status, body, overall=100):
    assert status == 200, body
    assert body["cpr_score"]["total_score"]["overall"] == overall


class TestVpEventListTypes:
    # 수정 전 500 → 400
    @pytest.mark.parametrize(
        "vp",
        [
            '{"a":1}',  # truthy dict
            '"abc"',  # str
            "5",  # int
            "true",  # bool
            "[1]",  # 원소 int
            '["a"]',  # 원소 str
            "[[]]",  # 원소 list
            "[null]",  # 원소 null
            "[{}]",  # 원소 빈 dict(timestamp 없음)
            '[{"event":0}]',  # timestamp 누락
            '[{"event":0,"timestamp":"100"}]',  # str timestamp
            '[{"event":0,"timestamp":null}]',  # null timestamp
            '[{"event":0,"timestamp":[1]}]',  # list timestamp
            '[{"event":0,"last_timestamp":0}]',  # last_timestamp=0 → 폴백 timestamp 없음(정렬 키 None)
            '[{"timestamp":100}]',  # event 누락
            '[{"event":5,"timestamp":100}]',  # 미지 event
            '[{"event":-1,"timestamp":100}]',
            '[{"event":"0","timestamp":100}]',  # str event
            '[{"event":null,"timestamp":100}]',
            '[{"event":[0],"timestamp":100}]',
        ],
    )
    def test_crashing_shapes_are_400(self, vp):
        status, body = _post(_full_session_parts(vp_event_list=vp))
        _assert_invalid(status, body)

    # 수정 전 200 → 200 유지
    @pytest.mark.parametrize(
        "vp, overall",
        [
            ("[]", 100),
            ("{}", 100),  # falsy dict — vp_action.py `if not` 로 무시
            ('""', 100),
            ("0", 100),
            ("null", 100),
            ("false", 100),
            ("{not json", 100),  # 파서가 [] 로 폴백
            ('[{"event":0,"timestamp":100},{"event":1,"timestamp":2000}]', 100),
            ('[{"event":0,"timestamp":100.5},{"event":1,"timestamp":2000.5}]', 100),  # float timestamp
            ('[{"event":0,"last_timestamp":100},{"event":1,"last_timestamp":2000}]', 100),  # 대체 키만
            ('[{"event":0,"timestamp":1e999}]', 100),  # inf(float) — 비교 가능
            ('[{"event":true,"timestamp":100}]', 100),  # True == 1(END_COMP) 동치
            (
                '[{"event":0,"timestamp":10},{"event":1,"timestamp":20},{"event":10,"timestamp":30},'
                '{"event":11,"timestamp":40},{"event":20,"timestamp":50},{"event":21,"timestamp":60}]',
                100,
            ),
            # 아래 3건은 VP 구간이 세션 전체를 덮어 overall 0 — 값까지 수정 전과 동일해야 한다
            ('[{"event":0,"timestamp":0}]', 0),
            ('[{"event":0,"timestamp":true}]', 0),  # bool timestamp(int 하위)
            ('[{"event":0.0,"timestamp":100}]', 0),  # 0.0 == START_COMP 동치
            ('[{"event":0,"timestamp":100,"extra":{"x":1}}]', 0),  # 추가 키 무시
        ],
    )
    def test_previously_accepted_shapes_stay_200(self, vp, overall):
        status, body = _post(_full_session_parts(vp_event_list=vp))
        _assert_ok(status, body, overall=overall)

    def test_parse_complete_log_counts_only_lists(self, capsys):
        # O8: 비-list 는 vp_event_count=None 으로 기록되고(len() TypeError 없음) 이후 400.
        _post(_full_session_parts(vp_event_list="5"))
        logs = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
        parse_complete = [log for log in logs if log.get("message") == "parse_complete"]
        assert parse_complete and parse_complete[0]["vp_event_count"] is None


class TestOrganizationTypes:
    @pytest.mark.parametrize(
        "org",
        ["[1]", '"x"', "5", "true", '{"org_id":5}', '{"org_id":[1]}', '{"org_id":{"a":1}}', '{"org_id":true}', '{"org_id":1.5}'],
    )
    def test_crashing_shapes_are_400(self, org):
        status, body = _post(_full_session_parts(Organization=org))
        _assert_invalid(status, body)

    @pytest.mark.parametrize(
        "org",
        [
            "null", "{}", "[]", '""', "0", "false",  # falsy → main.py `or {}` 흡수
            '{"org_id":null}', '{"org_id":"abc"}', '{"org_id":""}',
            '{"org_id":0}', '{"org_id":[]}', '{"org_id":{}}', '{"org_id":false}',  # falsy org_id → org_prefix "" 흡수
            '{"org_id":"6f616b42-0ed8-571e-823f-ee4aca6b7ce9"}', '{"org_name":5}', "{not json",
        ],
    )
    def test_previously_accepted_shapes_stay_200(self, org):
        status, body = _post(_full_session_parts(Organization=org))
        _assert_ok(status, body)


class TestUsageTypes:
    @pytest.mark.parametrize(
        "usage",
        ['{"Regional_Option":5}', '{"Regional_Option":[1]}', '{"Regional_Option":{"a":1}}', '{"Regional_Option":true}', '{"Regional_Option":1.5}'],
    )
    def test_crashing_regional_option_is_400(self, usage):
        status, body = _post(_full_session_parts(Usage=usage))
        _assert_invalid(status, body)

    @pytest.mark.parametrize(
        "usage",
        [
            "null", "{}", "[]", '""', "0", "false",  # reference document의 falsy 기본값 보존
            '{"Regional_Option":null}', '{"Regional_Option":"british"}', '{"Regional_Option":"KOR"}',
            '{"Regional_Option":0}', '{"Regional_Option":""}', '{"Regional_Option":[]}',
            '{"Regional_Option":{}}', '{"Regional_Option":false}',  # falsy → _normalize_region "british"
        ],
    )
    def test_previously_accepted_shapes_stay_200(self, usage):
        status, body = _post(_full_session_parts(Usage=usage))
        _assert_ok(status, body)


    @pytest.mark.parametrize("usage", ["[1]", '"x"', "5", "true"])
    def test_document_usage_requires_get_compatible_object(self, usage):
        # Reference 문서 복원으로 새로 소비하는 잘못된 입력은 승인된 보호 경계에서 400 처리.
        status, body = _post(_full_session_parts(Usage=usage))
        _assert_invalid(status, body)


class TestConditionValueTypes:
    _BASE = {
        "mode": "training", "target": "adult", "training_type": "compression_only",
        "guideline": "ARC2025", "cpr_cycle_type": "302", "is_2rescuers": False,
    }

    @pytest.mark.parametrize(
        "key, message",
        [
            ("guideline", lambda_handler._MSG_UNSUPPORTED_GUIDELINE),
            ("target", lambda_handler._MSG_UNSUPPORTED_TARGET),
            ("training_type", lambda_handler._MSG_UNSUPPORTED_TRAINING_TYPE),
        ],
    )
    @pytest.mark.parametrize("value", [["ARC2025"], ["adult"], ["cpr"], {"a": 1}, 5, None, True, 1.5, ""])
    def test_non_string_values_use_existing_fixed_messages(self, key, message, value):
        # list/dict 는 수정 전 set 멤버십 TypeError→500, 나머지는 원래 400 — 모두 같은 고정 문구.
        condition = dict(self._BASE)
        condition[key] = value
        status, body = _post({"rawHexBPfile": comp_session(60), "condition": json.dumps(condition)})
        assert status == 400
        assert body == {"type": "client_error", "message": message}

    def test_validation_order_is_unchanged(self):
        # guideline → target → training_type → CPR 파일. 복수 위반 시 첫 항목 문구.
        condition = dict(self._BASE, guideline=["x"], target=["y"], training_type=["z"])
        status, body = _post({"condition": json.dumps(condition)})
        assert (status, body["message"]) == (400, lambda_handler._MSG_UNSUPPORTED_GUIDELINE)
        condition = dict(self._BASE, target=["y"], training_type=["z"])
        status, body = _post({"condition": json.dumps(condition)})
        assert (status, body["message"]) == (400, lambda_handler._MSG_UNSUPPORTED_TARGET)


class TestRequestContext:
    @pytest.mark.parametrize(
        "request_context",
        [{"identity": None}, {}, None, {"identity": "x"}, {"identity": {"sourceIp": "1.2.3.4"}}],
    )
    def test_request_context_variants_are_200(self, request_context):
        def _patch(event):
            event["requestContext"] = request_context

        status, body = _post(_full_session_parts(), _patch)
        _assert_ok(status, body)

    def test_missing_request_context_is_200(self):
        def _patch(event):
            event.pop("requestContext", None)

        status, body = _post(_full_session_parts(), _patch)
        _assert_ok(status, body)

    def test_source_ip_resolution(self):
        headers = {"X-Forwarded-For": "9.9.9.9, 8.8.8.8"}
        assert lambda_handler._source_ip({"requestContext": {"identity": {"sourceIp": "1.2.3.4"}}}, headers) == "1.2.3.4"
        assert lambda_handler._source_ip({"requestContext": {"identity": None}}, headers) == "9.9.9.9"
        assert lambda_handler._source_ip({}, {}) is None


class TestSentry:
    def test_invalid_dsn_logs_and_continues(self, monkeypatch, capsys):
        # O5: BadDsn 이 핸들러 밖으로 새지 않는다(수정 전 Lambda 자체 오류). 유효 DSN 네트워크 테스트는 두지 않는다.
        monkeypatch.setenv("SENTRY_DSN", "not-a-dsn")
        status, body = _post(_full_session_parts())
        _assert_ok(status, body)
        logs = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
        failed = [log for log in logs if log.get("message") == "sentry_init_failed"]
        assert failed, logs
        assert failed[0]["level"] == "error"
        assert failed[0]["error_type"] == "BadDsn"

    def test_blank_dsn_is_treated_as_unset(self, monkeypatch, capsys):
        monkeypatch.setenv("SENTRY_DSN", "   ")
        status, body = _post(_full_session_parts())
        _assert_ok(status, body)
        assert "sentry_init_failed" not in capsys.readouterr().out


class TestResponseSerialization:
    def test_unserializable_result_is_500_server_error(self, monkeypatch):
        # P4: run_calculator 가 set 을 포함한 결과를 돌려주면 json.dumps TypeError → 500 계약(수정 전 핸들러 밖 예외).
        def _fake(*_args, **_kwargs):
            return {"cpr_score": {"total_score": {"overall": 100}}, "bad": {1, 2}}

        monkeypatch.setattr(lambda_handler, "run_calculator", _fake)
        status, body = _post(_full_session_parts())
        assert status == 500
        assert body == {"type": "server_error", "message": "Internal server error"}


class TestClientErrorLogging:
    def test_400_log_includes_stacktrace(self, capsys):
        # P3: 400 분기 request_failed 로그에도 stacktrace(list) 가 실린다.
        _post({"condition": condition_json()})  # CPR 파일 누락 → 400
        logs = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
        failed = [log for log in logs if log.get("message") == "request_failed"]
        assert failed and failed[0]["level"] == "warning"
        assert isinstance(failed[0]["stacktrace"], list) and failed[0]["stacktrace"]

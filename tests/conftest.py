# 검토 반영 2026-09-05: P13 테스트 공용 환경 가드(tests/ 하위 전체에 자동 적용).
# - STAGE=test 강제 → S3 선저장/차트 업로드 생략. Sentry·제출 환경변수는 제거한다.
# - 실제 제출 경로는 제거되어 환경변수만으로 활성화되지 않는다.
# - boto3.client / util.uploader.client 를 pytest.fail 스텁으로 교체: 어떤 테스트가 stage 가드를 우회해 실제
#   AWS 클라이언트를 만들려 하면 즉시 실패한다. (가짜 자격증명 방식은 실네트워크로 나가므로 쓰지 않는다.)
#   pytest.fail 은 BaseException 계열(Failed)이라 코드의 `except Exception` 삼킴에 걸리지 않는다.
# - 각 테스트 파일의 기존 _lambda_env fixture 와 중복되어도 무해하다.
import boto3
import pytest

import util.uploader


def _forbid_aws_client(*_args, **_kwargs):
    pytest.fail("테스트에서 실제 AWS 클라이언트 생성 시도 — stage 가드 누락")


@pytest.fixture(autouse=True)
def _test_env_guard(monkeypatch):
    monkeypatch.setenv("STAGE", "test")
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    monkeypatch.delenv("ARC_SUBMIT_LAMBDA_NAME", raising=False)
    monkeypatch.delenv("HSTM_SUBMIT_LAMBDA_NAME", raising=False)
    monkeypatch.setattr(boto3, "client", _forbid_aws_client)
    monkeypatch.setattr(util.uploader, "client", _forbid_aws_client)
    yield

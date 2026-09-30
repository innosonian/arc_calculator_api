# 검토 반영 2026-09-05: P13 테스트 공용 환경 가드(tests/ 하위 전체에 자동 적용).
# - STAGE=test 강제 → S3 선저장/차트 업로드 생략. Sentry·제출 환경변수는 제거한다.
# - 실제 제출 경로는 제거되어 환경변수만으로 활성화되지 않는다.
# - boto3.client / util.uploader.client 를 pytest.fail 스텁으로 교체: 어떤 테스트가 stage 가드를 우회해 실제
#   AWS 클라이언트를 만들려 하면 즉시 실패한다. (가짜 자격증명 방식은 실네트워크로 나가므로 쓰지 않는다.)
#   pytest.fail 은 BaseException 계열(Failed)이라 코드의 `except Exception` 삼킴에 걸리지 않는다.
# - 2026-09-28 X4-05: CI 러너(scripts/run_actions_regression.py)와 같은 수준으로, 시험이 도는 동안
#   소켓 연결·이름 해석 감사 이벤트와 boto3 Session/botocore 클라이언트 생성·API 호출·HTTP 전송도 막는다.
#   tests/ 전체가 이미 그 러너의 차단 아래 통과하므로 결과는 달라지지 않는다. 다른 시험 폴더(실제
#   loopback 사용)에는 적용되지 않는다. 파일별로 중복되던 _lambda_env fixture 는 이 가드로 대체됐다.
import boto3
import boto3.session
import botocore.client
import botocore.httpsession
import botocore.session
import pytest

from tests.network_guard_support import offline_network
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
    # The Actions runner's SDK seams (run_actions_regression._install_sdk_guards).
    monkeypatch.setattr(boto3, "resource", _forbid_aws_client)
    monkeypatch.setattr(boto3.session.Session, "client", _forbid_aws_client)
    monkeypatch.setattr(boto3.session.Session, "resource", _forbid_aws_client)
    monkeypatch.setattr(botocore.session.Session, "create_client", _forbid_aws_client)
    monkeypatch.setattr(botocore.client.BaseClient, "_make_api_call", _forbid_aws_client)
    monkeypatch.setattr(botocore.httpsession.URLLib3Session, "send", _forbid_aws_client)
    with offline_network():
        yield

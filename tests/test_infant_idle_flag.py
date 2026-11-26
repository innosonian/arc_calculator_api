"""infant idle 플래그(16) 실데이터 통합 테스트.

infant 마네킨(Brayden Baby)은 압박 위치 코드의 상위 비트를 플래그로 설정해 보내는 경우가 있다.
16은 idle(누르지 않음), 17은 정위치, 18은 복부다. 이전 infant 규칙은 마스크 전에 0 여부를 먼저
판정해 16을 복부로 판정했다. 이 파일은 그 판정이 실제로 발생하던 2026-03 녹화 두 건을 파싱부터
지표 집계까지 전체 파이프라인으로 실행해, 복부 판정이 발생하지 않는지 확인한다.

- infant_cco_idle_flag.bin: 압박 전용 정상 수행 세션(압박 31회). 첫 압박의 표본이 깊이 0인 16 뒤에
  17이 오는 형태라 이전 규칙에서는 첫 압박이 복부(n_IncorrectStomach 1)가 됐다.
- infant_cpr_idle_flag_pause.bin: cpr 세션(압박 120회). 세션 첫 압박의 표본이 16뿐이고, 16번째 압박은
  17 사이에 긴 멈춤의 16이 섞여 있어 이전 규칙에서는 2회가 복부로 판정됐다.

두 세션 모두 실제 복부 코드(18)가 없으므로 모든 압박이 정위치여야 한다.
"""

from pathlib import Path

import pytest

from main import run_calculator
from services.http.schemas import ConditionType

DATASET = Path(__file__).resolve().parent / "dataset"
IDLE_FLAG = 0x10
POSITION_BYTE = 13
PACKET_BYTES = 28
RT_HEADER = 0xA8


def _position_codes(data: bytes) -> set[int]:
    return {
        data[i + POSITION_BYTE]
        for i in range(0, len(data) - len(data) % PACKET_BYTES, PACKET_BYTES)
        if data[i] == RT_HEADER
    }


@pytest.mark.parametrize(
    "file_name,training_type,compressions",
    [
        ("infant_cco_idle_flag.bin", "compression_only", 31),
        ("infant_cpr_idle_flag_pause.bin", "cpr", 120),
    ],
)
def test_idle_flag_is_not_judged_as_stomach(file_name, training_type, compressions):
    data = (DATASET / file_name).read_bytes()
    codes = _position_codes(data)
    # 픽스처 전제: idle 플래그가 포함되어 있고 실제 복부 코드(2, 18)는 없다.
    assert IDLE_FLAG in codes
    assert not codes & {2, 18}

    condition = ConditionType(
        mode="training",
        target="infant",
        training_type=training_type,
        guideline="AHA2020",
        cpr_cycle_type="302",
        is_2rescuers=False,
    )
    result = run_calculator(data, b"", condition, stage="test")
    metrics = result["metrics"]
    hand_position = metrics["HandPosition"]

    assert metrics["CompressionActionNumber"] == compressions
    assert hand_position["n_IncorrectStomach"] == 0
    assert hand_position["n_IncorrectLR"] == 0
    assert hand_position["n_Good"] == compressions
    assert result["cpr_score"]["total_score"]["score_hand_position"] == 100

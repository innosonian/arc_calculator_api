# 원본: hstm_v2 tests/test_action_boundary.py (ARC 각색 이식 — guideline ARC2020, 게이트 서술을
# ARC 환경에서 실행. 검출 로직·기대값은 원본 그대로)
"""훈련 종류상 버려질 액션이 반대편 액션을 삼키지 않는지 고정한다.

예전 generate_action_rtdata_list는 comp/vent를 항상 둘 다 검출한 뒤 append 직전에만
_is_valid_action으로 걸렀다. 그래서 버려질 액션이 검출되는 순간에도 그 패킷이 액션 경계로
소비되어 buffer와 last_vent_vol을 리셋했고, 남아야 할 반대편 액션이 1회 증발했다.

CPR 최소량 null 정책의 경계에서는 액션 1회 손실로 점수 산출 여부가 바뀔 수 있다.
현재 경계는 성인·소아 압박 90회, 영아 압박 45회, 공통 호흡 6회이다.
아래 전용 훈련의 60/12 샘플은 기존 검출 회귀 입력이며, CPR 정책의 기준값을 뜻하지 않는다.

여기서 고정하는 것은 '가능한 손실 경로'다. 원본 저장소의 실캡처 8건 전부에서 트리거
(압박 전이와 환기 하강이 같은 50ms 패킷에 정렬)는 0회 관측됐고, 이 수정은 모든
실데이터에서 no-op이다(카운트·점수·경과시간 전 항목 동일).

[이식 주의] 원본 테스트 파일은 원본 저장소에서 untracked(미커밋) 상태였고, 검출 게이팅
수정 4건은 원본 프로덕션 코드에 구현돼 있지 않다(원본 스위트에서도 동일 4건 red 확인).
스펙 §0(기능 동등 기본, §4·§5 외 변경 금지)에 따라 arc는 원본 코드 동작을 그대로
이식하므로, 해당 4건은 expectedFailure로 표시해 계약 문서로만 보존한다.
스펙 승인으로 게이팅 수정이 이식되면 unexpected success로 표면화된다.
"""

import unittest
from unittest import TestCase

from config.constants import ACTION_TYPE_COMP, ACTION_TYPE_VENT
from data_handlers.action_data import ActionDataPrepare
from data_handlers.data_parser import DataParser
from services.config import Config

PACKET_BYTES = 28
# 실측 파형 모양: 상승 → 피크 → 펌웨어가 한 패킷에 0으로 스냅(tests/dataset/vo_1.bin과 동일 거동)
BREATH_SHAPE = [(0, 0), (10, 12), (30, 35), (50, 52), (0, 0), (0, 0)]


def _packet(comp_count: int = 0, vent_raw: tuple[int, int] = (0, 0), timestamp: int = 0) -> bytes:
    packet = bytearray(PACKET_BYTES)
    packet[0] = 0xA8  # rtdata 헤더
    packet[12] = comp_count
    packet[14], packet[15] = vent_raw
    packet[20:28] = timestamp.to_bytes(8, "big")
    return bytes(packet)


def _count_actions(packets: list[bytes], training_type: str) -> tuple[int, int]:
    config = Config({
        "mode": "training", "target": "adult", "training_type": training_type,
        "guideline": "ARC2020", "cpr_cycle_type": "302", "is_2rescuers": False,
    })
    rtdata_list = DataParser().parse_cpr_bytes(b"".join(packets), config)
    for rtdata in rtdata_list:
        rtdata["is_aed_overlapped"] = False
    actions = ActionDataPrepare(config).get_action_list(rtdata_list)
    return (
        len([a for a in actions if a["action_type"] == ACTION_TYPE_COMP]),
        len([a for a in actions if a["action_type"] == ACTION_TYPE_VENT]),
    )


def _breaths(count: int, comp_count_at: dict[tuple[int, int], int] | None = None) -> list[bytes]:
    """호흡 count회. comp_count_at[(호흡 index, 패킷 index)] 로 특정 패킷의 Ccnt를 바꾼다."""
    comp_count_at = comp_count_at or {}
    packets = []
    timestamp = 0
    for breath_index in range(count):
        for packet_index, vent_raw in enumerate(BREATH_SHAPE):
            packets.append(
                _packet(
                    comp_count_at.get((breath_index, packet_index), 0),
                    vent_raw,
                    timestamp + packet_index * 50,
                )
            )
        timestamp += len(BREATH_SHAPE) * 50
    return packets


class TestVentilationOnlyIgnoresCompressionCount(TestCase):
    """ventilation_only에서 Ccnt 변화가 진행 중인 호흡을 삼키면 안 된다.

    앱은 ventilation_only에서 Ccnt를 아예 읽지 않고 볼륨 파형만 본다
    (OneRescuerRealtimeEngine.shouldProcessCompression). 계산기도 같아야 한다 —
    가슴에 손이 스쳐 Ccnt가 한 번 올라갔다고 실제 호흡 12회가 11회로 기록되면 안 된다.
    """

    def test_twelve_breaths_count_twelve(self):
        self.assertEqual((0, 12), _count_actions(_breaths(12), "ventilation_only"))

    @unittest.expectedFailure  # 원본 미구현(미커밋 수정) — 모듈 docstring [이식 주의] 참조
    def test_compression_count_change_mid_breath_does_not_swallow_it(self):
        # 8번째 호흡의 피크 패킷에서 Ccnt 0→1 (가슴 접촉). 수정 전에는 vent가 11로 떨어졌다.
        packets = _breaths(12, comp_count_at={(7, 3): 1})
        self.assertEqual((0, 12), _count_actions(packets, "ventilation_only"))

    def test_compression_count_change_between_breaths_is_also_ignored(self):
        packets = _breaths(12, comp_count_at={(4, 0): 1, (4, 5): 2})
        self.assertEqual((0, 12), _count_actions(packets, "ventilation_only"))


class TestCompressionOnlyIgnoresVentilationVolume(TestCase):
    """compression_only에서 환기량 노이즈가 압박을 삼키면 안 된다.

    성인 vent_vol_compensation=10이라 raw 바이트 1만 있어도 '환기량 있음'이 된다. 한 패킷에서
    comp/vent가 동시에 성립하면 삼항 연산자가 vent를 고르는데, cco에서는 그 vent가 버려지면서도
    last_comp_cnt는 이미 갱신돼 그 압박이 사라졌다.
    """

    def _compressions(self, count: int, vent_raw_at: dict[int, tuple[int, int]] | None = None) -> list[bytes]:
        vent_raw_at = vent_raw_at or {}
        # 압박 1회 = Ccnt가 1 오른 패킷 + 뒤따르는 패킷 하나
        packets = [_packet(0, (0, 0), 0)]
        for n in range(1, count + 1):
            packets.append(_packet(n, vent_raw_at.get(n, (0, 0)), n * 100))
            packets.append(_packet(n, (0, 0), n * 100 + 50))
        return packets

    def test_sixty_compressions_count_sixty(self):
        self.assertEqual((60, 0), _count_actions(self._compressions(60), "compression_only"))

    @unittest.expectedFailure  # 원본 미구현(미커밋 수정) — 모듈 docstring [이식 주의] 참조
    def test_ventilation_noise_does_not_swallow_a_compression(self):
        # 30번째 압박 패킷에 raw 1바이트(=10mL)의 환기 노이즈. 직전 패킷 대비 하강 + min<10 성립.
        packets = [_packet(0, (0, 0), 0), _packet(0, (1, 1), 50)]
        for n in range(1, 61):
            packets.append(_packet(n, (1, 0) if n == 30 else (0, 0), n * 100))
            packets.append(_packet(n, (0, 0), n * 100 + 50))
        self.assertEqual((60, 0), _count_actions(packets, "compression_only"))


class TestCompressionSeedIsFirstPacket(TestCase):
    """압박 카운트는 '업로드 파일 첫 패킷의 Ccnt' 기준 증분이다 — 파일에 없는 압박은 세지 않는다.

    앱이 0을 기준으로 삼아 파일에 없는 압박 1회를 크레딧하던 결함(iOS 2026-08-26 수정)의
    반대편 계약을 고정한다. 두 시드가 어긋나면 앱 60 / 계산기 59로 갈려 정책 경계가 선다.
    """

    def _from_ccnt(self, start: int, compressions: int) -> list[bytes]:
        packets = [_packet(start, (0, 0), 0)]
        for n in range(1, compressions + 1):
            packets.append(_packet(start + n, (0, 0), n * 100))
        return packets

    def test_counts_increments_after_first_packet(self):
        self.assertEqual((60, 0), _count_actions(self._from_ccnt(0, 60), "compression_only"))

    @unittest.expectedFailure  # 원본 미구현(미커밋 수정) — 모듈 docstring [이식 주의] 참조
    def test_pre_session_compression_is_not_credited(self):
        # 세션이 열리기 전(RT 전송 시작~세션 개시 사이)에 이미 1회 압박해 Ccnt=1로 시작한 경우.
        # 그 압박의 파형은 파일에 없으므로 세면 안 된다 — 60회를 더 해야 60이다.
        self.assertEqual((60, 0), _count_actions(self._from_ccnt(1, 60), "compression_only"))


class TestCprCountsBothActionTypes(TestCase):
    """cpr에서는 두 종류 다 유효하므로 검출 게이팅이 아무것도 바꾸지 않는다(무회귀)."""

    def test_cpr_counts_compressions_and_ventilations(self):
        packets = [_packet(0, (0, 0), 0)]
        timestamp = 0
        for _ in range(2):
            for n in range(1, 31):
                timestamp += 50
                packets.append(_packet(n, (0, 0), timestamp))
            for vent_raw in BREATH_SHAPE * 2:
                timestamp += 50
                packets.append(_packet(30, vent_raw, timestamp))
            packets.append(_packet(0, (0, 0), timestamp + 50))
        comp, vent = _count_actions(packets, "cpr")
        self.assertEqual(60, comp)
        self.assertEqual(4, vent)


# EOF에서 아직 하강 중인 마지막 호흡: 상승 후 볼륨이 10 미만으로 내려오지 못한 채 파일이 끝난다.
SLOW_TAIL_BREATH = [(0, 0), (10, 12), (30, 35), (50, 52), (48, 50), (44, 46)]


class TestTrailingBreathFlushVentOnly(TestCase):
    """EOF에서 하강 중인 마지막 호흡을 세는지 고정한다 (ventilation_only 한정).

    앱은 피크에서 30mL 떨어진 순간 호흡을 세고 0.35초 뒤 녹화를 끊지만, 폐가 수 초에 걸쳐
    수축하는 마네킹은 그 안에 볼륨<10에 도달하지 못한다(실측 2.1~2.6초). 원본 저장소의
    실세션 replay에서 vent-only 128건 중 60건이 이 경로로 11회로 세어져 있었고 전부
    '12회 미만' 오판 가능 케이스였다. 플러시 후 12→13 전이는 2건뿐이며(EOF 잔여 440mL =
    실제 호흡) 관대한 방향이라 안전하다.
    """

    def _with_slow_tail(self, full_breaths: int) -> list[bytes]:
        packets = []
        timestamp = 0
        for _ in range(full_breaths):
            for vent_raw in BREATH_SHAPE:
                packets.append(_packet(0, vent_raw, timestamp))
                timestamp += 50
        for vent_raw in SLOW_TAIL_BREATH:
            packets.append(_packet(0, vent_raw, timestamp))
            timestamp += 50
        return packets

    @unittest.expectedFailure  # 원본 미구현(미커밋 수정) — 모듈 docstring [이식 주의] 참조
    def test_trailing_breath_still_descending_at_eof_is_counted(self):
        # 완결 호흡 11 + 하강 중 호흡 1 → 12. 수정 전에는 11로 세어져 정책 경계가 섰다.
        self.assertEqual((0, 12), _count_actions(self._with_slow_tail(11), "ventilation_only"))

    def test_fully_deflated_last_breath_is_not_double_counted(self):
        # BREATH_SHAPE는 0으로 스냅해 끝난다 → last_vent_vol == 0 → 플러시 없음. 12는 12.
        self.assertEqual((0, 12), _count_actions(_breaths(12), "ventilation_only"))

    def test_trailing_noise_below_minimum_is_not_flushed(self):
        # EOF 잔여가 임계(10mL=raw 1) 미만이면 호흡 진행으로 보지 않는다.
        packets = _breaths(12) + [_packet(0, (0, 0), 99000)]
        self.assertEqual((0, 12), _count_actions(packets, "ventilation_only"))

    def test_flush_is_scoped_to_ventilation_only(self):
        # cpr에서는 플러시하지 않는다(사이클·채점 영향을 차단).
        self.assertEqual(11, _count_actions(self._with_slow_tail(11), "cpr")[1])

    def test_compression_only_never_flushes_vent(self):
        # cco에서 환기 검출 자체가 꺼져 있으므로(EOF 노이즈 포함) 플러시도 없다.
        packets = [_packet(0, (0, 0), 0)]
        packets += [_packet(n, (0, 0), n * 500) for n in range(1, 61)]
        packets.append(_packet(60, (5, 5), 31000))
        self.assertEqual((60, 0), _count_actions(packets, "compression_only"))

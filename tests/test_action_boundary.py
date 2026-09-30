"""Historical boundary inputs under the decisions in docs/DECISIONS.md D38-D43.

The four formerly expected failures retain their original names and bytes.
Their old expected/observed counts were F1 12/11, F2 60/59, F3 60/61 and
F4 12/11. Independent detection, first-packet baseline and two peak-relative
confirmations now justify the ordinary assertions below. The twelve-breath
fixture is not the product's eight-breath goal. Historical app/recording
statistics are not evidence from the current repository or this test run.

D138 (2026-09-30): the D42 expectations below are kept under the retained
adapters' option (``eof_single_confirmation=False``, arc-internal-detection-v4
and pending-v3); the current rule additionally recognizes a last breath whose
descent was observed in exactly one packet before the file ends, and one that
was cut with no descent packet but had risen at least the drop threshold.
"""

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


def _count_actions(packets: list[bytes], training_type: str, eof_single_confirmation: bool = True) -> tuple[int, int]:
    config = Config({
        "mode": "training", "target": "adult", "training_type": training_type,
        "guideline": "ARC2020", "cpr_cycle_type": "302", "is_2rescuers": False,
    })
    rtdata_list = DataParser().parse_cpr_bytes(b"".join(packets), config)
    for rtdata in rtdata_list:
        rtdata["is_aed_overlapped"] = False
    actions = ActionDataPrepare(config, eof_single_confirmation=eof_single_confirmation).get_action_list(rtdata_list)
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
    """D38: counter changes cannot cancel a pending ventilation."""

    def test_twelve_breaths_count_twelve(self):
        self.assertEqual((0, 12), _count_actions(_breaths(12), "ventilation_only"))

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

    def test_ventilation_noise_does_not_swallow_a_compression(self):
        # 30번째 압박 패킷에 raw 1바이트(=10mL)의 환기 노이즈. 직전 패킷 대비 하강 + min<10 성립.
        packets = [_packet(0, (0, 0), 0), _packet(0, (1, 1), 50)]
        for n in range(1, 61):
            packets.append(_packet(n, (1, 0) if n == 30 else (0, 0), n * 100))
            packets.append(_packet(n, (0, 0), n * 100 + 50))
        self.assertEqual((60, 0), _count_actions(packets, "compression_only"))


class TestCompressionSeedIsFirstPacket(TestCase):
    """D41: the app includes a baseline packet; earlier events are not inferred."""

    def _from_ccnt(self, start: int, compressions: int) -> list[bytes]:
        packets = [_packet(start, (0, 0), 0)]
        for n in range(1, compressions + 1):
            packets.append(_packet(start + n, (0, 0), n * 100))
        return packets

    def test_counts_increments_after_first_packet(self):
        self.assertEqual((60, 0), _count_actions(self._from_ccnt(0, 60), "compression_only"))

    def test_pre_session_compression_is_not_credited(self):
        # 세션이 열리기 전(RT 전송 시작~세션 개시 사이)에 이미 1회 압박해 Ccnt=1로 시작한 경우.
        # 그 압박의 파형은 파일에 없으므로 세면 안 된다 — 60회를 더 해야 60이다.
        self.assertEqual((60, 0), _count_actions(self._from_ccnt(1, 60), "compression_only"))


class TestCprCountsBothActionTypes(TestCase):
    """D38 applies to CPR as well as the single-skill modes."""

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


# EOF의 마지막 두 패킷이 각각 최고량보다 충분히 낮다. 0 도달 여부와 무관하게 확정된다.
SLOW_TAIL_BREATH = [(0, 0), (10, 12), (30, 35), (50, 52), (48, 50), (44, 46)]


class TestTrailingBreathFlushVentOnly(TestCase):
    """The historical tail is confirmed normally; D42 adds no EOF inference.

    Its packet maxima are raw52→50→46, or520→500→460mL. Both final
    packets are at least10mL below the same peak, independent of file length.
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

    # Both options (False = the retained adapters' D42 rule, True = D138).
    OPTIONS = (False, True)

    def test_trailing_breath_still_descending_at_eof_is_counted(self):
        # 완결 호흡 11 + 하강 중 호흡 1 → 12. 수정 전에는 11로 세어져 정책 경계가 섰다.
        for option in self.OPTIONS:
            self.assertEqual((0, 12), _count_actions(self._with_slow_tail(11), "ventilation_only", option))

    def test_fully_deflated_last_breath_is_not_double_counted(self):
        # Two zero packets confirm each breath once; EOF never adds another.
        for option in self.OPTIONS:
            self.assertEqual((0, 12), _count_actions(_breaths(12), "ventilation_only", option))

    def test_trailing_noise_below_minimum_is_not_flushed(self):
        # A later zero packet does not create a new candidate.
        packets = _breaths(12) + [_packet(0, (0, 0), 99000)]
        for option in self.OPTIONS:
            self.assertEqual((0, 12), _count_actions(packets, "ventilation_only", option))

    def test_same_confirmed_tail_is_counted_in_cpr(self):
        # D38/D39: the former11 expectation only reflected the old detector.
        # This input already has two confirmations, so this is not EOF recovery.
        for option in self.OPTIONS:
            self.assertEqual(12, _count_actions(self._with_slow_tail(11), "cpr", option)[1])

    def test_compression_only_never_flushes_vent(self):
        # cco에서 환기 검출 자체가 꺼져 있으므로(EOF 노이즈 포함) 플러시도 없다.
        packets = [_packet(0, (0, 0), 0)]
        packets += [_packet(n, (0, 0), n * 500) for n in range(1, 61)]
        packets.append(_packet(60, (5, 5), 31000))
        for option in self.OPTIONS:
            self.assertEqual((60, 0), _count_actions(packets, "compression_only", option))

    def test_tail_cut_after_one_low_packet_counts_only_under_the_current_rule(self):
        # The same slow tail recorded one packet shorter: raw 52 -> 50 (520 -> 500 mL,
        # one packet 20 mL below the peak) and then the file ends. D42 (retained
        # option) leaves it uncounted; D138 recognizes the observed descent.
        packets = self._with_slow_tail(11)[:-1]
        self.assertEqual((0, 11), _count_actions(packets, "ventilation_only", False))
        self.assertEqual((0, 12), _count_actions(packets, "ventilation_only", True))
        self.assertEqual(11, _count_actions(packets, "cpr", False)[1])
        self.assertEqual(12, _count_actions(packets, "cpr", True)[1])
        self.assertEqual((0, 0), _count_actions(packets, "compression_only", True))

    def test_tail_cut_at_its_peak_counts_only_under_the_current_rule(self):
        # Two packets shorter: the file ends at the 520 mL peak, no descent was recorded.
        # D138 (b): it rose 520 mL above its zero baseline, so the cut point is the peak.
        packets = self._with_slow_tail(11)[:-2]
        self.assertEqual((0, 11), _count_actions(packets, "ventilation_only", False))
        self.assertEqual((0, 12), _count_actions(packets, "ventilation_only", True))
        # Cut while still rising (120 -> 350 mL): counted as well; cut before any volume: not.
        self.assertEqual((0, 12), _count_actions(self._with_slow_tail(11)[:-3], "ventilation_only", True))
        self.assertEqual((0, 12), _count_actions(self._with_slow_tail(11)[:-4], "ventilation_only", True))
        self.assertEqual((0, 11), _count_actions(self._with_slow_tail(11)[:-5], "ventilation_only", True))
        for cut in (3, 4, 5):
            self.assertEqual((0, 11), _count_actions(self._with_slow_tail(11)[:-cut], "ventilation_only", False))

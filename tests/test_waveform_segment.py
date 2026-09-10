# 원본: hstm_v2 tests/test_waveform_segment.py (동일 이식 — 가이드라인 비의존)
from unittest import TestCase

from calculators.waveform import count_depth_peaks, segment_compressions


def _packets(samples_per_packet: list[list[int]]) -> list[list[int]]:
    return samples_per_packet


# 한 번의 압박(상승→마루→하강)을 3패킷으로 표현: [0...], [80...], [0...]
def _one_comp(peak=80) -> list[list[int]]:
    return [[0] * 10, [0, 20, 40, 60, peak, peak - 10, 60, 40, 20, 10], [5, 2, 0, 0, 0, 0, 0, 0, 0, 0]]


class TestSegmentCompressions(TestCase):
    """분절은 패킷별 max 파형의 진폭 zigzag(카운트와 동일), 값은 샘플 단위."""

    def test_empty(self):
        self.assertEqual([], segment_compressions([]))

    def test_single_compression_peak_and_recoil(self):
        events = segment_compressions(_one_comp(peak=80))
        self.assertEqual(1, len(events))
        self.assertEqual(80, events[0].peak_depth)
        self.assertEqual(0, events[0].recoil_depth)  # 완전 이완
        self.assertEqual(1, events[0].peak_packet_index)
        self.assertEqual(4, events[0].peak_sample_offset)

    def test_fifteen_clean_compressions(self):
        packets = []
        for _ in range(15):
            packets += _one_comp()
        events = segment_compressions(packets)
        self.assertEqual(15, len(events))
        self.assertEqual([80] * 15, [e.peak_depth for e in events])
        self.assertEqual([0] * 15, [e.recoil_depth for e in events])

    def test_boundary_marker_tail_is_not_an_event(self):
        # 15회 압박 뒤 깊이 0 마커 패킷이 붙어도 이벤트는 15개.
        packets = []
        for _ in range(15):
            packets += _one_comp()
        packets += [[0] * 10, [0] * 10]
        self.assertEqual(15, len(segment_compressions(packets)))

    def test_fall_tail_action_window_not_scored_as_extra_event(self):
        # 액션 경계가 어긋나 마지막 압박의 하강이 별도 윈도우(max 62)로 잘려 나가도,
        # 하강은 연속 파형이므로 새 이벤트가 되지 않는다. 깊이 62가 shallow 압박으로 채점되지 않는다.
        packets = []
        for _ in range(14):
            packets += _one_comp()
        packets += [[0] * 10, [0, 20, 40, 60, 80, 80, 76, 72, 68, 65]]  # 15번째 압박: 상승~하강 시작
        packets += [[62, 50, 30, 10, 0, 0, 0, 0, 0, 0]]  # 경계로 잘린 하강 꼬리 윈도우
        events = segment_compressions(packets)
        self.assertEqual(15, len(events))
        self.assertEqual(80, events[-1].peak_depth)
        self.assertEqual(0, events[-1].recoil_depth)

    def test_incomplete_recoil_recorded_as_residual_depth(self):
        # 이완이 50까지만 올라오는 압박 3회: 분리는 되고(진폭 50), recoil 잔여 깊이 50 기록.
        # 단, 마지막 압박은 구간이 50에서 끝나(이완 진행 중 절단) 미관측 → 0.
        packets = [[50], [100], [50], [100], [50], [100], [50]]
        events = segment_compressions(packets)
        self.assertEqual(3, len(events))
        self.assertEqual([100, 100, 100], [e.peak_depth for e in events])
        self.assertEqual([50, 50, 0], [e.recoil_depth for e in events])

    def test_last_event_recoil_not_penalized_when_fall_is_truncated(self):
        # 사이클의 comp 샘플이 마지막 압박의 하강 도중에 끝나는 실데이터 케이스
        # (하강 꼬리가 다음 환기 액션으로 잘림, 예: [..., 66, 58, 58, 58]).
        # 이완 완료 미관측이므로 잔여 깊이로 패널티를 주지 않는다.
        packets = []
        for _ in range(2):
            packets += _one_comp()
        packets += [[0, 20, 50, 76, 76, 74, 72, 70, 68, 66], [62, 60, 58, 58, 58, 58, 58, 58, 58, 58]]
        events = segment_compressions(packets)
        self.assertEqual(3, len(events))
        self.assertEqual(0, events[-1].recoil_depth)  # 58로 기록하면 가짜 '이완 불완전'
        self.assertEqual([0, 0], [e.recoil_depth for e in events[:2]])

    def test_last_event_recoil_kept_when_valley_observed(self):
        # 마지막 압박이라도 골(상승 반전)이 관측되면 잔여 깊이를 그대로 기록한다.
        packets = [[0], [80], [40], [55]]  # 80 → 40(골) → 55(되올라옴): 잔여 40 관측됨
        events = segment_compressions(packets)
        self.assertEqual(1, len(events))
        self.assertEqual(40, events[-1].recoil_depth)

    def test_last_compression_cut_at_peak_has_no_recoil_penalty(self):
        # 기록이 정점에서 끝나면 이완 미관측 → recoil 0(패널티 없음).
        packets = [[0], [80], [0], [0, 40, 80]]
        events = segment_compressions(packets)
        self.assertEqual(2, len(events))
        self.assertEqual(0, events[-1].recoil_depth)

    def test_peak_value_is_sample_precise_within_packet(self):
        # 분절은 패킷 max 기준이지만 peak 값·오프셋은 패킷 내 최대 샘플에서 나온다.
        packets = [[0] * 10, [10, 30, 55, 77, 71, 60, 44, 21, 8, 3], [0] * 10]
        events = segment_compressions(packets)
        self.assertEqual(1, len(events))
        self.assertEqual(77, events[0].peak_depth)
        self.assertEqual(3, events[0].peak_sample_offset)

    def test_recoil_is_residual_depth_between_peaks(self):
        # 정점(100) 이후 잔여 깊이가 48까지만 내려가는 불완전 이완: recoil 잔여 48 기록.
        # 다음 압박의 상승 구간 샘플은 recoil로 오인되지 않는다.
        rise_fall = [55, 70, 85, 95, 100, 92, 80, 70, 60, 52]
        partial_release = [48, 50, 52, 55, 58, 60, 62, 64, 66, 68]
        packets = [rise_fall, partial_release] * 3
        events = segment_compressions(packets)
        self.assertEqual(3, len(events))
        self.assertEqual([100, 100, 100], [e.peak_depth for e in events])
        self.assertEqual([48, 48, 48], [e.recoil_depth for e in events])

    def test_invariant_event_count_equals_count_depth_peaks(self):
        # 불변식: 이벤트 수 == 패킷별 max 파형의 count_depth_peaks.
        cases = [
            [],
            [[0] * 10],
            _one_comp(),
            _one_comp() * 5,
            [[50], [100], [50], [100], [50]],
            [[0], [20], [0], [20], [0]],  # 진폭 미달 노이즈
            [[0], [80], [58], [0]],  # 하강 edge dip
            [[0], [80], [0], [0, 40, 80]],  # 정점 절단
        ]
        for packets in cases:
            pmax = [max(p) for p in packets]
            self.assertEqual(
                count_depth_peaks(pmax),
                len(segment_compressions(packets)),
                f"불변식 위반: {packets}",
            )


def _wide_comp() -> list[list[int]]:
    # 폭 3패킷(150ms 이상)짜리 정상 압박: 상승-정점-하강이 모두 진폭 임계 위에 걸친다.
    return [
        [0, 0, 0, 0, 0, 10, 20, 30, 40, 50],
        [60, 80, 80, 70, 60, 50, 40, 35, 30, 28],
        [26, 20, 10, 5, 0, 0, 0, 0, 0, 0],
    ]


def _tap() -> list[list[int]]:
    # 폭 1패킷(50ms)짜리 짧은 탭: 진폭 임계는 넘지만 마네킨 카운트는 올리지 못하는 패턴.
    return [[0, 0, 0, 28, 26, 6, 0, 0, 0, 0]]


def _comp_and_tap_packets(n: int) -> list[list[int]]:
    packets = []
    for _ in range(n):
        packets += _wide_comp() + [[0] * 10] + _tap() + [[0] * 10]
    return packets


class TestDropTapEvents(TestCase):
    """탭 필터는 카운트 불일치 트리거가 걸린 경우에만 짧은 폭 이벤트를 걸러낸다."""

    def test_event_run_widths(self):
        from calculators.waveform import event_run_widths

        packets = _wide_comp() + [[0] * 10] + _tap()
        events = segment_compressions(packets)
        self.assertEqual([3, 1], event_run_widths(packets, events))

    def test_taps_dropped_when_counts_disagree(self):
        from calculators.waveform import drop_tap_events

        packets = _comp_and_tap_packets(8)
        events = segment_compressions(packets)
        self.assertEqual(16, len(events))  # 압박 8 + 탭 8
        kept = drop_tap_events(packets, events, count_increments=8)
        self.assertEqual(8, len(kept))
        self.assertTrue(all(e.peak_depth >= 80 for e in kept))  # 탭(28)은 전부 제거

    def test_taps_kept_when_counts_agree(self):
        from calculators.waveform import drop_tap_events

        packets = _comp_and_tap_packets(8)
        events = segment_compressions(packets)
        # 차이(16-12=4)가 트리거 최소치(5) 미만이면 보정하지 않는다.
        self.assertEqual(16, len(drop_tap_events(packets, events, count_increments=12)))

    def test_all_narrow_falls_back_to_original(self):
        from calculators.waveform import drop_tap_events

        packets = []
        for _ in range(6):
            packets += _tap() + [[0] * 10]
        events = segment_compressions(packets)
        # 전부 걸러지는 이상 케이스(합성 파형 등)에선 원본을 유지한다.
        self.assertEqual(6, len(drop_tap_events(packets, events, count_increments=0)))

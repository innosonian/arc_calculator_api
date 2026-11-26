from unittest import TestCase

from calculators.waveform import COMP_PEAK_AMP, CompressionSegmenter, compression_events, segment_compressions


def _packets(samples_per_packet: list[list[int]]) -> list[list[int]]:
    return samples_per_packet


# 한 번의 압박(상승→마루→하강)을 3패킷으로 표현: [0...], [80...], [0...]
def _one_comp(peak=80) -> list[list[int]]:
    return [[0] * 10, [0, 20, 40, 60, peak, peak - 10, 60, 40, 20, 10], [5, 2, 0, 0, 0, 0, 0, 0, 0, 0]]


class TestSegmentCompressions(TestCase):
    """분절은 패킷별 max 파형의 진폭 zigzag(카운트와 동일), 값은 샘플 단위."""

    def _segment(self, packets, min_amp=COMP_PEAK_AMP):
        segmenter = CompressionSegmenter(min_amp)
        peaks = []
        for pi, packet in enumerate(packets):
            peak = segmenter.feed(pi, max(packet) if packet else 0)
            if peak is not None:
                peaks.append(peak)
        peak = segmenter.finish()
        if peak is not None:
            peaks.append(peak)
        events = segment_compressions(packets, min_amp)
        self.assertEqual(peaks, segmenter.peak_packet_indexes)
        self.assertEqual(peaks, [event.peak_packet_index for event in events])
        self.assertEqual(events, compression_events(packets, peaks))
        return events

    def test_empty(self):
        self.assertEqual([], self._segment([]))

    def test_single_compression_peak_and_recoil(self):
        events = self._segment(_one_comp(peak=80))
        self.assertEqual(1, len(events))
        self.assertEqual(80, events[0].peak_depth)
        self.assertEqual(0, events[0].recoil_depth)  # 완전 이완
        self.assertEqual(1, events[0].peak_packet_index)
        self.assertEqual(4, events[0].peak_sample_offset)

    def test_fifteen_clean_compressions(self):
        packets = []
        for _ in range(15):
            packets += _one_comp()
        events = self._segment(packets)
        self.assertEqual(15, len(events))
        self.assertEqual([80] * 15, [e.peak_depth for e in events])
        self.assertEqual([0] * 15, [e.recoil_depth for e in events])

    def test_boundary_marker_tail_is_not_an_event(self):
        # 15회 압박 뒤 깊이 0 마커 패킷이 붙어도 이벤트는 15개.
        packets = []
        for _ in range(15):
            packets += _one_comp()
        packets += [[0] * 10, [0] * 10]
        self.assertEqual(15, len(self._segment(packets)))

    def test_fall_tail_action_window_not_scored_as_extra_event(self):
        # 액션 경계가 어긋나 마지막 압박의 하강이 별도 윈도우(max 62)로 잘려 나가도,
        # 하강은 연속 파형이므로 새 이벤트가 되지 않는다. 깊이 62가 shallow 압박으로 채점되지 않는다.
        packets = []
        for _ in range(14):
            packets += _one_comp()
        packets += [[0] * 10, [0, 20, 40, 60, 80, 80, 76, 72, 68, 65]]  # 15번째 압박: 상승~하강 시작
        packets += [[62, 50, 30, 10, 0, 0, 0, 0, 0, 0]]  # 경계로 잘린 하강 꼬리 윈도우
        events = self._segment(packets)
        self.assertEqual(15, len(events))
        self.assertEqual(80, events[-1].peak_depth)
        self.assertEqual(0, events[-1].recoil_depth)

    def test_incomplete_recoil_recorded_as_residual_depth(self):
        # 이완이 50까지만 올라오는 압박 3회: 분리는 되고(진폭 50), recoil 잔여 깊이 50 기록.
        # 단, 마지막 압박은 구간이 50에서 끝나(이완 진행 중 절단) 미관측 → 0.
        packets = [[50], [100], [50], [100], [50], [100], [50]]
        events = self._segment(packets)
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
        events = self._segment(packets)
        self.assertEqual(3, len(events))
        self.assertEqual(0, events[-1].recoil_depth)  # 58로 기록하면 가짜 '이완 불완전'
        self.assertEqual([0, 0], [e.recoil_depth for e in events[:2]])

    def test_last_event_recoil_kept_when_valley_observed(self):
        # 마지막 압박이라도 골(상승 반전)이 관측되면 잔여 깊이를 그대로 기록한다.
        packets = [[0], [80], [40], [55]]  # 80 → 40(골) → 55(되올라옴): 잔여 40 관측됨
        events = self._segment(packets)
        self.assertEqual(1, len(events))
        self.assertEqual(40, events[-1].recoil_depth)

    def test_last_compression_cut_at_peak_has_no_recoil_penalty(self):
        # 기록이 정점에서 끝나면 이완 미관측 → recoil 0(패널티 없음).
        packets = [[0], [80], [0], [0, 40, 80]]
        events = self._segment(packets)
        self.assertEqual(2, len(events))
        self.assertEqual(0, events[-1].recoil_depth)

    def test_peak_value_is_sample_precise_within_packet(self):
        # 분절은 패킷 max 기준이지만 peak 값·오프셋은 패킷 내 최대 샘플에서 나온다.
        packets = [[0] * 10, [10, 30, 55, 77, 71, 60, 44, 21, 8, 3], [0] * 10]
        events = self._segment(packets)
        self.assertEqual(1, len(events))
        self.assertEqual(77, events[0].peak_depth)
        self.assertEqual(3, events[0].peak_sample_offset)

    def test_recoil_is_residual_depth_between_peaks(self):
        # 정점(100) 이후 잔여 깊이가 48까지만 내려가는 불완전 이완: recoil 잔여 48 기록.
        # 다음 압박의 상승 구간 샘플은 recoil로 오인되지 않는다.
        rise_fall = [55, 70, 85, 95, 100, 92, 80, 70, 60, 52]
        partial_release = [48, 50, 52, 55, 58, 60, 62, 64, 66, 68]
        packets = [rise_fall, partial_release] * 3
        events = self._segment(packets)
        self.assertEqual(3, len(events))
        self.assertEqual([100, 100, 100], [e.peak_depth for e in events])
        self.assertEqual([48, 48, 48], [e.recoil_depth for e in events])

    def test_streaming_matches_batch_for_existing_corner_cases(self):
        # 기존 케이스를 feed/finish로 재생해 정점과 샘플 속성 모두 배치 결과와 대조한다.
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
            with self.subTest(packets=packets):
                self._segment(packets)

    def test_min_amp_argument_still_controls_detection(self):
        # 개정 3에서 28 raw 탭은 기본 진폭 30 미달이다. 명시적 25 인자는 이전처럼 1회다.
        packets = [[0], [28], [0]]
        self.assertEqual([], self._segment(packets))
        self.assertEqual(1, len(self._segment(packets, min_amp=25)))


class TestCompressionSegmenter(TestCase):
    def test_spec_vectors_ignore_all_three_counter_patterns(self):
        # count_record/peak_record는 §3 상태 전이를 손으로 추적한 오라클이다.
        cases = [
            ("hold_jitter", [0, 100, 99, 101, 100, 99, 100, 70], [(7, 3)]),
            ("hold_flat", [0, 100, 100, 100, 70], [(4, 1)]),
            ("partial_recoil_x3", [0, 100, 30, 100, 30, 100, 0], [(2, 1), (4, 3), (6, 5)]),
            ("shallow_pump", [10, 30] * 5 + [10], []),
            ("shallow_30_15", [30, 60] * 5 + [30], [(2, 1), (4, 3), (6, 5), (8, 7), (10, 9)]),
            ("start_pressed", [80, 80, 50], [(2, 0)]),
            ("end_rising_30", [0, 30], [(1, 1)]),
            ("end_rising_29", [0, 29], []),
            ("spike", [0, 120, 0], [(2, 1)]),
            ("amp_boundary_29", [0, 100, 71, 71], [(3, 1)]),
            ("amp_boundary_30", [0, 100, 70, 70], [(2, 1)]),
            ("two_rescuer_vp", [0, 100, 50, 110, 10, 100, 0], [(2, 1), (4, 3), (6, 5)]),
            ("infant_mask", [0, 100, 70], [(2, 1)]),
        ]
        for name, depths, expected in cases:
            for variant in ("zero", "every_record", "during_hold"):
                with self.subTest(case=name, counter=variant):
                    raw = bytearray()
                    for record, depth in enumerate(depths):
                        packet = bytearray(28)
                        packet[0] = 0xA8
                        packet[1:11] = bytes([depth] * 10)
                        if name == "spike" and record == 1:
                            packet[1:11] = bytes([0, 0, 0, 0, 0, 120, 0, 0, 0, 0])
                        packet[12] = (record if variant == "every_record" else
                                      max(0, record - 1) * 3 if variant == "during_hold" else 0)
                        packet[13] = 0x44 if name == "infant_mask" else 0
                        raw.extend(packet)
                    packets = [list(raw[i + 1:i + 11]) for i in range(0, len(raw), 28)]
                    segmenter = CompressionSegmenter()
                    confirmed = []
                    for record, packet in enumerate(packets):
                        peak = segmenter.feed(record, max(packet))
                        if peak is not None:
                            confirmed.append((record, peak))
                    peak = segmenter.finish()
                    if peak is not None:
                        confirmed.append((len(packets) - 1, peak))
                    self.assertEqual(expected, confirmed)
                    self.assertEqual([peak for _, peak in expected], segmenter.peak_packet_indexes)
                    events = segment_compressions(packets)
                    self.assertEqual([peak for _, peak in expected], [event.peak_packet_index for event in events])
                    if name == "spike":
                        # §8: 한 샘플 스파이크도 pmax 기준에서 1회이며 첫 최대 샘플은 offset 5다.
                        self.assertEqual(5, events[0].peak_sample_offset)

    def test_finish_does_not_emit_twice(self):
        segmenter = CompressionSegmenter()
        self.assertIsNone(segmenter.feed(0, 0))
        self.assertIsNone(segmenter.feed(1, 30))
        self.assertEqual(1, segmenter.finish())
        self.assertIsNone(segmenter.finish())
        self.assertEqual([1], segmenter.peak_packet_indexes)

    def test_empty_finish(self):
        segmenter = CompressionSegmenter()
        self.assertIsNone(segmenter.finish())
        self.assertEqual([], segmenter.peak_packet_indexes)

    def test_peak_ties_keep_first_record_and_first_sample(self):
        packets = [[0] * 10, [0, 80, 80] + [0] * 7, [80] * 10, [50] * 10]
        event = segment_compressions(packets)[0]
        self.assertEqual((1, 1), (event.peak_packet_index, event.peak_sample_offset))

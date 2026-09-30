"""Preserve historical counter/tail inputs, applying approved D39/D41/D42 and D138.

Opposing old expectations are corrected explicitly rather than discarding
their input. A one-zero-packet waveform is still unconfirmed in mid-stream
and, under the retained adapters' D42 option, at the end of the file; D138
(the current rule) recognizes the candidate that is still open when the file
ends (one observed low packet, or a rise of at least the drop threshold with
no descent recorded). Separate two-zero-packet waveforms cover confirmed
breaths.
"""
from unittest import TestCase

from config.constants import ACTION_TYPE_COMP, ACTION_TYPE_VENT, VENT_TAIL_MIN_PACKETS
from config.enums import Actor
from data_handlers.action_data import ActionDataPrepare
from services.config import Config
from services.http.schemas import ParsedData
from services.preparers import make_pre_action_list, prepare_data


def _rt(count=0, vol=0, ts=0, depth=0):
    # 액션 경계 검출이 읽는 필드만 의미 있는 값으로 채운 합성 패킷.
    return {
        "compression_depth": [depth] * 10,
        "compression_rate": 0,
        "compression_count": count,
        "hand_position": 0,
        "ventilation_volume": [vol, vol],
        "ventilation_speed": 0,
        "ventilation_count": 0,
        "packet_sequence_num": 0,
        "cycle_num": 0,
        "timestamp": ts,
        "actor": Actor.REAL_PERSON,
    }


def _comp_stream(counts):
    # 카운트 값의 나열 그대로 패킷을 만든다. 같은 값이 연속되면 같은 압박 안의 패킷이다.
    return [_rt(count=c, ts=i * 50, depth=40) for i, c in enumerate(counts)]


def _breath(n_up=6, vol=40):
    # Historical input: only one low packet, insufficient under D39/D42.
    return [vol] * n_up + [0]


def _confirmed_breath(n_up=6, vol=40):
    return [0] + [vol] * n_up + [0, 0]


def _vent_stream(volumes):
    return [_rt(vol=v, ts=i * 50) for i, v in enumerate(volumes)]


def _count(actions, action_type):
    return sum(1 for a in actions if a["action_type"] == action_type)


def _config(training_type="cpr"):
    return Config({
        "mode": "training", "target": "adult", "training_type": training_type,
        "guideline": "ARC2020", "cpr_cycle_type": "302", "is_2rescuers": False,
    })


class TestFirstCompressionRecovery(TestCase):
    """D41 supersedes first-positive-count recovery; input bytes are unchanged."""

    def setUp(self):
        self.prep = ActionDataPrepare(_config())

    def test_stream_starting_at_zero_is_unchanged(self):
        # 0에서 시작하면 카운트 전이 5회가 그대로 액션 5개
        actions = self.prep.generate_action_rtdata_list(_comp_stream([0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5]))
        self.assertEqual(5, _count(actions, ACTION_TYPE_COMP))

    def test_stream_starting_at_one_counts_only_four_observed_changes(self):
        # Original input formerly expected5. Baseline1 has four later changes.
        actions = self.prep.generate_action_rtdata_list(_comp_stream([1, 1, 2, 2, 3, 3, 4, 4, 5, 5]))
        self.assertEqual(4, _count(actions, ACTION_TYPE_COMP))

    def test_first_observed_action_retains_measurement_packets(self):
        # Original input formerly produced a synthetic first action at baseline.
        # One observed change remains, with its actual packet evidence.
        actions = self.prep.generate_action_rtdata_list(_comp_stream([1, 1, 2, 2]))
        first = [a for a in actions if a["action_type"] == ACTION_TYPE_COMP][0]
        self.assertEqual(1, _count(actions, ACTION_TYPE_COMP))
        self.assertTrue(first["rtdata_list"])
        self.assertEqual(1, first["rtdata_list"][0]["compression_count"])

    def test_counter_wraparound_still_counts_every_change(self):
        # 카운터가 순환(35 -> 1)해도 값이 바뀌는 횟수만큼 액션이 생긴다
        counts = [0] + [c for c in range(1, 36) for _ in (0, 1)] + [c for c in range(1, 26) for _ in (0, 1)]
        actions = self.prep.generate_action_rtdata_list(_comp_stream(counts))
        self.assertEqual(60, _count(actions, ACTION_TYPE_COMP))


class TestTruncatedLastVentilation(TestCase):
    """D42/D138: duration never substitutes for an observed confirmation."""

    def setUp(self):
        self.prep = ActionDataPrepare(_config())
        # The retained adapters' option (arc-internal-detection-v4 / pending-v3).
        self.retained = ActionDataPrepare(_config(), eof_single_confirmation=False)

    def test_historical_single_low_packet_breaths_are_unconfirmed(self):
        # Original input formerly expected2; each has only one confirmation.
        # Unchanged under the retained D42 option.
        actions = self.retained.generate_action_rtdata_list(_vent_stream(_breath() + _breath()))
        self.assertEqual(0, _count(actions, ACTION_TYPE_VENT))

    def test_single_low_packet_at_end_of_file_is_one_breath(self):
        # D138: the first isolated low is followed by a recovery (no pair), so
        # the whole stream is one open candidate; its last packet is the one
        # observed confirmation. One breath, not the two of the old length rule.
        actions = self.prep.generate_action_rtdata_list(_vent_stream(_breath() + _breath()))
        self.assertEqual(1, _count(actions, ACTION_TYPE_VENT))
        vent = [a for a in actions if a["action_type"] == ACTION_TYPE_VENT][0]
        self.assertEqual(list(range(14)), vent["_source_packet_indices"])
        self.assertEqual(13, vent["_event_packet"])

    def test_two_confirmations_count_each_complete_breath(self):
        actions = self.prep.generate_action_rtdata_list(_vent_stream(_confirmed_breath() * 2))
        self.assertEqual(2, _count(actions, ACTION_TYPE_VENT))

    def test_historical_long_open_tail_does_not_supply_confirmation(self):
        # Preserve former15-packet length boundary, but neither candidate is
        # confirmed. The old expected2 was length-based EOF inference.
        tail = [40] * (VENT_TAIL_MIN_PACKETS - 1)
        actions = self.retained.generate_action_rtdata_list(_vent_stream(_breath() + tail))
        self.assertEqual(0, _count(actions, ACTION_TYPE_VENT))
        # D138 (b): the stream is one open candidate that rose 40 mL above its zero
        # baseline; the current rule counts it once (its rise, not the tail length).
        actions = self.prep.generate_action_rtdata_list(_vent_stream(_breath() + tail))
        self.assertEqual(1, _count(actions, ACTION_TYPE_VENT))

    def test_historical_short_open_tail_does_not_supply_confirmation(self):
        # The unchanged first waveform is also incomplete; old expectation1
        # incorrectly treats its single zero as a new-policy confirmation.
        tail = [40] * (VENT_TAIL_MIN_PACKETS - 2)
        actions = self.retained.generate_action_rtdata_list(_vent_stream(_breath() + tail))
        self.assertEqual(0, _count(actions, ACTION_TYPE_VENT))
        actions = self.prep.generate_action_rtdata_list(_vent_stream(_breath() + tail))
        self.assertEqual(1, _count(actions, ACTION_TYPE_VENT))  # D138 (b), independent of the tail length
        actions = self.prep.generate_action_rtdata_list(_vent_stream(_breath() + [40]))
        self.assertEqual(1, _count(actions, ACTION_TYPE_VENT))

    def test_confirmed_breath_then_long_open_tail_is_a_second_breath_only_under_the_current_rule(self):
        volumes = _confirmed_breath() + [40] * (VENT_TAIL_MIN_PACKETS * 2)
        actions = self.retained.generate_action_rtdata_list(_vent_stream(volumes))
        self.assertEqual(1, _count(actions, ACTION_TYPE_VENT))
        # D138 (b): after the confirmed breath rearmed at zero, the tail is a new candidate
        # that rose 40 mL and was still open when the file ended: a second breath, once.
        actions = self.prep.generate_action_rtdata_list(_vent_stream(volumes))
        self.assertEqual(2, _count(actions, ACTION_TYPE_VENT))

    def test_compression_only_never_adds_tail_ventilation(self):
        # 압박 단독 훈련에서는 환기 액션 자체가 유효하지 않으므로 꼬리 보정도 없다
        prep = ActionDataPrepare(_config("compression_only"))
        tail = [40] * VENT_TAIL_MIN_PACKETS
        actions = prep.generate_action_rtdata_list(_vent_stream(_breath() + tail))
        self.assertEqual(0, _count(actions, ACTION_TYPE_VENT))


class TestAttemptCountThroughPipeline(TestCase):
    """파이프라인 끝의 횟수도 기준선 제외와 두 패킷 확정 결정을 따른다."""

    def _prepared(self, rtdata_list, training_type, **detection):
        config = _config(training_type)
        actions, aed = make_pre_action_list(config, ParsedData(rtdata_list=rtdata_list, aed_data_list=[]), [],
                                            **detection)
        return prepare_data(actions, aed, config)

    def test_sixty_counter_values_starting_at_one_have_fifty_nine_changes(self):
        # Same input formerly expected60. D41 counts only59 observed changes;
        # it cannot satisfy the product goal60 by crediting the baseline.
        counts = [c for c in range(1, 61) for _ in range(10)]
        prepared = self._prepared(_comp_stream(counts), "compression_only")
        self.assertEqual(59, prepared["comp_count"])

    def test_ten_historical_single_low_packet_breaths_are_unconfirmed(self):
        # Keep all original samples; repeated isolated lows are not consecutive.
        # Unchanged under the retained D42 option.
        volumes = []
        for _ in range(10):
            volumes += _breath()
        prepared = self._prepared(_vent_stream(volumes), "ventilation_only", eof_single_confirmation=False)
        self.assertEqual(0, prepared["vent_count"])

    def test_ten_single_low_packet_breaths_end_with_one_observed_descent(self):
        # D138: only the low packet that ends the file is an observed descent of
        # the still-open candidate; the nine mid-stream lows stay unconfirmed.
        volumes = []
        for _ in range(10):
            volumes += _breath()
        prepared = self._prepared(_vent_stream(volumes), "ventilation_only")
        self.assertEqual(1, prepared["vent_count"])

    def test_ten_separately_confirmed_breaths_count_ten(self):
        prepared = self._prepared(_vent_stream(_confirmed_breath() * 10), "ventilation_only")
        self.assertEqual(10, prepared["vent_count"])

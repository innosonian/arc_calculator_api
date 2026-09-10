# 원본: hstm_v2 tests/test_action_boundary_recovery.py (ARC 각색 이식 — guideline만 ARC2020으로 교체)
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
    # 임계 이상으로 올라간 패킷 n_up개 뒤에 임계 아래로 내려오는 패킷 1개(여기서 확정된다).
    return [vol] * n_up + [0]


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
    """스트림 첫 패킷에 이미 카운트가 올라가 있으면 그 압박도 액션이 되어야 한다."""

    def setUp(self):
        self.prep = ActionDataPrepare(_config())

    def test_stream_starting_at_zero_is_unchanged(self):
        # 0에서 시작하면 카운트 전이 5회가 그대로 액션 5개
        actions = self.prep.generate_action_rtdata_list(_comp_stream([0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5]))
        self.assertEqual(5, _count(actions, ACTION_TYPE_COMP))

    def test_stream_starting_at_one_recovers_first_compression(self):
        # 첫 패킷이 이미 1이면 0 -> 1 전이가 없어도 압박 5회로 센다
        actions = self.prep.generate_action_rtdata_list(_comp_stream([1, 1, 2, 2, 3, 3, 4, 4, 5, 5]))
        self.assertEqual(5, _count(actions, ACTION_TYPE_COMP))

    def test_recovered_action_has_packet(self):
        # 첫 패킷에서 즉시 확정된 액션은 버퍼가 비어 있으므로 그 패킷을 담아야 한다
        actions = self.prep.generate_action_rtdata_list(_comp_stream([1, 1, 2, 2]))
        first = [a for a in actions if a["action_type"] == ACTION_TYPE_COMP][0]
        self.assertEqual(1, len(first["rtdata_list"]))
        self.assertEqual(1, first["rtdata_list"][0]["compression_count"])

    def test_counter_wraparound_still_counts_every_change(self):
        # 카운터가 순환(35 -> 1)해도 값이 바뀌는 횟수만큼 액션이 생긴다
        counts = [0] + [c for c in range(1, 36) for _ in (0, 1)] + [c for c in range(1, 26) for _ in (0, 1)]
        actions = self.prep.generate_action_rtdata_list(_comp_stream(counts))
        self.assertEqual(60, _count(actions, ACTION_TYPE_COMP))


class TestTruncatedLastVentilation(TestCase):
    """볼륨이 임계 이상인 채로 스트림이 끝나면, 관측이 충분히 길 때만 마지막 호흡을 인정한다."""

    def setUp(self):
        self.prep = ActionDataPrepare(_config())

    def test_fully_descended_stream_is_unchanged(self):
        # 정상 하강으로 끝난 호흡 2개는 그대로 2개
        actions = self.prep.generate_action_rtdata_list(_vent_stream(_breath() + _breath()))
        self.assertEqual(2, _count(actions, ACTION_TYPE_VENT))

    # 버퍼는 직전 호흡을 확정한 하강 패킷 1개로 시작하므로, 꼬리 길이 + 1 이 관측 길이다.

    def test_long_open_tail_is_counted(self):
        # 완료된 호흡 1개 뒤에 하강 없이 끊긴 호흡(관측 길이가 임계 이상)은 1개 더 센다
        tail = [40] * (VENT_TAIL_MIN_PACKETS - 1)
        actions = self.prep.generate_action_rtdata_list(_vent_stream(_breath() + tail))
        self.assertEqual(2, _count(actions, ACTION_TYPE_VENT))

    def test_short_open_tail_is_ignored(self):
        # 관측 길이가 임계보다 짧은 꼬리는 직전 호흡의 잔여 흔들림으로 보고 세지 않는다
        tail = [40] * (VENT_TAIL_MIN_PACKETS - 2)
        actions = self.prep.generate_action_rtdata_list(_vent_stream(_breath() + tail))
        self.assertEqual(1, _count(actions, ACTION_TYPE_VENT))

    def test_compression_only_never_adds_tail_ventilation(self):
        # 압박 단독 훈련에서는 환기 액션 자체가 유효하지 않으므로 꼬리 보정도 없다
        prep = ActionDataPrepare(_config("compression_only"))
        tail = [40] * VENT_TAIL_MIN_PACKETS
        actions = prep.generate_action_rtdata_list(_vent_stream(_breath() + tail))
        self.assertEqual(0, _count(actions, ACTION_TYPE_VENT))


class TestAttemptCountThroughPipeline(TestCase):
    """정책(스펙 §5.3)이 읽는 comp_count / vent_count가 파이프라인 끝에서 복원된 값이어야 한다."""

    def _prepared(self, rtdata_list, training_type):
        config = _config(training_type)
        actions, aed = make_pre_action_list(config, ParsedData(rtdata_list=rtdata_list, aed_data_list=[]), [])
        return prepare_data(actions, aed, config)

    def test_sixty_compressions_starting_at_one_count_sixty(self):
        # 첫 패킷이 1인 60회 압박 스트림은 60으로 집계되어 최소 시도(60)를 충족해야 한다
        counts = [c for c in range(1, 61) for _ in range(10)]
        prepared = self._prepared(_comp_stream(counts), "compression_only")
        self.assertEqual(60, prepared["comp_count"])

    def test_ten_complete_breaths_stay_ten(self):
        # 실제로 10회만 불고 정상 종료한 세션은 보정 후에도 10이어야 한다
        volumes = []
        for _ in range(10):
            volumes += _breath()
        prepared = self._prepared(_vent_stream(volumes), "ventilation_only")
        self.assertEqual(10, prepared["vent_count"])

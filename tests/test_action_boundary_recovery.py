from unittest import TestCase

from config.constants import ACTION_TYPE_COMP, ACTION_TYPE_VENT, VENT_TAIL_MIN_PACKETS
from config.enums import Actor
from data_handlers.action_data import ActionDataPrepare
from services.config import Config
from services.http.schemas import ParsedData
from services.preparers import make_pre_action_list, prepare_data


def _rt(count=0, vol=0, ts=0, depth=0, aed=False):
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
        "is_aed_overlapped": aed,
    }


def _comp_stream(depths, counts=None):
    # DEPTH_V1: 실제 상승·하강을 명시한다. Ccnt는 깊이와 독립된 교란 입력이다.
    counts = counts if counts is not None else [0] * len(depths)
    return [_rt(count=c, ts=1000 + i * 50, depth=d) for i, (d, c) in enumerate(zip(depths, counts))]


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
        "guideline": "AHA2020", "cpr_cycle_type": "302", "is_2rescuers": False,
    })


class TestFirstCompressionRecovery(TestCase):
    """첫 Record가 이미 눌린 상태여도 30 raw 하강에서 압박을 확정한다."""

    def setUp(self):
        self.prep = ActionDataPrepare(_config())

    def test_stream_starting_at_zero_is_unchanged(self):
        # Ccnt가 0으로 고정되어도 깊이 0→80→0을 다섯 번 그리면 5회다.
        actions = self.prep.generate_action_rtdata_list(_comp_stream([0, 80, 0] * 5))
        self.assertEqual(5, _count(actions, ACTION_TYPE_COMP))

    def test_stream_starting_pressed_recovers_first_compression(self):
        # 첫 깊이 80의 하강도 센다. firmware count의 초기값 1에는 의존하지 않는다.
        actions = self.prep.generate_action_rtdata_list(_comp_stream([80, 0] * 5, [1] * 10))
        self.assertEqual(5, _count(actions, ACTION_TYPE_COMP))

    def test_recovered_action_excludes_confirmation_packet(self):
        # 사용자 후속 결정: [직전 경계, 이번 경계)이므로 Record 1 확정 패킷은 다음 버퍼 소유다.
        actions = self.prep.generate_action_rtdata_list(_comp_stream([80, 0, 80, 0]))
        first = [a for a in actions if a["action_type"] == ACTION_TYPE_COMP][0]
        self.assertEqual(1, len(first["rtdata_list"]))
        self.assertEqual([80], [max(r["compression_depth"]) for r in first["rtdata_list"]])
        self.assertEqual([0], [r["record_index"] for r in first["rtdata_list"]])
        self.assertEqual(1, first["count_record"])

    def test_counter_wraparound_does_not_change_depth_count(self):
        # Ccnt가 매 Record 증가·순환해도 실제 파형의 60회만 센다.
        depths = [0, 80, 0] * 60
        counts = [(i % 35) + 1 for i in range(len(depths))]
        actions = self.prep.generate_action_rtdata_list(_comp_stream(depths, counts))
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
    """게이트가 읽는 comp_count / vent_count가 파이프라인 끝에서 복원된 값이어야 한다."""

    def _prepared(self, rtdata_list, training_type):
        config = _config(training_type)
        actions, aed = make_pre_action_list(config, ParsedData(rtdata_list=rtdata_list, aed_data_list=[]), [])
        return prepare_data(actions, aed, config)

    def test_sixty_compressions_starting_pressed_count_sixty(self):
        # Ccnt 0인 첫 눌림부터 60번 하강하면 게이트 입력은 60이다.
        prepared = self._prepared(_comp_stream([80, 0] * 60), "compression_only")
        self.assertEqual(60, prepared["comp_count"])

    def test_ten_complete_breaths_stay_ten(self):
        # 실제로 10회만 불고 정상 종료한 세션은 보정 후에도 10이어야 한다
        volumes = []
        for _ in range(10):
            volumes += _breath()
        prepared = self._prepared(_vent_stream(volumes), "ventilation_only")
        self.assertEqual(10, prepared["vent_count"])

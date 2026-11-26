"""압박 위치 표본 재구성 테스트.

액션 버퍼는 직전 확정 Record부터 이번 30 raw 하강 Record 직전까지다. 압박 N의 이완 구간은
액션 N+1 버퍼의 선두에 포함될 수 있다. 압박 액션의 hand_position은 자기 버퍼의 마지막
압박 구간과 다음 버퍼 선두의 이완 구간을 합친 이번 압박의 표본이어야 한다.
"""

from unittest import TestCase

from config.constants import ACTION_TYPE_COMP
from config.enums import Actor
from data_handlers.action_data import ActionDataPrepare
from services.config import Config
from services.http.schemas import ParsedData
from services.preparers import make_pre_action_list


def _rt(count=0, pos=0, vol=0, ts=0, depth=40, actor=Actor.REAL_PERSON):
    return {
        "compression_depth": [depth] * 10,
        "compression_rate": 0,
        "compression_count": count,
        "hand_position": pos,
        "ventilation_volume": [vol, vol],
        "ventilation_speed": 0,
        "ventilation_count": 0,
        "packet_sequence_num": 0,
        "cycle_num": 0,
        "timestamp": ts,
        "actor": actor,
    }


def _stream(pairs, actors=None):
    # (깊이 raw, 위치 코드)로 실제 파형을 명시한다. Ccnt는 고정 0이다.
    actors = actors or [Actor.REAL_PERSON] * len(pairs)
    return [_rt(depth=d, pos=p, ts=1000 + i * 50, actor=a) for i, ((d, p), a) in enumerate(zip(pairs, actors))]


def _config(is_2rescuers=False):
    return Config(
        {
            "mode": "training",
            "target": "adult",
            "training_type": "cpr",
            "guideline": "AHA2020",
            "cpr_cycle_type": "302",
            "is_2rescuers": is_2rescuers,
        }
    )


def _comps(rtdata_list, is_2rescuers=False):
    actions, _ = make_pre_action_list(
        _config(is_2rescuers), ParsedData(rtdata_list=rtdata_list, aed_data_list=[]), []
    )
    return [a for a in actions if a["action_type"] == ACTION_TYPE_COMP]


# 100→70 하강으로 Record 4·12에서 확정한다. 좌우 코드는 각 확정 뒤 이완 꼬리에 있다.
TWO_COMPRESSIONS = [
    (0, 0), (0, 0), (40, 1), (100, 1), (70, 1),
    (40, 1), (10, 8), (0, 0), (0, 0), (40, 1), (100, 1), (100, 1),
    (70, 1), (10, 4), (0, 0), (0, 0),
]


class TestReleaseCodeAttribution(TestCase):
    def test_release_codes_belong_to_the_compression_that_produced_them(self):
        comps = _comps(_stream(TWO_COMPRESSIONS))
        self.assertEqual(2, len(comps))
        self.assertEqual([1, 1, 1, 1, 8], comps[0]["hand_position"])
        self.assertEqual([1, 1, 1, 1, 4], comps[1]["hand_position"])

    def test_previous_release_codes_are_not_carried_into_the_next_compression(self):
        comps = _comps(_stream(TWO_COMPRESSIONS))
        self.assertNotIn(8, comps[1]["hand_position"])

    def test_last_compression_of_session_keeps_its_release_codes(self):
        # 마지막 압박의 이완 구간은 LAST 버퍼 선두에 포함된다.
        comps = _comps(_stream(TWO_COMPRESSIONS))
        self.assertIn(4, comps[-1]["hand_position"])

    def test_compression_before_ventilation_keeps_its_release_codes(self):
        # 압박 2의 이완 뒤에 환기가 이어지면 이완 구간은 VENT 버퍼 선두에 포함된다.
        rtdata = _stream(TWO_COMPRESSIONS + [(0, 0)] * 4)
        for vol in [40] * 6 + [0]:
            rtdata.append(_rt(count=2, vol=vol, depth=0))
        rtdata += _stream([(0, 0)] * 3)
        for i, rt in enumerate(rtdata):
            rt["timestamp"] = i * 50
        comps = _comps(rtdata)
        self.assertEqual([1, 1, 1, 1, 4], comps[1]["hand_position"])

    def test_release_is_not_attributed_when_next_buffer_has_no_zero(self):
        # 다음 버퍼에 0이 없으면 이완 구간과 다음 하강 구간을 구분할 수 없으므로 다음 압박의 표본으로 유지한다.
        pairs = [
            (0, 0), (40, 1), (100, 1), (90, 1),
            (70, 1), (10, 8), (40, 1), (100, 1),
            (90, 1), (70, 0), (0, 0),
        ]
        comps = _comps(_stream(pairs))
        self.assertEqual([1, 1, 1], comps[0]["hand_position"])
        self.assertEqual([1, 8, 1, 1, 1], comps[1]["hand_position"])

    def test_release_finished_inside_own_buffer_is_kept_and_next_head_is_not_taken(self):
        # 위치 코드가 먼저 0으로 돌아온 뒤 30 raw 하강하면 자기 창의 이완만 쓴다.
        pairs = [
            (0, 0), (40, 1), (100, 1), (90, 8), (80, 0), (70, 0),
            (40, 1), (100, 1), (90, 4), (80, 0), (70, 0),
            (0, 1), (0, 0),
        ]
        comps = _comps(_stream(pairs))
        self.assertEqual([1, 1, 8], comps[0]["hand_position"])
        self.assertEqual([1, 1, 4], comps[1]["hand_position"])


class TestTwoRescuerAttribution(TestCase):
    def test_virtual_partner_release_codes_do_not_reach_real_person_compression(self):
        # 압박 1은 가상 파트너, 압박 2는 실제 사용자다. 파트너의 이완 코드 8은 파트너 액션의 표본에만 포함되어야 한다.
        vp, rp = Actor.VIRTUAL_PARTNER, Actor.REAL_PERSON
        actors = [vp] * 7 + [rp] * 9
        comps = _comps(_stream(TWO_COMPRESSIONS, actors), is_2rescuers=True)
        self.assertEqual(vp, comps[0]["actor"])
        self.assertEqual(rp, comps[1]["actor"])
        self.assertIn(8, comps[0]["hand_position"])
        self.assertNotIn(8, comps[1]["hand_position"])


class TestOwnHandPositionEdgeCases(TestCase):
    def test_all_zero_own_buffer_yields_empty_sample(self):
        # 자기 버퍼가 전부 0이면 압박 구간이 없고 경계를 넘는 압박도 없으므로 빈 표본이다.
        self.assertEqual([], ActionDataPrepare._own_hand_position([0, 0, 0], [1, 4, 0]))

    def test_empty_buffers_yield_empty_sample(self):
        self.assertEqual([], ActionDataPrepare._own_hand_position([], []))

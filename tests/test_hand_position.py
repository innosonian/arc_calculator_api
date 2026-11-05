"""압박 위치 판정 단위 테스트.

판정 함수는 표본의 순서와 관계없이 표본 전체에서 오류 코드의 존재 여부로 판정한다. 표본은
ActionDataPrepare._own_hand_position이 이번 압박의 하강 구간과 이완 구간으로 재구성한 값이며,
재구성 규칙은 test_hand_position_attribution.py가 검증한다. 재구성된 표본은 하강 구간 뒤에 이완
구간이 이어지고 0을 포함하지 않으므로, 아래 표본도 이 형태로 둔다.

마네킨은 좌우 코드(4/8)를 압박 카운트가 오른 뒤 이완 구간 1~2패킷에만 보내는 경우가 있으므로,
표본 전체를 대상으로 판정해야 앱 실시간 판정과 같아진다. 아래 표본은 유럽법인·사무실 마네킨과
골든 데이터의 패턴을 재구성 형태로 옮긴 것이다.
"""

from unittest import TestCase

from calculators.action_evaluator import ActionEvaluator


class TestHandPosition(TestCase):
    def setUp(self):
        self.service = ActionEvaluator(None)

    def test_side_code_only_in_release_phase_is_side(self):
        # 유럽 마네킨 패턴: 이완 구간 마지막 패킷에만 8, 하강 구간은 전부 1
        self.assertEqual("right", self.service.get_hand_position_spot([1, 1, 1, 1, 1, 1, 8]))

    def test_golden_side_code_in_release_phase_is_side(self):
        # 골든 cpr_3 압박 64번(1부터 셈)의 재구성 표본: 하강 구간 1 4패킷, 다음 버퍼 선두의 이완 구간 1, 1, 4
        self.assertEqual("left", self.service.get_hand_position_spot([1, 1, 1, 1, 1, 1, 4]))

    def test_side_code_through_downstroke_is_side(self):
        # 사무실 마네킨 패턴: 하강 구간 내내 4
        self.assertEqual("left", self.service.get_hand_position_spot([4, 4, 4, 4, 4, 4]))

    def test_down_code_in_release_phase_is_down(self):
        self.assertEqual("down", self.service.get_hand_position_spot([1, 1, 1, 1, 2]))

    def test_down_code_takes_priority_over_side_codes(self):
        self.assertEqual("down", self.service.get_hand_position_spot([1, 1, 4, 4, 2]))

    def test_empty_samples_is_center(self):
        # 버퍼가 비었거나 0뿐이면 재구성된 표본이 빈 리스트다.
        self.assertEqual("center", self.service.get_hand_position_spot([]))

    def test_all_center_is_center(self):
        self.assertEqual("center", self.service.get_hand_position_spot([1, 1, 1, 1, 1, 1]))

    def test_side_tie_is_right(self):
        self.assertEqual("right", self.service.get_hand_position_spot([8, 1, 1, 4]))

    def test_more_left_than_right_is_left(self):
        self.assertEqual("left", self.service.get_hand_position_spot([8, 1, 1, 4, 4]))

    def test_input_list_is_not_modified(self):
        samples = [1, 1, 1, 1, 1, 1, 8]
        self.service.get_hand_position_spot(samples)
        self.assertEqual([1, 1, 1, 1, 1, 1, 8], samples)


class TestFingerPosition(TestCase):
    """infant 판정.

    infant 마네킨(Brayden Baby)은 압박 위치 코드의 상위 비트를 플래그로 설정해 보내는 경우가 있다.
    16은 판정 없음(idle), 17은 정위치, 18은 복부이며 하위 4비트가 성인과 같은 위치 코드다.
    판정 함수는 하위 4비트만 남긴 뒤 성인 판정 규칙(get_hand_position_spot)을 그대로 적용한다.
    16은 판정 단계에서 마스크 후 0이 되어 무시된다. 표본 재구성은 마스크 전 원본 코드로 0을 찾아
    구간을 나누므로, 16은 재구성 단계에서는 0이 아니라 압박 구간에 포함되고 표본에도 남는다.
    infant 마네킨은 좌우 코드(4/8)를 보내지 않지만, 규칙을 성인과 같게 유지하므로 left/right로 판정한다.
    """

    def setUp(self):
        self.service = ActionEvaluator(None)

    def test_idle_flag_only_is_center(self):
        # 세션 첫 압박 패턴: 위치 코드는 카운트가 오르는 패킷부터 수신되므로 첫 압박 버퍼는 16뿐이다.
        # 마스크 후 전부 0이라 기본값 center다. 실제로 누른 위치와 관계없이 center가 되는 한계가 있다.
        self.assertEqual("center", self.service.get_finger_position_spot([16] * 17))

    def test_idle_flag_before_center_is_center(self):
        # 깊이 0인 16 뒤에 17이 오는 사례: 16은 무시되고 17만 판정에 사용되어 center다.
        self.assertEqual("center", self.service.get_finger_position_spot([16] * 17 + [17] * 6))

    def test_idle_flag_between_center_codes_is_center(self):
        # 긴 멈춤 사례: 17 사이에 16이 수백 개 있다. 이전 규칙에서는 16 하나로 down이 됐다.
        self.assertEqual("center", self.service.get_finger_position_spot([17, 17] + [16] * 300 + [17, 17]))

    def test_flagged_down_code_is_down(self):
        # 18(0x12)은 하위 4비트가 2이므로 복부다.
        self.assertEqual("down", self.service.get_finger_position_spot([16, 16, 18, 18, 18]))

    def test_down_code_in_downstroke_is_down(self):
        self.assertEqual("down", self.service.get_finger_position_spot([2, 1, 1, 1]))

    def test_side_code_is_side_like_adult(self):
        # infant 마네킨은 4/8을 보내지 않지만 규칙은 성인과 같다. 이전 규칙에서는 down이었다.
        self.assertEqual("left", self.service.get_finger_position_spot([1, 1, 1, 1, 1, 1, 4]))

    def test_flagged_side_codes_use_adult_tie_rule(self):
        # 20(0x14)은 좌, 24(0x18)는 우다. 마스크 후 4와 8이 동수이므로 성인 규칙대로 right다.
        self.assertEqual("right", self.service.get_finger_position_spot([24, 17, 20]))

    def test_all_center_is_center(self):
        self.assertEqual("center", self.service.get_finger_position_spot([1, 1, 1, 1, 1]))

    def test_empty_samples_is_center(self):
        self.assertEqual("center", self.service.get_finger_position_spot([]))

    def test_high_bits_are_ignored(self):
        # 17(0x11)은 하위 4비트가 1이므로 정위치다.
        self.assertEqual("center", self.service.get_finger_position_spot([17, 17, 1, 1]))

    def test_input_list_is_not_modified(self):
        samples = [16, 17, 4, 18, 1]
        self.service.get_finger_position_spot(samples)
        self.assertEqual([16, 17, 4, 18, 1], samples)

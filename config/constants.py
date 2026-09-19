PACKET_MS = 50
# Historical fixture boundary only. The approved detector requires two real
# confirmation packets and never adds a breath solely because EOF is reached.
VENT_TAIL_MIN_PACKETS = 15
HANDSOFF_DEADTIME_MS = 1000  # TODO: action 소요 시간이 handsoff 로 넘어간다는 기준을 정해야함

# lay person 기준, aed shock(no-shock) 이후 cpr 시작 사이에 적용하는 handsoff buffer
HANDSOFF_BUFFER_AFTER_AED_FOR_LAY = 5000

AED_EVENT_ARRIVED = 0
AED_EVENT_POWER_ON = 1
AED_EVENT_PADS_ON = 10
AED_EVENT_PRESS_BUTTON_SHOCK = 15
AED_EVENT_SHOCK_DELIVERED = 16
AED_EVENT_NO_SHOCK_ADVISE = 21
AED_EVENT_BEGIN_CPR = 19
AED_SEPARATE_EVENTS = [AED_EVENT_SHOCK_DELIVERED, AED_EVENT_NO_SHOCK_ADVISE]

CYCLE_COMPLETE_CONDITION_COMP_COUNT = 10


ACTION_TYPE_COMP = "comp"
ACTION_TYPE_VENT = "vent"
ACTION_TYPE_AED = "aed"
ACTION_TYPE_LAST = "last"

CALC_CASE_CPR = "cpr"
CALC_CASE_NOT_CALC = "not_calc"
CALC_CASE_DID_NOT_RESCUE_VENT = "did_not_rescue_vent"
CALC_CASE_ONLY_COMP = "only_comp"
CALC_CASE_ONLY_VENT = "only_vent"
CALC_CASE_RESCUE_VENT = "rescue_vent"

EVENT_ID_START_COMP = 0
EVENT_ID_END_COMP = 1
EVENT_ID_START_VENT = 10
EVENT_ID_END_VENT = 11
EVENT_ID_START_AED = 20
EVENT_ID_END_AED = 21

# Historical clamp value retained for legacy imports/fixtures. Active detection
# uses peak-drop10mL(adult/child) or provisional5mL(infant), without this clamp.
MINIMUM_VENT_VOLUME = 10

# 압박이 실제로 일어난(신호 있는) 패킷·액션을 판별하는 raw depth 하한.
# raw depth는 mm의 2배(채점 시 /2). 마네킨 경계 마커가 만든 가짜 압박은 깊이≈0이라
# 이 값 미만으로 걸러진다. 환기량(MINIMUM_VENT_VOLUME)과는 별개 개념이다.
MINIMUM_COMP_DEPTH = 10

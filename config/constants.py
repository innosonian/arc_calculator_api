PACKET_MS = 50
# 스트림이 하강 전에 끊긴 마지막 환기를 액션으로 인정할 최소 관측 길이(패킷 수).
# 직전 액션을 확정한 패킷부터 세어 이보다 짧으면 새 호흡이 아니라 직전 호흡의 잔여 흔들림으로 본다.
# 실데이터에서 잔여 흔들림은 1~8패킷, 잘린 새 호흡은 27패킷 이상으로 갈린다.
VENT_TAIL_MIN_PACKETS = 15  # 15 * PACKET_MS = 750ms
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

MINIMUM_VENT_VOLUME = 10

# 압박이 실제로 일어난(신호 있는) 패킷·액션을 판별하는 raw depth 하한.
# raw depth는 mm의 2배(채점 시 /2). 마네킨 경계 마커가 만든 가짜 압박은 깊이≈0이라
# 이 값 미만으로 걸러진다. 환기량(MINIMUM_VENT_VOLUME)과는 별개 개념이다.
MINIMUM_COMP_DEPTH = 10

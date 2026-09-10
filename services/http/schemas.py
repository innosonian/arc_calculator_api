"""참고 HSTM V2와 동일한 HTTP 입력 타입·기본 조건·비율 해석."""

from typing import TypedDict


class ConditionType(TypedDict):
    # 원본 hstm_v2 services/http/schemas.py:4-10
    mode: str
    target: str
    training_type: str
    guideline: str
    cpr_cycle_type: str
    is_2rescuers: bool


class RequestBody(TypedDict):
    cpr_b64_data: bytes
    aed_b64_data: bytes
    condition: ConditionType | None
    vp_event_list: list[dict]


class ActionRequestBody(TypedDict):
    action_list: list[dict]
    aed_part_list: list[dict]
    vp_action_list: list[dict]


class VPEvent(TypedDict):
    # 원본 hstm_v2 services/http/schemas.py:26-28
    event: int
    timestamp: int


class ParsedData(TypedDict):
    # 원본 hstm_v2 services/http/schemas.py:31-33
    rtdata_list: list[dict]
    aed_data_list: list[dict]


# 참고 HSTM V2의 누락 입력 기본값을 그대로 유지한다.
DEFAULT_CONDITION = ConditionType(
    mode="training",
    target="adult",
    training_type="cpr",
    guideline="AHA2020",
    cpr_cycle_type="302",
    is_2rescuers=False,
)


def comp_vent_targets(condition: dict | None) -> tuple[int, int]:
    """앱이 CompVentRatio를 전달하는 cpr_cycle_type 값에서 (사이클당 압박, 환기) 목표를 유도한다.

    앱은 '152'/'302' 형식을 보낸다. 압박:환기의 압박값이 코칭 문구·판정 문구의 압박 횟수
    (30 vs 15)로 쓰이며, 환기값(항상 2)은 환기 횟수 코칭에 쓰인다. '152'만 15:2로 보고
    그 외(302 및 미상)는 30:2로 둔다.

    정규화(str 변환·분리자 제거·strip 등)는 하지 않는다: 압박수 채점 border를 고르는
    cycle_evaluator._get_comp_cnt_key가 raw 값을 정확히 '152'와 비교하므로, 여기서만 관대하게
    해석하면 문구/판정 숫자(15)와 채점 기준(30:2)이 어긋난다. 두 경로가 동일하게 해석하도록
    raw 값을 그대로 비교한다(숫자 152, 공백 포함 어떤 변형도 양쪽에서 동일하게 30:2로 떨어진다).
    """
    # 원본 hstm_v2 services/http/schemas.py:46-61 (V1 패리티 보존)
    raw = (condition or {}).get("cpr_cycle_type")
    if raw == "152":
        return 15, 2
    return 30, 2

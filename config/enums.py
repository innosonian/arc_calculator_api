# 원본 hstm_v2 config/enums.py:1-19 그대로 이식(변경 없음).
from enum import Enum


class Actor(Enum):
    REAL_PERSON = "RP"
    VIRTUAL_PARTNER = "VP"


class ActorType(Enum):
    ONLY_REAL_PERSON = "ONLY:RP"
    VIRTUAL_PARTNER_COMP = "VP:COMP"
    VIRTUAL_PARTNER_VENT = "VP:VENT"
    ONLY_VIRTUAL_PARTNER = "ONLY:VP"

    def can_calc_comp(self) -> bool:
        return self in [ActorType.ONLY_REAL_PERSON, ActorType.VIRTUAL_PARTNER_VENT]

    def can_calc_vent(self) -> bool:
        return self in [ActorType.ONLY_REAL_PERSON, ActorType.VIRTUAL_PARTNER_COMP]

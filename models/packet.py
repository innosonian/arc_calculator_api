# 원본 hstm_v2 models/packet.py (V1 패리티 보존)
from typing import TypedDict

from config.enums import Actor


class CPRRTData(TypedDict):
    compression_depth: list[int]
    compression_rate: int
    compression_count: int
    hand_position: int
    ventilation_volume: list[int]
    ventilation_speed: int
    ventilation_count: int
    # F-9: packet_sequence_num은 파싱만 되고 이후 로직에서 미사용(기기 스펙 확인 대기).
    packet_sequence_num: int
    # cycle_num도 파싱만 됨(사이클은 CountMarker가 재계산).
    cycle_num: int
    timestamp: int
    actor: Actor


class AEDTData(TypedDict):
    event: int
    timestamp: int

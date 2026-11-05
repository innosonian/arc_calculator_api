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
    packet_sequence_num: int
    cycle_num: int
    timestamp: int
    actor: Actor


class AEDTData(TypedDict):
    event: int
    timestamp: int

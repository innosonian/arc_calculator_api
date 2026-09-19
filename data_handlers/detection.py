"""Independent packet events for the approved September 2026 detection rules.

This module identifies events and their source evidence only. It does not assign
elapsed time, scores, cycles, or public response fields.
"""

from dataclasses import dataclass

from config.constants import ACTION_TYPE_COMP, ACTION_TYPE_VENT
from services.config import Config


ADULT_CHILD_DROP_ML = 10
# User-approved provisional value; physical-device validation remains pending.
INFANT_DROP_ML = 5
CONFIRMATION_PACKETS = 2


@dataclass(frozen=True)
class DetectedAction:
    action_type: str
    packet_index: int
    # Half-open contiguous input range. These indices never reorder/filter input.
    evidence_start: int
    evidence_stop: int
    compression_rate: int = 0
    peak_volume: int | None = None
    peak_index: int | None = None
    first_confirmation_index: int | None = None


class PacketActionDetector:
    """Detect zero, one, or two independent events per original packet."""

    def __init__(self, config: Config):
        self.drop_ml = INFANT_DROP_ML if config.calculation_config.is_infant() else ADULT_CHILD_DROP_ML
        self.include_comp = not config.calculation_config.is_vent_only()
        self.include_vent = not config.calculation_config.is_cco()

    def detect(self, packets: list[dict]) -> list[DetectedAction]:
        if not packets:
            return []

        events = []
        previous_count = packets[0]["compression_count"]
        previous_depth = max(packets[0]["compression_depth"])
        previous_depth_rising = False
        previous_volume = 0
        compression_start = 0
        compression_pending_start = None
        candidate_start = None
        peak = 0
        peak_index = None
        first_confirmation = None
        confirmations = 0
        locked = False
        # The initial positive value may start a candidate. After a confirmed
        # breath, a rearm packet is only a baseline for a subsequent new rise.
        require_new_rise = False

        for index, packet in enumerate(packets):
            count = packet["compression_count"]
            volume = max(packet["ventilation_volume"])
            depth = max(packet["compression_depth"])
            depth_rising = index > 0 and depth > previous_depth
            upward_onset = depth_rising and not previous_depth_rising
            if upward_onset and compression_pending_start is None:
                compression_pending_start = index - 1

            if index > 0 and count != previous_count and count > 0:
                if self.include_comp:
                    events.append(DetectedAction(
                        action_type=ACTION_TYPE_COMP,
                        packet_index=index,
                        evidence_start=compression_start,
                        evidence_stop=index,
                        compression_rate=packet["compression_rate"],
                    ))
                compression_start = index
                compression_pending_start = None
            # Zero is a new observed baseline, not an event. A later0->1 is an
            # observable change even when an earlier1 occurred before the reset.
            previous_count = count

            if self.include_vent:
                if locked:
                    if volume == 0 or upward_onset:
                        locked = False
                        require_new_rise = True
                    # A rearm packet is not also evidence of a new breath.
                elif candidate_start is None:
                    if volume > 0 and (not require_new_rise or volume > previous_volume):
                        candidate_start = index
                        peak = volume
                        peak_index = index
                        confirmations = 0
                        first_confirmation = None
                else:
                    if volume > peak:
                        peak = volume
                        peak_index = index
                        confirmations = 0
                        first_confirmation = None
                    elif peak - volume >= self.drop_ml:
                        if confirmations == 0:
                            first_confirmation = index
                        confirmations += 1
                    else:
                        confirmations = 0
                        first_confirmation = None

                    if volume == 0 and peak < self.drop_ml:
                        # A return to zero closes a signal that can never meet
                        # the approved drop. Do not carry its onset into a
                        # later, separate breath's measurement interval.
                        candidate_start = None
                        peak = 0
                        peak_index = None
                        require_new_rise = True

                    if confirmations == CONFIRMATION_PACKETS:
                        events.append(DetectedAction(
                            action_type=ACTION_TYPE_VENT,
                            packet_index=index,
                            evidence_start=candidate_start,
                            evidence_stop=index + 1,
                            peak_volume=peak,
                            peak_index=peak_index,
                            first_confirmation_index=first_confirmation,
                        ))
                        candidate_start = None
                        peak = 0
                        peak_index = None
                        confirmations = 0
                        first_confirmation = None
                        require_new_rise = True
                        # Recognition occurs first. The same packet may leave
                        # the detector rearmed, but cannot start a second event.
                        locked = not (volume == 0 or upward_onset)
                        # Preserve a compression already rising through this
                        # breath, but do not reuse the previous compression's
                        # peak across an intervening ventilation phase. This
                        # affects evidence boundaries only, never event counts.
                        if compression_pending_start is not None:
                            compression_start = max(compression_start, compression_pending_start)
                        elif depth == 0:
                            compression_start = index

            previous_volume = volume
            previous_depth = depth
            previous_depth_rising = depth_rising

        # EOF provides no additional observation or inferred confirmation.
        return events

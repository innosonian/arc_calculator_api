from config.enums import Actor
from models.packet import CPRRTData, AEDTData
from services.config import Config
from config.constants import MAX_RAW_BYTES


class RawInputRejected(ValueError):
    def __init__(self, category, reason, status):
        super().__init__(reason)
        self.category = category
        self.reason = reason
        self.status = status


class DataParser:
    CPR_PARSING_BYTES = 28
    AED_PARSING_BYTES = 10

    def validate_cpr_bytes(self, cpr_byte_data: bytes) -> None:
        if len(cpr_byte_data) > MAX_RAW_BYTES:
            raise RawInputRejected("PAYLOAD_TOO_LARGE", "RAW_TOO_LARGE", 413)
        if not cpr_byte_data:
            raise RawInputRejected("INVALID_REQUEST", "EMPTY_RAW", 400)
        if not self._validate_data_length(cpr_byte_data):
            raise RawInputRejected("INVALID_REQUEST", "INVALID_RAW_LENGTH", 400)
        if cpr_byte_data[0] != 0xA8:
            raise RawInputRejected("INVALID_REQUEST", "INVALID_RAW_HEADER", 400)
        if len(cpr_byte_data) % self.CPR_PARSING_BYTES == 20 and any(cpr_byte_data[-20:]):
            raise RawInputRejected("INVALID_REQUEST", "NONZERO_RAW_TRAILER", 400)

    def parse_cpr_bytes(self, cpr_byte_data: bytes, config: Config) -> list[dict | CPRRTData]:
        rtdata_list = []
        # now = int(datetime.now(tz=pytz.UTC).timestamp() * 1000)
        # self.CPR_PARSING_BYTES = 20

        self.validate_cpr_bytes(cpr_byte_data)

        vent_vol_compensation = config.calculation_config.get_vent_vol_compensation()
        chunks = [
            cpr_byte_data[i : i + self.CPR_PARSING_BYTES] for i in range(0, len(cpr_byte_data), self.CPR_PARSING_BYTES)
        ]

        for i, chunk in enumerate(chunks):
            if len(chunk) == self.CPR_PARSING_BYTES:
                # if chunk[18] == 0:
                #     continue

                rtdata = CPRRTData(
                    compression_depth=list(chunk[1:11]),
                    compression_rate=chunk[11],
                    compression_count=chunk[12],
                    hand_position=chunk[13],
                    ventilation_volume=[c * vent_vol_compensation for c in chunk[14:16]],
                    ventilation_speed=self._translate_vent_speed(chunk[16]),
                    ventilation_count=chunk[17],
                    packet_sequence_num=chunk[18],
                    cycle_num=chunk[19],
                    timestamp=int.from_bytes(chunk[20:], "big"),
                    # timestamp=now + i * 50,
                    actor=Actor.REAL_PERSON,
                )
                rtdata_list.append(dict(rtdata))

        # TODO: Error handling to be added here (invalid chunks, invalid rtdata, packet_sequence_num)

        return rtdata_list

    def _validate_data_length(self, cpr_byte_data: bytes):
        return len(cpr_byte_data) % self.CPR_PARSING_BYTES in [0, 20]

    def parse_aed_bytes(self, aed_byte_data: bytes) -> list[dict | AEDTData]:
        aed_data_list = []

        try:
            self._validate(aed_byte_data[0], "aed")
        except (ValueError, IndexError):
            return aed_data_list

        # Split byte data into 20-byte chunks
        chunks = [
            aed_byte_data[i : i + self.AED_PARSING_BYTES] for i in range(0, len(aed_byte_data), self.AED_PARSING_BYTES)
        ]

        for chunk in chunks:
            if len(chunk) == self.AED_PARSING_BYTES:
                aed_data = AEDTData(
                    event=chunk[1],
                    timestamp=int.from_bytes(chunk[2:], "big"),
                )
                if aed_data["timestamp"]:
                    aed_data_list.append(dict(aed_data))

        return aed_data_list

    def _validate(self, key: int, bin_type: str = "cpr") -> None:
        valid_key = 168 if bin_type == "cpr" else 180
        if key != valid_key:
            raise ValueError

    def _translate_vent_speed(self, value: int) -> int:
        return value & 127

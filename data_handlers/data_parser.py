# 원본 hstm_v2 data_handlers/data_parser.py (V1 패리티 보존)
# 스펙 §4.5(I-1): 원본의 레거시 액션 분절기 generate_action_rtdata_list(원본 81-109행)는 미이식
# (프로덕션 호출 0건 grep 재확인 — 실사용 분절기는 ActionDataPrepare.generate_action_rtdata_list).
# 해당 제거로 ACTION_TYPE_COMP/ACTION_TYPE_VENT import도 정리.
from config.enums import Actor
from models.packet import CPRRTData, AEDTData
from services.config import Config


class DataParser:
    CPR_PARSING_BYTES = 28
    AED_PARSING_BYTES = 10

    def parse_cpr_bytes(self, cpr_byte_data: bytes, config: Config) -> list[dict | CPRRTData]:
        rtdata_list = []
        # now = int(datetime.now(tz=pytz.UTC).timestamp() * 1000)
        # self.CPR_PARSING_BYTES = 20

        if not self._validate_data_length(cpr_byte_data):
            raise ValueError("Data length error")

        try:
            self._validate(cpr_byte_data[0], "cpr")
        except ValueError:
            return rtdata_list

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
                    # F-5: hand_position 원바이트 도메인(값 의미) — 기기 스펙 확인 대기.
                    hand_position=chunk[13],
                    ventilation_volume=[c * vent_vol_compensation for c in chunk[14:16]],
                    ventilation_speed=self._translate_vent_speed(chunk[16]),
                    ventilation_count=chunk[17],
                    # F-9: packet_sequence_num 미사용(파싱만) — 기기 스펙 확인 대기.
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
        # F-4: 20바이트 꼬리 허용(len % 28 in [0, 20]) 근거 — 기기 스펙 확인 대기.
        # 20바이트 꼬리 청크는 파싱 루프의 길이 검사에서 조용히 버려진다.
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
        # F-3: 상위 1비트(0x80)를 버리는 마스킹 — 상위 비트 의미는 기기 스펙 확인 대기.
        return value & 127

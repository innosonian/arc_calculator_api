# 신규 테스트 공용 헬퍼 — 합성 rtdata 바이너리와 multipart 이벤트 빌더.
# 패킷 레이아웃 provenance: 원본 hstm_v2 tests/test_action_boundary.py:24-35,
# data_handlers/data_parser.py:34-47 (28바이트, 헤더 0xA8, offset 1-10 depth, 11 rate,
# 12 Ccnt, 13 hand, 14-15 vent raw, 20-28 timestamp BE).
import base64
import json

PACKET_BYTES = 28

# 상승 → 피크 → 0 스냅 호흡 파형(성인 보정 10배: raw 50/52 → 500/520ml → ARC good 구간)
BREATH_SHAPE = [(0, 0), (10, 12), (30, 35), (50, 52), (0, 0), (0, 0)]

# 한 번의 압박 깊이 램프(패킷 내 샘플 10개, raw 100 = digit 50mm → ARC adult good)
COMP_RAMP = (0, 20, 60, 100, 100, 60, 20, 0, 0, 0)
FLAT = (0,) * 10


def packet(
    comp_count: int = 0,
    vent_raw: tuple[int, int] = (0, 0),
    timestamp: int = 0,
    depth: tuple[int, ...] = FLAT,
    rate: int = 0,
    hand: int = 1,
) -> bytes:
    p = bytearray(PACKET_BYTES)
    p[0] = 0xA8  # rtdata 헤더 매직
    p[1:11] = bytes(depth)
    p[11] = rate
    p[12] = comp_count
    p[13] = hand
    p[14], p[15] = vent_raw
    p[20:28] = timestamp.to_bytes(8, "big")
    return bytes(p)


# 얕은 압박 램프(raw 40 = digit 20mm → depth grade 0/"low") — 확정적 저득점 세션용
WEAK_RAMP = (0, 10, 20, 40, 40, 20, 10, 0, 0, 0)


def _compressions(
    count: int, start_ts: int, start_count: int = 0, ramp: tuple[int, ...] = COMP_RAMP
) -> tuple[list[bytes], int, int]:
    """압박 count회 — 각 압박은 Ccnt 증가 패킷(깊이 램프) + 후속 패킷 1개."""
    packets = []
    ts = start_ts
    ccnt = start_count
    for _ in range(count):
        ccnt += 1
        packets.append(packet(ccnt, (0, 0), ts, depth=ramp, rate=110))
        ts += 50
        packets.append(packet(ccnt, (0, 0), ts, rate=110))
        ts += 50
    return packets, ts, ccnt


def _breaths(count: int, start_ts: int, comp_count: int = 0) -> tuple[list[bytes], int]:
    packets = []
    ts = start_ts
    for _ in range(count):
        for vent_raw in BREATH_SHAPE:
            packets.append(packet(comp_count, vent_raw, ts))
            ts += 50
    return packets, ts


def comp_session(n_comp: int, ramp: tuple[int, ...] = COMP_RAMP) -> bytes:
    """compression_only용: 압박 n_comp회 세션 바이너리. ramp=WEAK_RAMP면 depth 0점 세션."""
    packets = [packet(0, (0, 0), 0)]
    body, _, _ = _compressions(n_comp, 100, ramp=ramp)
    return b"".join(packets + body)


def cpr_session(cycles: list[tuple[int, int]]) -> bytes:
    """cpr용: (압박 수, 호흡 수) 사이클 나열 세션 바이너리.

    호흡→압박 전이에서 사이클이 나뉜다(transformers/counter.py의 CountMarker 규칙).
    """
    packets = [packet(0, (0, 0), 0)]
    ts = 100
    ccnt = 0
    for n_comp, n_breath in cycles:
        body, ts, ccnt = _compressions(n_comp, ts, ccnt)
        packets += body
        body, ts = _breaths(n_breath, ts, comp_count=ccnt)
        packets += body
    return b"".join(packets)


def vo_session(n_breaths: int) -> bytes:
    """ventilation_only용: 호흡 n_breaths회 세션 바이너리."""
    body, _ = _breaths(n_breaths, 0)
    return b"".join(body)


def condition_json(
    target: str = "adult",
    training_type: str = "cpr",
    guideline: str | None = "ARC2025",
    cpr_cycle_type: str = "302",
    is_2rescuers: bool = False,
    mode: str = "training",
) -> bytes:
    condition = {
        "mode": mode,
        "target": target,
        "training_type": training_type,
        "cpr_cycle_type": cpr_cycle_type,
        "is_2rescuers": is_2rescuers,
    }
    if guideline is not None:
        condition["guideline"] = guideline
    return json.dumps(condition).encode("utf-8")


_BOUNDARY = "arc-test-boundary-3f9c1d"


def multipart_event(parts: dict[str, bytes | str]) -> dict:
    """API GW proxy 이벤트(base64 인코딩 multipart/form-data) 빌더.

    원본 lambda_handler._parse_multipart_body가 읽는 실명 part 그대로 전달한다
    (rawHexBPfile, aedHexBPfile, condition, Open_Skill, Usage, vp_event_list, Custom 등).
    """
    segments = []
    for name, payload in parts.items():
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        segments.append(
            (f'--{_BOUNDARY}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n').encode("utf-8")
            + payload
            + b"\r\n"
        )
    body = b"".join(segments) + f"--{_BOUNDARY}--\r\n".encode("utf-8")
    return {
        "httpMethod": "POST",
        "path": "/cpr-analysis",
        "headers": {"Content-Type": f"multipart/form-data; boundary={_BOUNDARY}"},
        "isBase64Encoded": True,
        "body": base64.b64encode(body).decode("ascii"),
    }

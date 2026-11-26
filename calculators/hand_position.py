"""HP_V2 hand-position judgement (2026-11-26 U1~U3, D142~D144).

The rule is the app's HandPositionReleaseTracker.observe/evaluatePosition
(arc-resus-skills shared/.../HandPositionReleaseTracker.kt, 116~145), applied to
the existing DEPTH_V1r3 window (ActionDataPrepare._own_hand_position). The
window splitting still uses the raw codes; infant codes are masked with 0x0F
only here, right before the judgement.

Result codes: 1 center, 2 down, 4 left, 8 right, 16 up, 10 unknown direction,
None no sample (the compression is not scored for hand position).
"""

BOARDS = frozenset(("nordic", "esp32c6", "unknown"))
UNKNOWN_BOARD = "unknown"
VALUES = {1: "center", 2: "down", 4: "left", 8: "right", 16: "up", 10: "unknown"}
# config/borders.py is hash-pinned: up/unknown read the left (side, 20) row.
SCORE_ROWS = {"up": "left", "unknown": "left"}
_SUPPORTED = frozenset((0, 1, 2, 4, 8, 16))
_DIRECTIONS = (4, 8, 16)
# Raw codes the app draws on an unknown board (BlePacketParser.handPositionForCode); 10 is an error marker.
_LEGACY_RESULTS = frozenset((2, 4, 8, 16, 10))


def normalize_board(value):
    """Return (board, recognized). Absent is unknown; an unrecognized value is unknown too."""
    if value is None:
        return UNKNOWN_BOARD, True
    if type(value) is str and value in BOARDS:
        return value, True
    return UNKNOWN_BOARD, False


def _first_wrong(codes):
    return next((code for code in codes if code not in (0, 1)), None)


def is_unsupported_legacy(window, board, infant):
    """Adult unknown board whose first non-0/1 code is outside 2/4/8/16/10: judged None (user, 2026-11-26)."""
    if infant or board != UNKNOWN_BOARD:
        return False
    wrong = _first_wrong(window)
    return wrong is not None and wrong not in _LEGACY_RESULTS


def classify(window, records, peak_record, board, infant):
    """The HP_V2 result code of one compression window, or None.

    ``records`` holds the Record number of each window element; only the
    nordic rule reads it (release samples are the Records after ``peak_record``).
    """
    codes = [code & 0x0F for code in window] if infant else list(window)
    if infant or board == UNKNOWN_BOARD:
        wrong = _first_wrong(codes)
        if wrong is None:
            return 1
        if infant:
            return 10
        return wrong if wrong in _LEGACY_RESULTS else None
    if 2 in codes:
        return 2
    if board == "esp32c6":
        samples = codes
    else:
        samples = [code for code, record in zip(codes, records) if record > peak_record]
    if not samples or all(code == 0 for code in samples):
        return None
    if any(code not in _SUPPORTED for code in samples):
        return 10
    counts = {direction: samples.count(direction) for direction in _DIRECTIONS}
    if sum(counts.values()) * 4 >= len(samples):
        top = max(counts.values())
        winners = [direction for direction, count in counts.items() if count == top]
        return winners[0] if len(winners) == 1 else 10
    return 1

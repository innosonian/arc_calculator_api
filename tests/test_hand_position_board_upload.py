"""hand_position_board upload part (D143): parsing, stored input identity, conflicts and Worker recovery."""

import base64
import json
from urllib import parse

import pytest

from mock_journey.projection import ProjectionSchema, project_input, typed_identity
from services.http.legacy_request import _parse_multipart_body
from services.http.service import parse_body
from tests.journey_support import (
    MULTIPART_BOUNDARY, MULTIPART_CONTENT_TYPE, EventLog, JourneyStore, V2Journey, dummy_course,
)
from tests.test_hand_position import PRESS_SIDE, _session
from tests.vcc_support import event as proxy_event


# Pressing-phase side codes only: unknown and esp32c6 judge left, nordic (release only) center.
DATA = _session([PRESS_SIDE] * 60)
EXPECTED = {None: "left", "unknown": "left", "nordic": "center", "esp32c6": "left", "Nordic": "left", "": "left"}


def _body(condition, board):
    parts = [(b"rawHexBPfile", DATA), (b"condition", json.dumps(condition).encode())]
    if board is not None:
        parts.append((b"hand_position_board", board.encode()))
    boundary = MULTIPART_BOUNDARY.encode()
    return b"".join(b"--" + boundary + b'\r\nContent-Disposition: form-data; name="' + name + b'"\r\n\r\n' + value
                    + b"\r\n" for name, value in parts) + b"--" + boundary + b"--\r\n"


def _upload(h, token, attempt, board, expected=202):
    event = proxy_event("POST", f"/api/v2/attempts/{attempt['attemptId']}/calculation/", token=token,
                        content_type=MULTIPART_CONTENT_TYPE)
    event.update(body=base64.b64encode(_body(attempt["condition"], board)).decode(), isBase64Encoded=True)
    return h.expect(expected, "POST", event["path"], event=event)


def _values(result):
    actions = [action for part in result["calculation"]["cpr_score"]["part_scores"]
               for action in part["action_with_score_list"]]
    return {action.get("hand_position", {}).get("value") for action in actions}


def _journey():
    h = V2Journey(JourneyStore.memory(), events=EventLog())
    token = h.login().token
    course = dummy_course("mock-compression-only", "adult")
    return h, token, h.start(token, course, course.practice_link_id)


@pytest.mark.parametrize("board", list(EXPECTED))
def test_each_board_value_reaches_the_calculation(board):
    h, token, attempt = _journey()
    _upload(h, token, attempt, board)
    assert h.work(attempt["attemptId"]) is True
    result = h.result(token, attempt["attemptId"])
    assert _values(result) == {EXPECTED[board]}
    unrecognized = [record for record in h.events.records if record["event"] == "hand_position_board_unrecognized"]
    assert len(unrecognized) == (1 if board in ("Nordic", "") else 0)
    for record in unrecognized:
        # One sanitized API diagnostic: bound identifiers only, never the sent value.
        assert (record["role"], record["category"]) == ("api", "diagnostic")
        assert set(record["fields"]) == {"level", "message", "attempt_id", "progress_epoch", "request_id",
                                         "http_request_id"}
        assert record["fields"]["attempt_id"] == attempt["attemptId"]
        assert "Nordic" not in json.dumps(record)
    metas = [json.loads(value["Body"]) for (_, key), value in h.objects.objects.items() if key.endswith(".meta.json")]
    assert [meta.get("hand_position_board") for meta in metas] == [board if board in ("nordic", "esp32c6") else None]
    assert all("hand_position_board" not in meta["condition"] for meta in metas)


@pytest.mark.parametrize("first,second,status", [
    ("nordic", "nordic", 200), ("nordic", "esp32c6", 409), ("nordic", None, 409),
    (None, "unknown", 200), (None, "bogus", 200), ("esp32c6", "unknown", 409),
])
def test_resending_the_same_file_follows_the_existing_conflict_rule(first, second, status):
    h, token, attempt = _journey()
    _upload(h, token, attempt, first)
    assert h.work(attempt["attemptId"]) is True
    reply = _upload(h, token, attempt, second, expected=status)
    if status == 409:
        assert reply["code"] == "ATTEMPT_INPUT_CONFLICT"
    assert _values(h.result(token, attempt["attemptId"])) == {EXPECTED[first]}


def test_worker_restart_recalculates_with_the_stored_board():
    h, token, attempt = _journey()
    _upload(h, token, attempt, "nordic")
    h.restart()  # New API and Worker on the same table and objects; nothing is held in memory.
    assert h.work(attempt["attemptId"]) is True
    assert _values(h.result(token, attempt["attemptId"])) == {"center"}


def test_parsers_keep_the_part_only_when_sent():
    headers = {"Content-Type": MULTIPART_CONTENT_TYPE}
    condition = {"mode": "training", "target": "adult", "training_type": "compression_only", "guideline": "ARC2025",
                 "cpr_cycle_type": "302", "is_2rescuers": False}
    sent = _parse_multipart_body(base64.b64encode(_body(condition, "nordic")).decode(), headers, True)
    assert sent["hand_position_board"] == "nordic" and "hand_position_board" not in sent["condition"]
    assert "hand_position_board" not in _parse_multipart_body(base64.b64encode(_body(condition, None)).decode(), headers, True)
    raw = _body(condition, "x").replace(b"x\r\n", b"\xff\xfe\r\n")
    assert _parse_multipart_body(base64.b64encode(raw).decode(), headers, True)["hand_position_board"] == "��"

    def form(**fields):
        encoded = parse.urlencode(fields)
        return base64.urlsafe_b64encode(parse.quote(encoded).encode()).decode()
    cpr = base64.urlsafe_b64encode(DATA).decode()
    assert parse_body(form(cpr_b64_data=cpr, hand_position_board="esp32c6"))["hand_position_board"] == "esp32c6"
    assert "hand_position_board" not in parse_body(form(cpr_b64_data=cpr))


def test_projection_stores_only_a_known_board_and_changes_identity_only_for_it():
    definition = {"condition": {"mode": "training", "target": "adult", "training_type": "compression_only",
                                "guideline": "ARC2025", "cpr_cycle_type": "302", "is_2rescuers": False},
                  "calculation_profile": {}, "goal": {"kind": "compressions", "required": 60}, "catalog_version": "t",
                  "profile_version": "p", "adapter_version": "a", "projection_version": "v"}
    schema = ProjectionSchema("v", {})

    def projected(board):
        body = {"condition": dict(definition["condition"]), "cpr_b64_data": DATA, "aed_b64_data": b""}
        if board is not ...:
            body["hand_position_board"] = board
        return project_input(body, definition, schema)

    plain = projected(...)
    assert "hand_position_board" not in plain.payload["calculation_input"]
    for same in (None, "unknown", "Nordic", "", 7):
        assert typed_identity(projected(same)) == typed_identity(plain)
    nordic, esp = projected("nordic"), projected("esp32c6")
    assert nordic.payload["calculation_input"]["hand_position_board"] == "nordic"
    assert len({typed_identity(plain), typed_identity(nordic), typed_identity(esp)}) == 3

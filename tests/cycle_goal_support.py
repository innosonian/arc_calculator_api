"""D136 CPR course journey on the public /api/v2 harness (memory in tests/, DynamoDB Local in integration_tests/).

Expected values follow docs/DECISIONS.md D136: a CPR practice completes when
the calculator's closed ``cpr`` cycle count meets the catalog's required 3
AND the unchanged tester passes; ``isCompleted``/``isPassed`` are true/false;
a passing final assessment finishes the course.
"""

import json

from mock_journey.contracts import CURRENT_ADAPTER_VERSION
from tests._synth import cpr_session


def cpr_course_journey(store):
    """A CPR course: a short practice stays incomplete, three cycles complete it; a passing final FINISHES."""
    from tests.journey_support import V2Journey, dummy_course

    h = V2Journey(store)
    course = dummy_course("mock-cpr", "adult")
    token = h.login().token
    detail = h.course(token, course)
    assert json.loads(h.store.row(f"ATTEMPT#{_first_attempt(h, token, course, detail)}", "META")["definition_json"]) \
        ["adapter_version"] == CURRENT_ADAPTER_VERSION == "arc-internal-detection-v5"

    def run(link_id, data):
        attempt = h.start(token, course, link_id)
        h.upload(token, attempt["attemptId"], attempt["condition"], data=data)
        assert h.work(attempt["attemptId"]) is True
        return h.result(token, attempt["attemptId"])

    def item(link_id):
        return next(row for row in h.course(token, course)["courseItems"] if row["courseItemLinkId"] == link_id)

    short = run(course.practice_link_id, cpr_session([(30, 2)]))
    assert short["evaluation"]["goal"] == {"kind": "cycles", "required": 3, "observed": 1, "met": False,
                                           "status": "evaluated"}
    assert short["evaluation"]["program_completed"] is False
    assert short["progressApplication"]["reason"] == "REQUIREMENTS_NOT_MET"
    assert item(course.practice_link_id)["isCompleted"] is False
    assert item(course.practice_link_id)["isPassed"] is False
    assert h.start(token, course, course.final_link_id, expected=409)["code"] == "PREREQUISITES_NOT_COMPLETED"

    complete = run(course.practice_link_id, cpr_session([(30, 2)] * 3))
    assert complete["evaluation"]["goal"] == {"kind": "cycles", "required": 3, "observed": 3, "met": True,
                                              "status": "evaluated"}
    assert complete["evaluation"]["score"]["decision"] == "pass"
    assert complete["evaluation"]["program_completed"] is True and complete["evaluation"]["reason_codes"] == []
    assert complete["progressApplication"]["reason"] == "APPLIED"
    assert item(course.practice_link_id)["isCompleted"] is True
    assert item(course.practice_link_id)["isPassed"] is True

    final = run(course.final_link_id, cpr_session([(30, 2)] * 3))
    assert final["evaluation"]["program_completed"] is True
    assert final["progressApplication"]["reason"] == "APPLIED"
    assert item(course.final_link_id)["isCompleted"] is True and item(course.final_link_id)["isPassed"] is True
    listing = h.courses(token)
    assert next(row for row in listing["results"] if row["enrollmentId"] == course.enrollment_id)["status"] == "FINISHED"
    return h


def _first_attempt(h, token, course, detail):
    attempt = h.start(token, course, course.practice_link_id, definition_hash=detail["definitionHash"])
    h.cancel(token, attempt["attemptId"])
    return attempt["attemptId"]



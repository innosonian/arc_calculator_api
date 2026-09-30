"""P4 behavior-preservation baseline: the exact DynamoDB write requests of scripted flows.

tests/fixtures/v2_baseline/write_requests.json was captured before the
storage-key/constant extraction (HEAD 337cf47 + base_after_v1p2.patch,
write-tree 84cd81fdd3863ae20f896b1d613866bc434f725b). Each flow in
tests/write_baseline_support.py runs again on today's code and every
PutItem/TransactWriteItems request must match the stored template: action
order, ConditionExpression strings, ExpressionAttributeNames/Values, typed
items and the key order of every object.

Capture method (scripts/capture_baselines.py, D125): all flows ran twice in
separate processes on JourneyStore.memory(), with different PYTHONHASHSEED and
with util.uploader's wall clock shifted by three days in the second run.
Values that differed between the runs became placeholders labelled by first
appearance within the flow (UUID, 32/64-hex digests of random or wall-clock
input, token/nonce tails, wall-clock dates and epoch seconds). Only the
decoded GSI cursor maps inside the Relay checkpoint row may differ in key
order between runs (they come from the in-memory query projection), so their
key order is not compared. Setup writes that golden_flow already covers
(login, starts, uploads) and all test seeding are excluded from the restart,
Relay and assigned-learner flows.

What this baseline cannot see (B-01, mutation experiment 2026-09-30): it fixes
the write requests and their order only. A change on the read side that
leaves every write unchanged -- a removed or weakened guard that only reads a
row (for example the ``final_ref`` check before a restart-limit closure), or a
Relay budget/cap that no scripted flow reaches -- passes this baseline and the
Worker call-order baseline alike. Such conditions need their own unit test
with an explicit negative case (tests/test_job_restart_limit.py,
tests/test_relay_backlog_throughput.py).
"""

from copy import deepcopy
import hashlib
import re

import pytest

from tests.write_baseline_support import FLOWS, RELAY_SCOPE, assert_writes_match, load_write_baseline, observe


BASELINE = load_write_baseline()
UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
H64 = r"[0-9a-f]{64}"
# ARCHITECTURE §4 row table and the existing USER/SESSION/ATTEMPT/JOB/OUTBOX rows.
KEY_LAYOUT = [
    (rf"USER#.+", r"STATE"),
    (rf"SESSION#{UUID}", rf"AUTH|COURSE_CREATE#{UUID}"),
    (rf"ATTEMPT#{UUID}", r"META"),
    (rf"JOB#{UUID}", r"STATE"),
    (rf"OUTBOX#{UUID}", r"DISPATCH"),
    (rf"COURSE_LEARNER#{H64}", rf"EPOCH#{UUID}#HEAD"),
    (rf"COURSE_PRINCIPAL#.+", rf"EPOCH#{UUID}"),
    (rf"COURSE#{H64}", rf"EPOCH#{UUID}#(HEAD|FINAL|ITEM#{H64}|START#{UUID}|REPORT#{UUID}#{UUID})"),
    (rf"COURSE_START#{UUID}", r"META"),
    (rf"SUBMISSION#{UUID}", rf"RESULT#{H64}"),
    (rf"RELAY_SCAN#{H64}", r"PROGRESS#v1"),
]


@pytest.fixture(scope="module")
def observed():
    return {}


def _observation(observed, name):
    if name not in observed:
        observed[name] = observe(name)
    return observed[name]


def test_baseline_covers_every_flow():
    assert list(BASELINE) == list(FLOWS)
    assert all(BASELINE[name] for name in FLOWS)


@pytest.mark.parametrize("name", list(FLOWS))
def test_write_requests_match_baseline(observed, name):
    assert_writes_match(BASELINE[name], _observation(observed, name), name)


def _actions(writes):
    for write in writes:
        if write["operation"] == "PutItem":
            yield "Put", write["request"]
        else:
            for action in write["request"]["TransactItems"]:
                ((kind, body),) = action.items()
                yield kind, body


def _key(kind, body):
    key = body["Item"] if kind == "Put" else body["Key"]
    return key["PK"]["S"], key["SK"]["S"]


@pytest.mark.parametrize("name", list(FLOWS))
def test_every_written_key_follows_the_documented_row_layout(observed, name):
    """Independent of the template: each PK/SK pair has one documented shape."""
    for kind, body in _actions(_observation(observed, name)):
        assert kind in {"Put", "ConditionCheck"}
        pk, sk = _key(kind, body)
        assert any(re.fullmatch(pk_rule, pk) and re.fullmatch(sk_rule, sk) for pk_rule, sk_rule in KEY_LAYOUT), (pk, sk)
        if kind == "Put":
            assert set(body["Item"]) >= {"PK", "SK"}
        else:
            assert set(body["Key"]) == {"PK", "SK"}


def test_final_cancel_releases_only_the_attempt_epoch_final(observed):
    """state.cancel_attempt of a created final assessment: HEAD then FINAL of the attempt's epoch."""
    writes = _observation(observed, "vcc_course_lifecycle")
    cancels = [write for write in writes if write["operation"] == "TransactWriteItems" and any(
        "Put" in action and action["Put"]["Item"].get("cancel_reason") for action in write["request"]["TransactItems"])]
    assert len(cancels) == 1
    actions = cancels[0]["request"]["TransactItems"]
    head, final = actions[-2]["Put"], actions[-1]["Put"]
    assert head["ConditionExpression"] == "#r = :r AND #e = :e"
    assert final["ConditionExpression"] == "#r = :r AND #e = :e AND #a = :a AND #p = :p"
    epoch = head["ExpressionAttributeValues"][":e"]["S"]
    assert head["Item"]["SK"]["S"] == f"EPOCH#{epoch}#HEAD" and final["Item"]["SK"]["S"] == f"EPOCH#{epoch}#FINAL"
    assert head["Item"]["PK"] == final["Item"]["PK"]
    assert final["Item"]["phase"] == {"S": "free"} and final["Item"]["active_attempt_id"] == {"NULL": True}


def test_relay_checkpoint_row_uses_the_environment_namespace(observed):
    expected = "RELAY_SCAN#" + hashlib.sha256(RELAY_SCOPE["environment"].encode()).hexdigest()
    keys = {_key(kind, body) for kind, body in _actions(_observation(observed, "relay_checkpoint"))
            if _key(kind, body)[0].startswith("RELAY_SCAN#")}
    assert keys == {(expected, "PROGRESS#v1")}


def test_restart_limit_closures_are_single_transactions(observed):
    legacy = [write for write in _observation(observed, "legacy_restart_limit")
              if write["operation"] == "TransactWriteItems" and any(
                  action.get("Put", {}).get("Item", {}).get("failure_basis") for action in write["request"]["TransactItems"])]
    assert len(legacy) == 1
    assert [_key(kind, body)[0].split("#")[0] for kind, body in _actions(legacy)] == ["JOB", "ATTEMPT", "USER"]
    for name in ("course_restart_limit_error", "course_restart_limit_killed"):
        sealed = [write for write in _observation(observed, name)
                  if write["operation"] == "TransactWriteItems" and any(
                      "terminal_seal" in action.get("Put", {}).get("Item", {})
                      for action in write["request"]["TransactItems"])]
        assert len(sealed) == 1
        keys = [_key(kind, body) for kind, body in _actions(sealed)]
        assert [pk.split("#")[0] for pk, _ in keys] == ["JOB", "ATTEMPT", "USER", "COURSE", "COURSE"]
        assert [sk.rsplit("#", 1)[-1] for _, sk in keys[3:]] == ["HEAD", "FINAL"]


# -- the matcher itself rejects changes (so a pass is evidence) ------------------

def _first_transaction(name):
    template = BASELINE[name]
    index = next(i for i, write in enumerate(template) if write["operation"] == "TransactWriteItems"
                 and len(write["request"]["TransactItems"]) > 1)
    return template, index


def test_matcher_rejects_a_changed_condition_string(observed):
    template, index = _first_transaction("golden_flow")
    changed = deepcopy(_observation(observed, "golden_flow"))
    action = changed[index]["request"]["TransactItems"][0]
    ((_, body),) = action.items()
    body["ConditionExpression"] += " AND #x = :x"
    with pytest.raises(AssertionError):
        assert_writes_match(template, changed)


def test_matcher_rejects_reordered_actions_and_keys(observed):
    template, index = _first_transaction("golden_flow")
    swapped = deepcopy(_observation(observed, "golden_flow"))
    items = swapped[index]["request"]["TransactItems"]
    items[0], items[1] = items[1], items[0]
    with pytest.raises(AssertionError):
        assert_writes_match(template, swapped)
    reordered = deepcopy(_observation(observed, "golden_flow"))
    ((_, body),) = reordered[index]["request"]["TransactItems"][0].items()
    names = body["ExpressionAttributeNames"]
    body["ExpressionAttributeNames"] = dict(reversed(list(names.items())))
    assert body["ExpressionAttributeNames"] == names
    with pytest.raises(AssertionError, match="key order"):
        assert_writes_match(template, reordered)


def test_matcher_rejects_a_changed_key_or_a_missing_write(observed):
    template = BASELINE["golden_flow"]
    changed = deepcopy(_observation(observed, "golden_flow"))
    item = changed[0]["request"]["Item"]
    item["SK"] = {"S": item["SK"]["S"] + "#X"}
    with pytest.raises(AssertionError):
        assert_writes_match(template, changed)
    with pytest.raises(AssertionError):
        assert_writes_match(template, _observation(observed, "golden_flow")[1:])

"""2026-09-28 decisions Q16/Q5/Q6/Q18: v2 session expiry commit guard, course
transaction floor, API payload/input relation and provider non-empty text."""

from copy import deepcopy
from dataclasses import asdict, replace
import json

import pytest

from mock_journey.aws_settings import AwsSettings
from mock_journey.course_contracts import StartCommand
from mock_journey.course_errors import CourseError
from mock_journey.course_provider import validate_bundle
from mock_journey.course_service import CourseService
from mock_journey.course_settings import fixture_course_settings
from mock_journey.storage_keys import session_key, user_key
from tests.course_store_fakes import _action_allowed
from mock_journey.settings import base64_body_bytes
from mock_journey.state import DynamoStateRepository
from mock_journey.typed import parse_json
from mock_journey.dev_course import CATALOG_VERSION, MODE
from scripts.validate_aws_dev_bundle import BundleError
from tests.aws_dev_bundle_support import check, documents
from tests.aws_runtime_support import configuration
from tests.course_guards_support import (  # noqa: F401 (re-export)
    COMMIT_OPERATIONS, EXPIRES_AT, NOW, Context, _view, assert_expiry_between_read_and_commit_is_session_expired,
    assert_unexpired_session_still_commits, memory_context,
)
from tests.vcc_provider_support import BUNDLE_DOC, source_bundle
from tests.vcc_state_support import REQUEST, repository, seed_auth


FINAL_START_ACTIONS = 8


# ---------------------------------------------------------------------------
# Q16: every v2 commit re-checks session expiry (shared with DynamoDB Local).


@pytest.mark.parametrize("name", sorted(COMMIT_OPERATIONS))
def test_stored_expiry_after_read_refuses_commit_as_session_expired(name):
    assert_expiry_between_read_and_commit_is_session_expired(memory_context(), name)


@pytest.mark.parametrize("name", sorted(COMMIT_OPERATIONS))
def test_session_valid_at_commit_time_still_commits(name):
    assert_unexpired_session_still_commits(memory_context(), name)


@pytest.mark.parametrize("name", sorted(COMMIT_OPERATIONS))
def test_every_v2_commit_carries_session_expiry_and_user_guards(name):
    context = memory_context()
    COMMIT_OPERATIONS[name](context)()
    assert context.store.calls
    for actions in context.store.calls:
        assert actions[0] == {
            "op": "condition_check", "key": session_key(context.auth.session_id),
            "if_match": {"status": "active", "revision": context.auth.revision,
                         "principal": context.auth.principal},
            "if_greater": {"expires_at": NOW},
        }
        assert actions[1]["op"] == "condition_check"
        assert actions[1]["key"] == user_key(context.auth.principal)
        assert set(actions[1]["if_match"]) == {"epoch", "revision"}
        assert "if_greater" not in actions[1]
        assert all("if_greater" not in action for action in actions[1:])


def test_clock_passing_expiry_before_commit_is_refused_without_transaction():
    now = [NOW]
    context = memory_context(clock=None)
    context.repo.clock = lambda: now[0]
    view = _view(context)
    command = StartCommand(REQUEST, 501, 101, 1001, context.bundle.definition_hash)
    original = context.repo.policy.can_start

    def expire_after_verdict(*args):
        original(*args)
        now[0] = EXPIRES_AT

    context.repo.policy.can_start = expire_after_verdict
    calls = len(context.store.calls)
    before = context.snapshot()
    with pytest.raises(CourseError) as raised:
        context.repo.start(context.auth, command, kind="content", view=view, template=None)
    assert raised.value.code == "SESSION_EXPIRED"
    assert len(context.store.calls) == calls
    assert context.snapshot() == before


def test_retry_exhaustion_is_unchanged_for_plain_conflicts():
    context = memory_context()
    view = _view(context)
    context.store.always_conflict = True
    with pytest.raises(CourseError) as raised:
        context.repo.start(context.auth, StartCommand(REQUEST, 501, 101, 1001, context.bundle.definition_hash),
                           kind="content", view=view, template=None)
    assert raised.value.code == "TEMPORARILY_UNAVAILABLE"


@pytest.mark.parametrize("stored,allowed", [
    (NOW + 1, True), (NOW, False), (NOW - 1, False), (None, False), (True, False),
    (str(NOW + 1), False), (float(NOW + 1), False),
])
def test_in_memory_fake_greater_condition_matches_dynamodb_number_semantics(stored, allowed):
    row = {"PK": "SESSION#s", "SK": "AUTH", "status": "active"}
    if stored is not None:
        row["expires_at"] = stored
    action = {"op": "condition_check", "key": {"PK": "SESSION#s", "SK": "AUTH"},
              "if_match": {"status": "active"}, "if_greater": {"expires_at": NOW}}
    assert _action_allowed(row, action) is allowed
    assert _action_allowed(None, action) is False


def test_dynamodb_adapter_encodes_greater_condition_and_rejects_other_shapes():
    state = DynamoStateRepository(object(), "synthetic-table")
    key = {"PK": "SESSION#s", "SK": "AUTH"}
    encoded = state._course_control_action({
        "op": "condition_check", "key": key,
        "if_match": {"status": "active", "revision": 0, "principal": "p"},
        "if_greater": {"expires_at": NOW},
    })["ConditionCheck"]
    assert encoded["ConditionExpression"] == (
        "attribute_exists(#pk) AND #c0 = :c0 AND #c1 = :c1 AND #c2 = :c2 AND #g0 > :g0")
    assert encoded["ExpressionAttributeNames"]["#g0"] == "expires_at"
    assert encoded["ExpressionAttributeValues"][":g0"] == {"N": str(NOW)}
    plain = state._course_control_action({"op": "condition_check", "key": key, "if_match": {"status": "active"}})
    assert plain["ConditionCheck"]["ConditionExpression"] == "attribute_exists(#pk) AND #c0 = :c0"
    for bad in ({}, {"expires_at": "1"}, {"expires_at": True}, {"": 1}, [("expires_at", 1)]):
        with pytest.raises(Exception) as raised:
            state._course_control_action({"op": "condition_check", "key": key,
                                          "if_match": {"status": "active"}, "if_greater": bad})
        assert getattr(raised.value, "code", None) == "TEMPORARILY_UNAVAILABLE"
    with pytest.raises(Exception) as raised:
        state._course_control_action({"op": "put", "key": key, "item": {**key, "status": "active"},
                                      "if_greater": {"expires_at": NOW}})
    assert getattr(raised.value, "code", None) == "TEMPORARILY_UNAVAILABLE"


# ---------------------------------------------------------------------------
# Q5: the AWS course transaction floor holds the largest course write-set.

def observed_write_sets():
    sizes = {}
    for name, build in COMMIT_OPERATIONS.items():
        context = memory_context()
        run = build(context)
        calls = len(context.store.calls)
        run()
        committed = context.store.calls[calls:]
        assert len(committed) == 1
        sizes[name] = len(committed[0])
    return sizes


def test_course_write_sets_fit_the_aws_floor_of_eight():
    sizes = observed_write_sets()
    assert sizes["start_final"] == FINAL_START_ACTIONS
    assert sizes["start_content"] == 7
    assert sizes["start_training"] == 7
    assert sizes["report"] == 6
    assert max(sizes.values()) == FINAL_START_ACTIONS


def course_configuration(max_actions):
    config = configuration("api")
    settings = asdict(fixture_course_settings())
    settings["max_transaction_actions"] = max_actions
    config["course"] = {"mode": MODE, "catalog_version": CATALOG_VERSION, "settings": settings}
    return config


@pytest.mark.parametrize("max_actions,accepted", [(6, False), (7, False), (8, True), (20, True)])
def test_aws_course_transaction_floor_is_eight(max_actions, accepted):
    raw = json.dumps(course_configuration(max_actions))
    if accepted:
        assert AwsSettings.parse(raw, "api").course.max_transaction_actions == max_actions
        return
    with pytest.raises(ValueError) as raised:
        AwsSettings.parse(raw, "api")
    assert str(raised.value) == "Invalid explicit AWS journey configuration."
    assert raised.value.__cause__ is None


def test_role_bundle_check_uses_the_same_floor():
    for max_actions, accepted in ((7, False), (8, True)):
        configs = documents()
        for role in ("api", "worker"):
            configs[role]["course"]["settings"]["max_transaction_actions"] = max_actions
        if accepted:
            assert check(configs)["status"] == "dummy_dev_bundle_valid"
        else:
            with pytest.raises(BundleError, match="ROLE_CONFIGURATION_INVALID"):
                check(configs)


# ---------------------------------------------------------------------------
# Q6: encoded API body limit must hold the base64 form of the input quota.

def api_configuration(payload_limit, input_bytes):
    config = configuration("api")
    config["api"]["payload_limit"] = payload_limit
    config["storage"]["input_bytes"] = input_bytes
    config["storage"]["artifact_bytes"] = max(config["storage"]["artifact_bytes"], input_bytes)
    return config


def test_base64_body_bytes_is_four_times_ceiling_of_thirds():
    assert [base64_body_bytes(value) for value in (1, 2, 3, 4, 2097152)] == [4, 4, 4, 8, 2796204]
    for bad in (0, -1, True, 1.0, "3", None):
        with pytest.raises(ValueError):
            base64_body_bytes(bad)


@pytest.mark.parametrize("payload_limit,input_bytes,accepted", [
    (4194304, 2097152, True),
    (2796204, 2097152, True),
    (2796203, 2097152, False),
    (2097152, 2097152, False),
    (1400000, 1000000, True),
    (1333335, 1000000, False),
])
def test_api_payload_limit_must_hold_base64_input(payload_limit, input_bytes, accepted):
    raw = json.dumps(api_configuration(payload_limit, input_bytes))
    if accepted:
        parsed = AwsSettings.parse(raw, "api")
        assert parsed.role_settings.payload_limit == payload_limit
        assert parsed.role_settings.storage.input_bytes == input_bytes
        return
    with pytest.raises(ValueError) as raised:
        AwsSettings.parse(raw, "api")
    assert str(raised.value) == "Invalid explicit AWS journey configuration."
    assert raised.value.__cause__ is None
    assert str(payload_limit) not in str(raised.value)


def test_worker_role_is_not_given_a_payload_relation():
    config = configuration("worker")
    config["storage"]["input_bytes"] = 2097152
    config["storage"]["artifact_bytes"] = 8000000
    assert AwsSettings.parse(json.dumps(config), "worker").role == "worker"


def test_role_bundle_rejects_payload_below_encoded_input():
    configs = documents()
    configs["api"]["api"]["payload_limit"] = base64_body_bytes(configs["api"]["storage"]["input_bytes"]) - 1
    with pytest.raises(BundleError, match="ROLE_CONFIGURATION_INVALID"):
        check(configs)


# ---------------------------------------------------------------------------
# Q18: provider rejects empty strings that course_response requires non-empty.

def test_empty_course_name_is_rejected_at_provider():
    _, bundle = source_bundle()
    course = parse_json(bundle.course_json)
    course["courseName"] = ""
    with pytest.raises(CourseError) as raised:
        validate_bundle(replace(bundle, course_json=course), fixture_course_settings())
    assert raised.value.code == "UPSTREAM_CONTRACT_MISMATCH"


@pytest.mark.parametrize("index", range(5))
def test_empty_placement_title_is_rejected_at_provider(index):
    _, bundle = source_bundle()
    placement = bundle.placements[index]
    detail = parse_json(placement.detail_json)
    detail["title"] = ""
    placements = list(bundle.placements)
    placements[index] = replace(placement, detail_json=detail)
    with pytest.raises(CourseError) as raised:
        validate_bundle(replace(bundle, placements=tuple(placements)), fixture_course_settings())
    assert raised.value.code == "UPSTREAM_CONTRACT_MISMATCH"


def test_non_empty_texts_still_pass_including_single_space():
    _, bundle = source_bundle()
    course = parse_json(bundle.course_json)
    course["courseName"] = " "
    assert parse_json(validate_bundle(replace(bundle, course_json=course),
                                      fixture_course_settings()).course_json)["courseName"] == " "


def _empty_course_name(bundle):
    course = parse_json(bundle.course_json)
    course["courseName"] = ""
    return replace(bundle, course_json=course)


def _empty_first_title(bundle):
    placements = list(bundle.placements)
    detail = parse_json(placements[0].detail_json)
    detail["title"] = ""
    placements[0] = replace(placements[0], detail_json=detail)
    return replace(bundle, placements=tuple(placements))


class MutatingProvider:
    """Upstream snapshot changed after a good refresh; validated like a provider."""

    def __init__(self, source, mutate):
        self.source, self.mutate = source, mutate

    def resolve_learner(self, auth):
        return self.source.resolve_learner(auth)

    def list_assignments(self, learner):
        return self.source.list_assignments(learner)

    def fetch_bundle(self, binding):
        return validate_bundle(self.mutate(self.source.fetch_bundle(binding)), fixture_course_settings())


def test_fixture_document_with_empty_course_name_is_rejected_on_fetch():
    document = deepcopy(BUNDLE_DOC)
    document["course_json"]["courseName"] = ""
    with pytest.raises(CourseError) as raised:
        source_bundle(document)
    assert raised.value.code == "UPSTREAM_CONTRACT_MISMATCH"


@pytest.mark.parametrize("mutate", [_empty_course_name, _empty_first_title], ids=["courseName", "placement_title"])
def test_refresh_with_empty_title_waits_and_keeps_previous_list(mutate):
    good, _ = source_bundle()
    repo, store, _ = repository()
    auth = seed_auth(store)
    first = CourseService(good, repo).refresh_for_session(auth)
    assert first.gates and [gate.state for gate in first.gates] == ["ready"] * len(first.gates)
    before = CourseService(good, repo).list_courses(auth, page=1, page_size=20)

    bad = MutatingProvider(good, mutate)
    result = CourseService(bad, repo).refresh_for_session(auth)
    assert result.inventory.state == "ready"
    assert len(result.gates) == len(first.gates)
    assert all(gate.state == "waiting" and gate.reason == "arc_progress_unavailable" for gate in result.gates)
    listed = CourseService(bad, repo).list_courses(auth, page=1, page_size=20)
    assert [row["courseName"] for row in listed["results"]] == [row["courseName"] for row in before["results"]]
    assert all(row["courseName"] for row in listed["results"])
    assert all(row["learningAvailability"] == {"state": "waiting", "reason": "arc_progress_unavailable"}
               for row in listed["results"])

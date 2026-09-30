"""P4 extraction: the shared key/layout/binding/version/validation modules keep the old surfaces.

tests/test_storage_contract_characterization.py and
tests/test_legacy_layout_characterization.py pin the behavior before and after
the extraction; tests/test_write_request_baseline.py pins every DynamoDB write.
This file checks the new single definitions against independently written
expectations, that each old name is kept as an alias of the same object (so
imports and identity-based checks do not change), and that call sites keep the
monkeypatch points they had.
"""

from copy import deepcopy
import json
import math

import pytest

from mock_journey import storage_keys


H = "a" * 64
P = "b" * 64


# -- storage_keys --------------------------------------------------------------------

@pytest.mark.parametrize("function,args,expected", [
    ("row_key", ("JOB", "j", "STATE"), {"PK": "JOB#j", "SK": "STATE"}),
    ("session_key", ("s",), {"PK": "SESSION#s", "SK": "AUTH"}),
    ("user_key", ("u",), {"PK": "USER#u", "SK": "STATE"}),
    ("attempt_key", ("a",), {"PK": "ATTEMPT#a", "SK": "META"}),
    ("job_key", ("j",), {"PK": "JOB#j", "SK": "STATE"}),
    ("outbox_key", ("j",), {"PK": "OUTBOX#j", "SK": "DISPATCH"}),
    ("course_head_key", (H, "e"), {"PK": f"COURSE#{H}", "SK": "EPOCH#e#HEAD"}),
    ("course_final_key", (H, "e"), {"PK": f"COURSE#{H}", "SK": "EPOCH#e#FINAL"}),
    ("course_item_key", (H, "e", P), {"PK": f"COURSE#{H}", "SK": f"EPOCH#e#ITEM#{P}"}),
    ("course_start_key", (H, "e", "st"), {"PK": f"COURSE#{H}", "SK": "EPOCH#e#START#st"}),
    ("course_report_key", (H, "e", "st", "r"), {"PK": f"COURSE#{H}", "SK": "EPOCH#e#REPORT#st#r"}),
    ("learner_head_key", (H, "e"), {"PK": f"COURSE_LEARNER#{H}", "SK": "EPOCH#e#HEAD"}),
    ("start_locator_key", ("st",), {"PK": "COURSE_START#st", "SK": "META"}),
    ("create_receipt_key", ("s", "q"), {"PK": "SESSION#s", "SK": "COURSE_CREATE#q"}),
    ("principal_locator_key", ("u", "e"), {"PK": "COURSE_PRINCIPAL#u", "SK": "EPOCH#e"}),
    ("submission_key", ("a", H), {"PK": "SUBMISSION#a", "SK": f"RESULT#{H}"}),
])
def test_storage_key_functions(function, args, expected):
    key = getattr(storage_keys, function)(*args)
    assert key == expected and list(key) == ["PK", "SK"]
    assert storage_keys.pair(key) == (expected["PK"], expected["SK"])


def test_course_pk_due_partition_and_non_string_parts():
    assert storage_keys.course_pk(H) == f"COURSE#{H}"
    assert (storage_keys.due_partition("JOB"), storage_keys.due_partition("OUTBOX")) == ("DUE#JOB", "DUE#OUTBOX")
    assert storage_keys.course_head_key(H, 3) == {"PK": f"COURSE#{H}", "SK": "EPOCH#3#HEAD"}
    assert storage_keys.row_key("USER", None, "STATE") == {"PK": "USER#None", "SK": "STATE"}


def test_old_key_aliases_are_gone_from_state_and_course_state():
    from mock_journey import course_state, state
    assert not hasattr(state, "_key")  # The state-side alias of row_key is gone; callers use storage_keys.
    for old in ("_session_key", "_user_key", "_learner_key_row", "_head_key", "_final_key", "_item_key",
                "_start_key", "_report_key", "_locator_key", "_create_key", "_attempt_key", "_principal_key"):
        assert not hasattr(course_state, old), old
    assert not hasattr(course_state, "storage_keys")


def test_storage_keys_and_course_modules_do_not_import_state():
    import ast
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "mock_journey"
    for name in ("storage_keys.py", "course_state.py", "course_records.py"):
        tree = ast.parse((root / name).read_text())
        imported = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        imported |= {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        assert "mock_journey.state" not in imported, name
    assert not [node for node in ast.walk(ast.parse((root / "storage_keys.py").read_text()))
                if isinstance(node, (ast.Import, ast.ImportFrom))]


def test_course_record_helpers_are_re_exported_objects():
    from mock_journey import course_records, course_state
    for name in ("bundle_record", "bundle_from_record", "assignment_record", "assignment_from_record",
                 "scope_identity_list", "start_request_digest", "report_request_digest"):
        assert getattr(course_state, name) is getattr(course_records, name), name
    for name in ("_json_field", "_parse_field", "_rfc3339", "_placement_record", "_unavailable"):
        assert callable(getattr(course_records, name)), name
        assert not hasattr(course_state, name), name


# -- state kernel ---------------------------------------------------------------------

def test_state_public_aliases_are_the_same_objects():
    from mock_journey import state
    assert state.encode_item is state._encode and state.decode_item is state._decode
    assert state.unavailable is state._unavailable and state.ReadConflict is state._ReadConflict


class _Recorder:
    def __init__(self):
        self.calls = []

    def transact_write_items(self, **request):
        self.calls.append(request)


def test_public_kernel_methods_delegate_through_instance_patch_points():
    from mock_journey.state import DynamoStateRepository
    repository = DynamoStateRepository(_Recorder(), "t", clock=lambda: 7.9)
    assert repository.now() == 7
    seen = []
    repository._write = lambda actions: seen.append(actions) or "patched"
    assert repository.write([{"x": 1}]) == "patched" and seen == [[{"x": 1}]]
    repository._now = lambda: 99
    assert repository.now() == 99
    assert repository.put_action({"PK": "A#1", "SK": "S"}) == {"Put": {
        "TableName": "t", "Item": {"PK": {"S": "A#1"}, "SK": {"S": "S"}},
        "ConditionExpression": "attribute_not_exists(PK)"}}


def test_extend_condition_appends_in_place_with_the_same_encoding():
    from mock_journey.state import DynamoStateRepository, extend_condition
    repository = DynamoStateRepository(_Recorder(), "t")
    old = {"revision": 1, "bound_session_id": "s", "principal": "p", "state": "created"}
    action = repository.attempt_action(old, {"PK": "ATTEMPT#a", "SK": "META", **old})
    names, values = action["Put"]["ExpressionAttributeNames"], action["Put"]["ExpressionAttributeValues"]
    returned = extend_condition(action, "#digest = :digest", {"#digest": "resume_digest"}, {":digest": H})
    assert returned is action
    assert action["Put"]["ExpressionAttributeNames"] is names and action["Put"]["ExpressionAttributeValues"] is values
    assert action["Put"]["ConditionExpression"] == (
        "#r = :revision AND #b = :bound AND #p = :principal AND #s = :state AND #digest = :digest")
    assert list(names) == ["#r", "#b", "#p", "#s", "#digest"] and names["#digest"] == "resume_digest"
    assert list(values)[-1] == ":digest" and values[":digest"] == {"S": H}
    without_values = extend_condition(repository.attempt_action(old, {"PK": "A", "SK": "M", **old}),
                                      "attribute_not_exists(#input)", {"#input": "input_digest"})
    assert without_values["Put"]["ConditionExpression"].endswith(" AND attribute_not_exists(#input)")
    assert list(without_values["Put"]["ExpressionAttributeValues"]) == [":revision", ":bound", ":principal", ":state"]
    chart = extend_condition({"Put": {"ConditionExpression": "#r = :r", "ExpressionAttributeNames": {"#r": "r"},
                                      "ExpressionAttributeValues": {":r": {"N": "1"}}}},
                             "#chart = :chart", {"#chart": "chart_snapshot"}, {":chart": {"kind": "unset", "revision": 0}})
    assert chart["Put"]["ExpressionAttributeValues"][":chart"] == {
        "M": {"kind": {"S": "unset"}, "revision": {"N": "0"}}}


def test_legacy_slot_helpers():
    from mock_journey.catalog import definition_key
    from mock_journey.state import empty_legacy_slot, legacy_slot_key
    attempt = {"program_id": "mock-cpr", "target": "infant"}
    assert legacy_slot_key(attempt) == "mock-cpr:infant" == definition_key("mock-cpr", "infant")
    first, second = empty_legacy_slot(), empty_legacy_slot()
    assert first == {"completed": False, "completed_by_attempt": None, "completed_at": None, "open_attempts": 0}
    assert first is not second


def _user(open_attempts=1):
    return {"principal": "p", "epoch": "e", "revision": 4, "updated_at": 1,
            "slots": {"mock-cpr:adult": {"completed": False, "completed_by_attempt": None, "completed_at": None,
                                         "open_attempts": open_attempts}}}


@pytest.mark.parametrize("attempt,user,expected", [
    ({"epoch": "other", "active_counted": True}, _user(), None),
    ({"epoch": "e", "active_counted": False}, _user(), None),
    ({"epoch": "e", "active_counted": 0, "program_id": "x", "target": "y"}, _user(), None),
    ({"epoch": "e", "active_counted": True, "program_id": "mock-cpr", "target": "adult"}, _user(0), "TEMPORARILY_UNAVAILABLE"),
    ({"epoch": "e", "active_counted": True, "program_id": "mock-cpr", "target": "child"}, _user(), "TEMPORARILY_UNAVAILABLE"),
    ({"epoch": "e", "active_counted": True, "program_id": "mock-cpr", "target": "adult"}, _user(2), 1),
])
def test_close_open_attempt_matrix(attempt, user, expected):
    # jobs.py calls this public kernel method directly (finalize, failure and seal write-sets).
    from mock_journey.errors import JourneyError
    from mock_journey.state import DynamoStateRepository
    state = DynamoStateRepository(_Recorder(), "t")
    before = deepcopy(user)
    for close in (lambda: state.close_open_attempt(attempt, user, 50),):
        if expected is None:
            assert close() is None
        elif type(expected) is str:
            with pytest.raises(JourneyError) as error:
                close()
            assert error.value.code == expected
        else:
            changed = close()
            assert changed["slots"]["mock-cpr:adult"]["open_attempts"] == expected
            assert (changed["revision"], changed["updated_at"]) == (5, 50)
            assert {key: value for key, value in changed.items() if key not in ("slots", "revision", "updated_at")} == {
                "principal": "p", "epoch": "e"}
        assert user == before


# -- bindings and versions --------------------------------------------------------------

def test_binding_helpers_are_one_definition():
    from mock_journey import calculation, contracts, internal_calculator, jobs, storage, worker
    assert worker.input_binding is contracts.input_binding and worker.call_binding is contracts.call_binding
    assert calculation.call_binding is contracts.call_binding
    assert contracts.INPUT_BINDING_FIELDS == ("attempt_id", "epoch", "input_digest", "adapter_version",
                                              "projection_version")
    assert contracts.CALL_BINDING_FIELDS == contracts.INPUT_BINDING_FIELDS + ("job_id", "call_id")
    assert jobs._BINDING == contracts.CALL_BINDING_FIELDS
    assert storage._INPUT_BINDING == frozenset(contracts.INPUT_BINDING_FIELDS)
    assert storage._CALL_BINDING == internal_calculator._BINDING == frozenset(contracts.CALL_BINDING_FIELDS)


def test_version_registry_helpers():
    from mock_journey import contracts, execution_definitions
    assert execution_definitions.PROJECTION_VERSION is contracts.PROJECTION_VERSION == "arc-local-projection-v1"
    assert contracts.CURRENT_ADAPTER_VERSION == "arc-internal-detection-v4"
    assert contracts.RETAINED_ADAPTER_VERSIONS == ("arc-local-calculator-pending-v2", "arc-internal-detection-pending-v3")
    assert contracts.VERIFY_ONLY_ADAPTER_VERSIONS == frozenset({"arc-local-calculator-pending-v2"})
    for version, versioned, retained, verify_only in (
            ("arc-internal-detection-v4", True, False, False),
            ("arc-internal-detection-pending-v3", True, True, False),
            ("arc-local-calculator-pending-v2", True, True, True),
            ("v1", False, False, False), (None, False, False, False)):
        assert contracts.is_versioned_goal({"adapter_version": version}) is versioned
        assert contracts.is_versioned_adapter(version) is versioned
        assert contracts.is_retained_adapter(version) is retained
        assert contracts.is_verify_only_adapter(version) is verify_only
    assert contracts.is_versioned_goal({}) is False
    kinds = ("cycles", "compressions", "ventilations")
    assert [contracts.expected_goal_status(kind, "arc-internal-detection-v4") for kind in kinds] == [
        "evaluated", "evaluated", "evaluated"]
    for pending in ("arc-internal-detection-pending-v3", "arc-local-calculator-pending-v2"):
        assert [contracts.expected_goal_status(kind, pending) for kind in kinds] == [
            "pending_policy", "evaluated", "evaluated"]
        assert contracts.expected_profile_version(pending) == "tester-goal-pending-v2"
    assert [contracts.expected_goal_status(kind, "v1") for kind in kinds] == [None, None, None]
    assert contracts.expected_profile_version("arc-internal-detection-v4") == "tester-goal-cycles-v1"
    assert contracts.expected_profile_version("v1") is None


def test_definition_key_sets_match_the_course_contract_tuples():
    from mock_journey import assembly, course_contracts, projection
    assert projection.CONDITION_FIELDS == course_contracts.CONDITION_KEYS
    assert projection.DEFINITION_KEYS == course_contracts.EXECUTION_KEYS
    assert frozenset(projection.PROFILE_SECTIONS) == course_contracts.CALCULATION_PROFILE_KEYS
    assert projection.DEFINITION_CORE_KEYS == ("condition", "calculation_profile", "profile_version",
                                               "adapter_version", "projection_version")
    assert projection.definition_core_schema() == assembly._DEFINITION_SCHEMA
    assert projection.project is projection._project
    assert projection.CONDITION_SCHEMA is projection._CONDITION and projection.DOCUMENT_SCHEMA is projection._DOCUMENT
    schema = projection.definition_core_schema()
    assert schema["condition"] is projection._CONDITION
    assert all(schema["calculation_profile"][key] is projection._DOCUMENT[key] for key in projection.PROFILE_SECTIONS)


# -- named constants ---------------------------------------------------------------------

def test_dummy_principal_and_finalize_limit_constants():
    from mock_journey import jobs
    from mock_journey.auth import PRINCIPAL
    assert jobs.FINALIZE_MAX_TRANSACTION_ACTIONS == 20
    assert jobs._is_dummy({"principal": PRINCIPAL}) is True
    assert jobs._is_dummy({"principal": "dummy-tester "}) is False and jobs._is_dummy({}) is False


@pytest.mark.parametrize("limit,writes", [(2, False), (3, True)])
def test_finalize_limit_is_the_named_constant(monkeypatch, limit, writes):
    """A legacy finalize writes three actions (JOB, ATTEMPT, USER)."""
    from mock_journey import jobs
    from mock_journey.errors import JourneyError
    from tests.job_restart_support import (
        DEFINITION, FINAL_REF, attempt_row, close_calls, evaluation, finalize_job, publication, repo, user_row,
    )
    monkeypatch.setattr(jobs, "FINALIZE_MAX_TRANSACTION_ACTIONS", limit)
    job = finalize_job(DEFINITION)
    repository, client = repo(close_calls(job, attempt_row(definition_json=DEFINITION), user_row(), write=writes))
    call = lambda: repository.finalize("job", "worker", 2, FINAL_REF, evaluation(met=True, passed=True),
                                       publication(job))
    if writes:
        assert call()["state"] == "evaluated"
        assert [operation for operation, _ in client.calls] == ["get", "read", "write"]
    else:
        with pytest.raises(JourneyError) as error:
            call()
        assert error.value.code == "TEMPORARILY_UNAVAILABLE"


# -- validation primitives and AWS scope ---------------------------------------------------

@pytest.mark.parametrize("value,number,finite,outcome", [
    (1, False, False, 1), (True, False, False, "error"), (1.0, False, False, "error"), (0, False, False, "error"),
    (1.5, True, False, 1.5), (math.nan, True, False, "nan"), (math.nan, True, True, "error"),
    (math.inf, True, False, math.inf),
    (math.inf, True, True, "error"), (10 ** 400, False, False, 10 ** 400), (10 ** 400, False, True, OverflowError),
])
def test_require_positive(value, number, finite, outcome):
    from mock_journey.settings import require_positive

    class Marker(Exception):
        pass

    if outcome == "error":
        with pytest.raises(Marker):
            require_positive(value, Marker, number=number, finite=finite)
    elif outcome == "nan":  # Without finite, NaN passes "<= 0" (only finite callers reject it).
        assert math.isnan(require_positive(value, Marker, number=number, finite=finite))
    elif outcome is OverflowError:
        with pytest.raises(OverflowError):
            require_positive(value, Marker, number=number, finite=finite)
    else:
        assert require_positive(value, Marker, number=number, finite=finite) == outcome


def test_named_bounds_and_lease_rule():
    from mock_journey import settings
    assert (settings.MAX_CONFLICT_RETRIES, settings.MAX_ENVIRONMENT_LENGTH) == (8, 128)
    assert settings.check_queue_url is settings._queue_url
    assert settings.lease_renewal_exceeds(60, 20, 39) is False
    assert settings.lease_renewal_exceeds(60, 20.5, 1) is True and settings.lease_renewal_exceeds(60, 20, 40) is True


def test_aws_scope_helpers():
    import hashlib
    import re
    from mock_journey import aws_scope
    assert aws_scope.PARTITIONS == ("aws", "aws-cn", "aws-us-gov")
    assert aws_scope.environment_namespace("OPS#", "dev") == "OPS#" + hashlib.sha256(b"dev").hexdigest()
    for partition, region, ok in (("aws", "us-east-2", True), ("aws", "cn-north-1", False),
                                  ("aws-cn", "cn-north-1", True), ("aws-cn", "us-east-2", False),
                                  ("aws-us-gov", "us-gov-west-1", True), ("aws", "us-gov-west-1", False),
                                  ("aws-iso", "us-east-2", True)):
        assert aws_scope.partition_matches_region(partition, region) is ok
    assert re.fullmatch(aws_scope.REGION_PATTERN, "ap-northeast-2")
    from mock_journey.settings import MAX_ENVIRONMENT_LENGTH
    assert re.fullmatch(aws_scope.ENVIRONMENT_PATTERN, "e" * MAX_ENVIRONMENT_LENGTH)
    assert not re.fullmatch(aws_scope.ENVIRONMENT_PATTERN, "e" * (MAX_ENVIRONMENT_LENGTH + 1))
    assert not re.fullmatch(aws_scope.ACCOUNT_ID_PATTERN, "1" * 13)
    # One 128: the settings bound is the aws_scope bound and the pattern is spelled from it.
    assert MAX_ENVIRONMENT_LENGTH is aws_scope.MAX_ENVIRONMENT_LENGTH == 128
    assert aws_scope.ENVIRONMENT_PATTERN == r"[A-Za-z0-9_.-]{1,128}"


# -- stored course_binding -> dict (one reader for state and jobs) -----------------------

BINDING_ROW = {"scope_key": H, "placement_key": P, "start_role": "training", "definition_hash": "c" * 64,
               "content_version": "arc-dummy-dev-v1", "epoch": "e-1", "policy_version": "vcc-policy-v1"}


def test_binding_from_row_field_order_and_legacy_rows():
    from mock_journey.course_contracts import COURSE_BINDING_FIELDS, binding_from_row
    assert COURSE_BINDING_FIELDS == ("scope_key", "placement_key", "start_role", "definition_hash",
                                     "content_version", "epoch", "policy_version")
    shuffled = {key: BINDING_ROW[key] for key in reversed(COURSE_BINDING_FIELDS)}
    for flag in (True, False):
        bound = binding_from_row({"attempt_id": "a", "course_binding": shuffled}, null_is_legacy=flag)
        assert bound == BINDING_ROW and list(bound) == list(COURSE_BINDING_FIELDS)
        assert bound is not shuffled
        assert binding_from_row({"attempt_id": "a"}, null_is_legacy=flag) is None
    # A stored null: legacy for jobs (null_is_legacy), an unusable value for state.
    assert binding_from_row({"course_binding": None}, null_is_legacy=True) is None
    with pytest.raises(ValueError):
        binding_from_row({"course_binding": None}, null_is_legacy=False)


@pytest.mark.parametrize("value", [
    "text", 7, [BINDING_ROW], {**BINDING_ROW, "extra": "x"}, {k: v for k, v in BINDING_ROW.items() if k != "epoch"}, {},
], ids=["str", "int", "list", "extra_field", "missing_field", "empty"])
def test_binding_from_row_rejects_incomplete_values_with_value_error(value):
    from mock_journey.course_contracts import binding_from_row
    for flag in (True, False):
        with pytest.raises(ValueError):
            binding_from_row({"course_binding": value}, null_is_legacy=flag)


def test_binding_from_row_keeps_course_binding_field_errors():
    from mock_journey.course_errors import CourseError
    from mock_journey.course_contracts import binding_from_row
    with pytest.raises(CourseError):
        binding_from_row({"course_binding": {**BINDING_ROW, "start_role": "other"}}, null_is_legacy=True)


def test_jobs_maps_an_unusable_binding_to_stored_input_invalid_and_state_to_unavailable():
    from mock_journey.errors import JourneyError
    from mock_journey.jobs import DynamoJobRepository
    from mock_journey.state import DynamoStateRepository
    from tests.job_restart_support import attempt_row, started_job
    from tests.mock_state_support import ScriptedClient, item, snapshot
    job = started_job()
    for value, expected in ((BINDING_ROW, True), ("text", "STORED_INPUT_INVALID")):
        attempt = attempt_row(course_binding=value)
        client = ScriptedClient([("get", {"Item": item(job)}), ("read", snapshot(job, attempt))])
        jobs = DynamoJobRepository(DynamoStateRepository(client, "t", clock=lambda: 20))
        if expected is True:
            assert jobs.is_course_job("job") is True
        else:
            with pytest.raises(JourneyError) as error:
                jobs.is_course_job("job")
            assert error.value.code == expected
    # The state reader answers TEMPORARILY_UNAVAILABLE for the same value (cancel path, fail-closed).
    from mock_journey import state
    with pytest.raises(JourneyError) as error:
        state._optional_course_binding({"course_binding": "text"})
    assert error.value.code == "TEMPORARILY_UNAVAILABLE"
    assert state._optional_course_binding({"course_binding": BINDING_ROW}) == BINDING_ROW


def test_strict_loads_options():
    from mock_journey.typed import strict_loads

    class Duplicate(Exception):
        pass

    class Nonfinite(Exception):
        pass

    assert strict_loads(b'{"a": [1, {"b": null}]}', duplicate=Duplicate) == {"a": [1, {"b": None}]}
    with pytest.raises(Duplicate):
        strict_loads('{"a": 1, "a": 2}', duplicate=Duplicate)
    with pytest.raises(Duplicate):
        strict_loads('[{"x": 1, "x": 1}]', duplicate=Duplicate, nonfinite=Nonfinite)
    assert math.isnan(strict_loads('{"a": NaN}', duplicate=Duplicate)["a"])
    with pytest.raises(Nonfinite):
        strict_loads('{"a": -Infinity}', duplicate=Duplicate, nonfinite=Nonfinite)
    with pytest.raises(json.JSONDecodeError):
        strict_loads("{", duplicate=Duplicate, nonfinite=Nonfinite)


def test_named_timing_and_execution_tuples():
    from mock_journey.aws_settings import AwsSettings, ExecutionVersions, RelayTiming, WorkerTiming
    from tests.aws_runtime_support import configuration
    worker = AwsSettings.parse(json.dumps(configuration("worker")), "worker")
    assert type(worker.timing) is WorkerTiming and worker.timing == (0.05, 0.1, 500)
    assert (worker.timing.renewal_interval_seconds, worker.timing.renewal_timeout_seconds,
            worker.timing.processing_reserve_ms) == (0.05, 0.1, 500)
    relay = AwsSettings.parse(json.dumps(configuration("relay")), "relay")
    assert type(relay.timing) is RelayTiming and relay.timing.processing_reserve_ms == relay.timing[0] == 500
    assert type(worker.execution) is ExecutionVersions
    assert worker.execution._fields == ("current_adapter_version", "projection_version", "retained_adapter_versions")


# -- legacy layout -----------------------------------------------------------------------

def test_legacy_layout_rules():
    from mock_journey import internal_calculator
    from util import legacy_layout
    assert legacy_layout.object_base("d/x", "dev", "_no_org", "2027-01-15", "CPR-ACTION-1800000000-u") == (
        "d/x/dev/_no_org/2027-01-15/CPR-ACTION-1800000000-u")
    assert legacy_layout.stage_prefix("d/x", "dev") == "d/x/dev/"
    assert (legacy_layout.RAW_SUFFIX, legacy_layout.META_SUFFIX, legacy_layout.AED_SUFFIX,
            legacy_layout.CHART_SUFFIX) == (".bin", ".meta.json", ".aed.bin", ".json")
    assert internal_calculator._STEM is legacy_layout.KEY_STEM


def test_legacy_layout_stays_free_of_sdk_imports():
    import ast
    from pathlib import Path
    tree = ast.parse((Path(__file__).resolve().parents[1] / "util" / "legacy_layout.py").read_text())
    imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    imported |= {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert imported == {"re"}


def test_ported_uploader_keeps_its_preflight_literals():
    """scripts/deployment_preflight.py reads these top-level literal assignments by AST."""
    import ast
    from pathlib import Path
    tree = ast.parse((Path(__file__).resolve().parents[1] / "util" / "uploader.py").read_text())
    literals = {target.id: node.value.value for node in tree.body if isinstance(node, ast.Assign)
                for target in node.targets if isinstance(target, ast.Name) and isinstance(node.value, ast.Constant)}
    assert literals["BUCKET"] == "brayden-online-v2-api-storage"
    assert literals["RTDATA_DIRECTORY"] == "calculator_result/interpreted_rtdata/arc"


def test_dormant_storage_binding_default_is_gone():
    from mock_journey import storage
    assert not hasattr(storage, "LegacyBindings")

"""P4 characterization: storage keys, bindings, definition keys, versions and settings primitives.

These tests pin the behavior that the P4 extraction (storage key and legacy
layout modules, binding/definition/version constants, shared validation
primitives) must keep. Expected values are written out independently of the
code under test (literal key strings, hand-built canonical digests, explicit
accept/reject matrices); nothing is compared with itself. The DynamoDB request
shapes are covered separately by tests/test_write_request_baseline.py.
"""

from copy import deepcopy
import hashlib
import json
import math

import pytest

from mock_journey import course_submission, state, storage_keys
from mock_journey.course_contracts import CourseBinding


H = "a" * 64
P = "b" * 64


# -- storage keys (ARCHITECTURE §4) ------------------------------------------------

STORAGE_KEYS = [
    ("session_key", ("s-1",), {"PK": "SESSION#s-1", "SK": "AUTH"}),
    ("user_key", ("dummy-tester",), {"PK": "USER#dummy-tester", "SK": "STATE"}),
    ("learner_head_key", (H, "e-1"), {"PK": f"COURSE_LEARNER#{H}", "SK": "EPOCH#e-1#HEAD"}),
    ("course_head_key", (H, "e-1"), {"PK": f"COURSE#{H}", "SK": "EPOCH#e-1#HEAD"}),
    ("course_final_key", (H, "e-1"), {"PK": f"COURSE#{H}", "SK": "EPOCH#e-1#FINAL"}),
    ("course_item_key", (H, "e-1", P), {"PK": f"COURSE#{H}", "SK": f"EPOCH#e-1#ITEM#{P}"}),
    ("course_start_key", (H, "e-1", "st-1"), {"PK": f"COURSE#{H}", "SK": "EPOCH#e-1#START#st-1"}),
    ("course_report_key", (H, "e-1", "st-1", "r-1"), {"PK": f"COURSE#{H}", "SK": "EPOCH#e-1#REPORT#st-1#r-1"}),
    ("start_locator_key", ("st-1",), {"PK": "COURSE_START#st-1", "SK": "META"}),
    ("create_receipt_key", ("s-1", "q-1"), {"PK": "SESSION#s-1", "SK": "COURSE_CREATE#q-1"}),
    ("attempt_key", ("a-1",), {"PK": "ATTEMPT#a-1", "SK": "META"}),
    ("principal_locator_key", ("dummy-tester", "e-1"), {"PK": "COURSE_PRINCIPAL#dummy-tester", "SK": "EPOCH#e-1"}),
]


@pytest.mark.parametrize("name,args,expected", STORAGE_KEYS, ids=[case[0] for case in STORAGE_KEYS])
def test_storage_keys_are_exact_pk_sk_dicts(name, args, expected):
    value = getattr(storage_keys, name)(*args)
    assert value == expected
    assert list(value) == ["PK", "SK"]


def test_non_string_key_parts_use_plain_f_string_formatting():
    # Stored epochs are strings; the formatting of other values is part of the
    # f-string contract (str()), never a join or a repr.
    assert storage_keys.course_head_key(H, 7) == {"PK": f"COURSE#{H}", "SK": "EPOCH#7#HEAD"}
    assert storage_keys.course_item_key(H, None, P) == {"PK": f"COURSE#{H}", "SK": f"EPOCH#None#ITEM#{P}"}
    assert storage_keys.user_key(True) == {"PK": "USER#True", "SK": "STATE"}


@pytest.mark.parametrize("kind,value,suffix,expected", [
    ("SESSION", "s-1", "AUTH", {"PK": "SESSION#s-1", "SK": "AUTH"}),
    ("USER", "dummy-tester", "STATE", {"PK": "USER#dummy-tester", "SK": "STATE"}),
    ("ATTEMPT", "a-1", "META", {"PK": "ATTEMPT#a-1", "SK": "META"}),
    ("JOB", "j-1", "STATE", {"PK": "JOB#j-1", "SK": "STATE"}),
    ("OUTBOX", "j-1", "DISPATCH", {"PK": "OUTBOX#j-1", "SK": "DISPATCH"}),
])
def test_state_row_key(kind, value, suffix, expected):
    from mock_journey.storage_keys import row_key
    key = row_key(kind, value, suffix)
    assert key == expected and list(key) == ["PK", "SK"]


def test_course_submission_keys_are_tuples_in_head_item_final_order():
    binding = CourseBinding(H, P, "final_assessment", "c" * 64, "v1", "e-1", "vcc-policy-v1")
    keys = course_submission._course_keys(binding, "e-1")
    assert list(keys) == ["head", "item", "final"]
    assert keys == {"head": (f"COURSE#{H}", "EPOCH#e-1#HEAD"),
                    "item": (f"COURSE#{H}", f"EPOCH#e-1#ITEM#{P}"),
                    "final": (f"COURSE#{H}", "EPOCH#e-1#FINAL")}
    assert all(type(value) is tuple for value in keys.values())


# -- input/call binding ------------------------------------------------------------

INPUT_FIELDS = ["attempt_id", "epoch", "input_digest", "adapter_version", "projection_version"]
CALL_FIELDS = INPUT_FIELDS + ["job_id", "call_id"]


def _job():
    return {"PK": "JOB#j", "SK": "STATE", "job_id": "j", "call_id": "c", "attempt_id": "a", "epoch": "e",
            "input_digest": H, "adapter_version": "av", "projection_version": "pv", "state": "running"}


def test_worker_binding_helpers_keep_field_order_and_ignore_other_fields():
    from mock_journey.worker import call_binding, input_binding
    job = _job()
    assert list(input_binding(job).items()) == [(key, job[key]) for key in INPUT_FIELDS]
    assert list(call_binding(job).items()) == [(key, job[key]) for key in CALL_FIELDS]
    missing = dict(job)
    del missing["call_id"]
    assert list(input_binding(missing)) == INPUT_FIELDS
    with pytest.raises(KeyError):
        call_binding(missing)


def test_binding_field_sets_and_container_types():
    from mock_journey import internal_calculator, jobs, storage
    assert type(storage._INPUT_BINDING) is frozenset and storage._INPUT_BINDING == frozenset(INPUT_FIELDS)
    assert type(storage._CALL_BINDING) is frozenset and storage._CALL_BINDING == frozenset(CALL_FIELDS)
    assert type(jobs._BINDING) is tuple and jobs._BINDING == tuple(CALL_FIELDS)
    assert type(internal_calculator._BINDING) is frozenset and internal_calculator._BINDING == frozenset(CALL_FIELDS)


def _independent_digest(mapping):
    """typed.digest of a flat str->str dict, re-derived from the documented canonical form."""
    tagged = ["dict", [[key, ["str", mapping[key]]] for key in sorted(mapping)]]
    return hashlib.sha256(json.dumps(tagged, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()


def test_arc_binding_digest_ignores_field_order():
    from mock_journey.typed import digest
    from mock_journey.worker import call_binding
    binding = call_binding(_job())
    reordered = dict(reversed(list(binding.items())))
    assert list(reordered) != list(binding)
    assert digest(binding) == digest(reordered) == _independent_digest(binding)


# -- execution definition keys -----------------------------------------------------

SCALAR = "scalar"
PROFILE_SCHEMA = {
    "Custom": {**{key: SCALAR for key in ("CertificateAdult", "CertificateChild", "CertificateInfant",
                                         "CertificateBaby", "PassThreshold", "PassThresholdChild")},
               "TrainCourse": {"Certification": SCALAR, "StopCondition": {
                   "finish_cycle": SCALAR, "finish_compression": SCALAR, "finish_ventilation": SCALAR}}},
    "Open_Skill": {"Passing_Score": SCALAR},
    "Usage": {key: SCALAR for key in ("Type", "Regional_Option", "Email", "Comment", "validNum", "hstreamId")},
    "Organization": {key: SCALAR for key in ("org_id", "org_name", "First_name", "Last_name")},
}
CONDITION = ("mode", "target", "training_type", "guideline", "cpr_cycle_type", "is_2rescuers")


def test_assembly_definition_schema_is_the_five_key_core():
    from mock_journey.assembly import _DEFINITION_SCHEMA
    assert _DEFINITION_SCHEMA == {
        "condition": {key: SCALAR for key in CONDITION},
        "calculation_profile": PROFILE_SCHEMA,
        "profile_version": SCALAR, "adapter_version": SCALAR, "projection_version": SCALAR,
    }
    assert list(_DEFINITION_SCHEMA) == ["condition", "calculation_profile", "profile_version",
                                        "adapter_version", "projection_version"]


def test_course_contract_key_tuples_keep_their_order():
    from mock_journey.course_contracts import CALCULATION_PROFILE_KEYS, CONDITION_KEYS, EXECUTION_KEYS
    assert EXECUTION_KEYS == ("condition", "calculation_profile", "profile_version", "adapter_version",
                              "projection_version", "goal", "catalog_version")
    assert CONDITION_KEYS == CONDITION
    assert type(CALCULATION_PROFILE_KEYS) is frozenset and CALCULATION_PROFILE_KEYS == frozenset(PROFILE_SCHEMA)


def _catalog_definition():
    from mock_journey.execution_definitions import execution_catalog
    return execution_catalog().get_definition("mock-cpr", "adult")


def test_catalog_requires_exactly_the_five_key_definition():
    from mock_journey.catalog import Catalog
    from mock_journey.errors import JourneyError

    class One:
        def __init__(self, value):
            self.value = value

        def get_definition(self, program_id, target):
            return deepcopy(self.value)

    definition = _catalog_definition()
    produced = json.loads(Catalog(One(definition)).definition("mock-cpr", "adult"))
    assert set(produced) == {"condition", "calculation_profile", "profile_version", "adapter_version",
                             "projection_version", "catalog_version", "goal"}
    for change in ({"goal": {}}, {"catalog_version": "x"}):
        with pytest.raises(JourneyError) as error:
            Catalog(One({**definition, **change})).definition("mock-cpr", "adult")
        assert error.value.code == "CALCULATOR_CONTRACT_MISMATCH"
    missing = dict(definition)
    del missing["profile_version"]
    with pytest.raises(JourneyError):
        Catalog(One(missing)).definition("mock-cpr", "adult")


def _projection_inputs():
    from mock_journey.catalog import Catalog
    from mock_journey.execution_definitions import PROJECTION_VERSION, execution_catalog
    from mock_journey.projection import ProjectionSchema
    execution = execution_catalog()
    definition = json.loads(Catalog(execution).definition("mock-cpr", "adult"))
    body = {"cpr_b64_data": b"\x01" * 28, "condition": deepcopy(definition["condition"])}
    return body, definition, ProjectionSchema(PROJECTION_VERSION, {})


def test_projected_definition_accepts_the_seven_keys_and_drops_credentials_only():
    from mock_journey.errors import JourneyError
    from mock_journey.projection import project_input
    body, definition, schema = _projection_inputs()
    assert project_input(body, definition, schema).payload["definition"] == definition
    with_secret = {**definition, "access_token": "secret-marker"}
    assert project_input(body, with_secret, schema).payload["definition"] == definition
    with pytest.raises(JourneyError) as error:
        project_input(body, {**definition, "unexpected": "x"}, schema)
    assert error.value.code == "MEASUREMENT_INPUT_INVALID"
    changed = deepcopy(definition)
    changed["calculation_profile"] = {"Dummy": {}}
    with pytest.raises(JourneyError) as error:
        project_input(body, changed, schema)
    assert error.value.code == "MEASUREMENT_INPUT_INVALID"


# -- adapter/profile/projection versions ----------------------------------------------

# D136: the current adapter evaluates a cycles goal by the closed-cycle rule;
# the two pending adapters are retained (v3 still calculates, v2 verify-only).
CURRENT = "arc-internal-detection-v4"
PENDING = "arc-internal-detection-pending-v3"
RETAINED = "arc-local-calculator-pending-v2"
PROFILE = "tester-goal-pending-v2"
CYCLE_PROFILE = "tester-goal-cycles-v1"
PROJECTION = "arc-local-projection-v1"


def _resolver(*args):
    return 3


def test_version_constants():
    from mock_journey import contracts
    from mock_journey.execution_definitions import PROJECTION_VERSION
    assert contracts.CURRENT_ADAPTER_VERSION == contracts.CYCLE_GOAL_ADAPTER_VERSION == CURRENT
    assert contracts.PENDING_GOAL_ADAPTER_VERSION == PENDING
    assert contracts.RETAINED_PENDING_GOAL_ADAPTER_VERSION == RETAINED
    assert contracts.PENDING_GOAL_ADAPTER_VERSIONS == frozenset({PENDING, RETAINED})
    assert type(contracts.PENDING_GOAL_ADAPTER_VERSIONS) is frozenset
    assert contracts.VERSIONED_GOAL_ADAPTER_VERSIONS == frozenset({CURRENT, PENDING, RETAINED})
    assert contracts.RETAINED_ADAPTER_VERSIONS == (RETAINED, PENDING)
    assert contracts.VERIFY_ONLY_ADAPTER_VERSIONS == frozenset({RETAINED})
    assert contracts.PENDING_GOAL_PROFILE_VERSION == PROFILE
    assert contracts.CYCLE_GOAL_PROFILE_VERSION == CYCLE_PROFILE
    assert PROJECTION_VERSION == PROJECTION


def _verified(kind, status, observed):
    from mock_journey.contracts import VerifiedCalculation
    return VerifiedCalculation({"x": 1}, kind, observed, "no_chart", goal_status=status)


@pytest.mark.parametrize("adapter,profile,kind,status,observed,expected", [
    (PENDING, PROFILE, "cycles", "pending_policy", None, ("pending_policy", False, ["GOAL_POLICY_UNRESOLVED"])),
    (RETAINED, PROFILE, "compressions", "evaluated", 60, ("evaluated", True, [])),
    (PENDING, "other-profile", "compressions", "evaluated", 60, "CALCULATOR_CONTRACT_MISMATCH"),
    (PENDING, PROFILE, "compressions", None, 60, "CALCULATOR_CONTRACT_MISMATCH"),
    # D136: the current adapter evaluates cycles (met = observed >= required).
    (CURRENT, CYCLE_PROFILE, "cycles", "evaluated", 3, ("evaluated", True, [])),
    (CURRENT, CYCLE_PROFILE, "cycles", "evaluated", 2, ("evaluated", False, ["GOAL_NOT_MET"])),
    (CURRENT, CYCLE_PROFILE, "compressions", "evaluated", 60, ("evaluated", True, [])),
    (CURRENT, CYCLE_PROFILE, "cycles", "pending_policy", None, "CALCULATOR_CONTRACT_MISMATCH"),
    (CURRENT, PROFILE, "cycles", "evaluated", 3, "CALCULATOR_CONTRACT_MISMATCH"),
    (CURRENT, CYCLE_PROFILE, "cycles", None, 3, "CALCULATOR_CONTRACT_MISMATCH"),
    ("v1", None, "compressions", None, 59, (None, False, ["GOAL_NOT_MET"])),
    ("v1", None, "compressions", "evaluated", 60, "CALCULATOR_CONTRACT_MISMATCH"),
])
def test_worker_evaluate_version_rules(monkeypatch, adapter, profile, kind, status, observed, expected):
    from mock_journey import worker
    from mock_journey.errors import JourneyError
    monkeypatch.setattr(worker, "_is_pass", lambda *args: True)
    definition = {"goal": {"kind": kind, "required": 60 if kind != "cycles" else 3},
                  "adapter_version": adapter, "condition": {"target": "adult"}}
    if profile is not None:
        definition["profile_version"] = profile
    verified = _verified(kind, status, observed)
    if type(expected) is str:
        with pytest.raises(JourneyError) as error:
            worker.evaluate({}, definition, verified)
        assert error.value.code == expected
        return
    result = worker.evaluate({}, definition, verified)
    assert (result["goal"].get("status"), result["program_completed"], result["reason_codes"]) == expected
    assert ("status" in result["goal"]) is (adapter in (CURRENT, PENDING, RETAINED))


def test_jobs_evaluation_uses_the_same_version_rules():
    from mock_journey.errors import JourneyError
    from mock_journey.jobs import DynamoJobRepository

    def attempt(adapter, profile):
        definition = {"goal": {"kind": "compressions", "required": 60}, "adapter_version": adapter}
        if profile is not None:
            definition["profile_version"] = profile
        return {"definition_json": json.dumps(definition)}

    versioned = {"goal": {"kind": "compressions", "required": 60, "observed": 60, "met": True,
                          "status": "evaluated"}, "score": {"decision": "pass"},
                 "program_completed": True, "reason_codes": []}
    plain = deepcopy(versioned)
    del plain["goal"]["status"]
    assert DynamoJobRepository.check_evaluation(versioned, attempt(PENDING, PROFILE)) == versioned
    assert DynamoJobRepository.check_evaluation(versioned, attempt(RETAINED, PROFILE)) == versioned
    assert DynamoJobRepository.check_evaluation(versioned, attempt(CURRENT, CYCLE_PROFILE)) == versioned
    assert DynamoJobRepository.check_evaluation(plain, attempt("v1", None)) == plain
    for value, row in ((versioned, attempt(PENDING, "other")), (plain, attempt(PENDING, PROFILE)),
                       (versioned, attempt(CURRENT, PROFILE)), (plain, attempt(CURRENT, CYCLE_PROFILE)),
                       (versioned, attempt("v1", None))):
        with pytest.raises(JourneyError) as error:
            DynamoJobRepository.check_evaluation(value, row)
        assert error.value.code == "CALCULATOR_CONTRACT_MISMATCH"


def test_internal_calculator_version_rules():
    from mock_journey.internal_calculator import InternalCalculator
    current = InternalCalculator(version=CURRENT, projection_version=PROJECTION, stage="dev",
                                 cycle_goal_resolver=_resolver)
    pending = InternalCalculator(version=PENDING, projection_version=PROJECTION, stage="dev",
                                 allow_pending_cycle_goal=True)
    retained = InternalCalculator(version=RETAINED, projection_version=PROJECTION, stage="dev",
                                  allow_pending_cycle_goal=True)
    assert current.can_calculate is True and pending.can_calculate is True and retained.can_calculate is False
    assert current.candidate_schema == "arc-internal-calculation-v3"
    assert pending.candidate_schema == retained.candidate_schema == "arc-internal-calculation-v2"
    other = InternalCalculator(version="arc-other-v9", projection_version=PROJECTION, stage="dev")
    assert other.can_calculate is True and other.candidate_schema == "arc-internal-calculation-v1"
    with pytest.raises(ValueError):
        InternalCalculator(version="arc-other-v9", projection_version=PROJECTION, stage="dev",
                           allow_pending_cycle_goal=True)
    with pytest.raises(ValueError):
        InternalCalculator(version=PENDING, projection_version=PROJECTION, stage="dev")
    # The cycle-goal adapter is never constructed without its resolver or as a pending adapter.
    with pytest.raises(ValueError):
        InternalCalculator(version=CURRENT, projection_version=PROJECTION, stage="dev")
    with pytest.raises(ValueError):
        InternalCalculator(version=CURRENT, projection_version=PROJECTION, stage="dev",
                           allow_pending_cycle_goal=True)


@pytest.mark.parametrize("change,accepted", [
    ({}, True),
    ({"retained_adapter_versions": []}, False),  # D127: an empty list is a configuration error.
    ({"retained_adapter_versions": [RETAINED, RETAINED]}, False),
    ({"retained_adapter_versions": [RETAINED]}, False),  # D136: partial (the pending-v3 adapter is retained too).
    ({"retained_adapter_versions": [PENDING]}, False),
    ({"retained_adapter_versions": [PENDING, RETAINED]}, False),  # Same set, other order.
    ({"retained_adapter_versions": [CURRENT]}, False),
    ({"retained_adapter_versions": [RETAINED, PENDING, CURRENT]}, False),
    ({"retained_adapter_versions": ["arc-other-v1"]}, False),
    ({"retained_adapter_versions": (RETAINED, PENDING)}, True),  # JSON has no tuple: dumps() makes it a list.
    ({"retained_adapter_versions": [1]}, False),
    ({"retained_adapter_versions": RETAINED}, False),
    ({"retained_adapter_versions": None}, False),
    ({"current_adapter_version": RETAINED}, False),
    ({"current_adapter_version": PENDING}, False),  # D136: the former current adapter is no longer current.
    ({"projection_version": "arc-local-projection-v2"}, False),
])
@pytest.mark.parametrize("role", ["api", "worker"])
def test_aws_execution_versions_matrix(role, change, accepted):
    from mock_journey.aws_settings import AwsSettings
    from tests.aws_runtime_support import configuration
    config = configuration(role)
    config["execution"].update(change)
    raw = json.dumps(config)
    if not accepted:
        with pytest.raises(ValueError, match="Invalid explicit AWS journey configuration."):
            AwsSettings.parse(raw, role)
        return
    settings = AwsSettings.parse(raw, role)
    retained = tuple(config["execution"]["retained_adapter_versions"])
    assert settings.execution == (CURRENT, PROJECTION, retained)
    current, projection, kept = settings.execution
    assert (current, projection, kept) == (CURRENT, PROJECTION, retained) and type(kept) is tuple


@pytest.mark.parametrize("supplied,accepted", [
    (["arc-old-a", "arc-old-b"], True),
    (["arc-old-b", "arc-old-a"], False),  # Same set, other order: the registry order is the contract.
    (["arc-old-a"], False),  # Partial.
    ([], False),  # Empty.
    (["arc-old-a", "arc-old-b", "arc-old-a"], False),  # Duplicate.
    (["arc-old-a", "arc-old-b", RETAINED], False),  # Superset.
], ids=["exact", "reordered", "partial", "empty", "duplicate", "superset"])
@pytest.mark.parametrize("role", ["api", "worker"])
def test_aws_retained_versions_must_equal_the_registry_exactly(monkeypatch, role, supplied, accepted):
    # D127 with a two-entry registry, so that partial/reordered lists differ from an empty one.
    from mock_journey import contracts
    from mock_journey.aws_settings import AwsSettings
    from tests.aws_runtime_support import configuration
    monkeypatch.setattr(contracts, "RETAINED_ADAPTER_VERSIONS", ("arc-old-a", "arc-old-b"))
    config = configuration(role)
    config["execution"]["retained_adapter_versions"] = supplied
    raw = json.dumps(config)
    if not accepted:
        with pytest.raises(ValueError, match=r"\AInvalid explicit AWS journey configuration\.\Z"):
            AwsSettings.parse(raw, role)
        return
    assert AwsSettings.parse(raw, role).execution.retained_adapter_versions == ("arc-old-a", "arc-old-b")


# -- positional timing/execution tuples -------------------------------------------------

def test_aws_timing_tuples_by_role():
    from mock_journey.aws_settings import AwsSettings
    from tests.aws_runtime_support import configuration
    worker = AwsSettings.parse(json.dumps(configuration("worker")), "worker")
    assert worker.timing == (0.05, 0.1, 500)
    assert (worker.timing[0], worker.timing[1], worker.timing[2]) == (0.05, 0.1, 500)
    assert isinstance(worker.timing, tuple) and len(worker.timing) == 3
    relay = AwsSettings.parse(json.dumps(configuration("relay")), "relay")
    assert relay.timing == (500,) and relay.timing[0] == 500 and len(relay.timing) == 1
    assert relay.relay_budget.reserve_ms == 500 + 40 + 20
    api = AwsSettings.parse(json.dumps(configuration("api")), "api")
    assert api.timing is None and relay.execution is None
    assert api.execution == worker.execution == (CURRENT, PROJECTION, (RETAINED, PENDING))
    assert hash(api.execution) == hash((CURRENT, PROJECTION, (RETAINED, PENDING)))
    assert api == AwsSettings.parse(json.dumps(configuration("api")), "api")
    assert json.dumps(worker.timing) == "[0.05, 0.1, 500]"


# -- settings validation primitives ---------------------------------------------------

@pytest.mark.parametrize("retries,valid", [(1, True), (8, True), (0, False), (9, False), (True, False),
                                           (4.0, False), (-1, False), (None, False)])
def test_conflict_retry_bounds(retries, valid):
    from mock_journey.settings import StateSettings
    if valid:
        assert StateSettings("t", retries).max_conflict_retries == retries
        assert state.DynamoStateRepository(object(), "t", max_conflict_retries=retries).max_conflict_retries == retries
        return
    with pytest.raises(ValueError, match=r"\AInvalid journey settings\.\Z"):
        StateSettings("t", retries)
    with pytest.raises((ValueError, TypeError)) as error:
        state.DynamoStateRepository(object(), "t", max_conflict_retries=retries)
    assert type(error.value) is ValueError and str(error.value) == "Invalid state repository configuration."


def test_state_repository_rejects_an_empty_table_name():
    with pytest.raises(ValueError, match="Invalid state repository configuration."):
        state.DynamoStateRepository(object(), "")


@pytest.mark.parametrize("values,valid", [((1,), True), ((1, 2, 3), True), ((0,), False), ((True,), False),
                                          ((1.0,), False), ((1, -1), False), ((), True)])
def test_journey_settings_positive(values, valid):
    from mock_journey import settings
    if valid:
        assert settings._positive(*values) is None
    else:
        with pytest.raises(ValueError, match=r"\AInvalid journey settings\.\Z"):
            settings._positive(*values)


@pytest.mark.parametrize("value,integer,outcome", [
    (1, False, 1), (0.5, False, 0.5), (1, True, 1),
    (0.5, True, ValueError), (True, False, ValueError), (0, False, ValueError), (-1.0, False, ValueError),
    (math.nan, False, ValueError), (math.inf, False, ValueError), ("1", False, ValueError),
    (10 ** 400, True, OverflowError), (10 ** 400, False, OverflowError),
])
def test_aws_settings_positive_including_the_overflow_path(value, integer, outcome):
    from mock_journey import aws_settings
    if type(outcome) is type and issubclass(outcome, Exception):
        with pytest.raises(Exception) as error:
            aws_settings._positive(value, integer=integer)
        assert type(error.value) is outcome
        if outcome is ValueError:
            assert str(error.value) == "Invalid explicit AWS journey configuration."
        return
    assert aws_settings._positive(value, integer=integer) == outcome


def test_api_environment_length_limit():
    from mock_journey.settings import ApiSettings, StateSettings, StorageSettings
    state_settings, storage = StateSettings("t", 4), StorageSettings("dev", "b", "d", 1, 2)
    assert ApiSettings(state_settings, storage, "e" * 128, 4).environment == "e" * 128
    with pytest.raises(ValueError, match="Invalid journey settings."):
        ApiSettings(state_settings, storage, "e" * 129, 4)


@pytest.mark.parametrize("lease,interval,timeout,valid", [
    (60, 20, 39, True), (60, 20.1, 1, False), (60, 20, 40, False), (60, 0, 1, False), (60, 1, 0, False),
])
def test_aws_lease_timing_rule(lease, interval, timeout, valid):
    from mock_journey.aws_lease import AwsLeaseGuardFactory
    if valid:
        AwsLeaseGuardFactory(lease_seconds=lease, interval_seconds=interval, renewal_timeout_seconds=timeout)
    else:
        with pytest.raises(ValueError, match="Invalid AWS lease configuration."):
            AwsLeaseGuardFactory(lease_seconds=lease, interval_seconds=interval, renewal_timeout_seconds=timeout)


def test_aws_lease_timing_rule_has_no_type_check():
    from mock_journey.aws_lease import AwsLeaseGuardFactory
    with pytest.raises(TypeError):
        AwsLeaseGuardFactory(lease_seconds=60, interval_seconds="1", renewal_timeout_seconds=1)


# -- AWS scope identifiers and namespaces ---------------------------------------------

RELAY = dict(environment="unit-relay", partition="aws", account_id="000000000000", region="us-east-2",
             queue_url="https://sqs.us-east-2.amazonaws.com/000000000000/unit-relay")


class _State:
    def __init__(self, table="unit-relay-table"):
        self.table_name, self.clock, self.client, self.max_conflict_retries = table, lambda: 1000, None, 2


@pytest.mark.parametrize("change,valid", [
    ({}, True),
    ({"environment": "e" * 128}, True), ({"environment": "e" * 129}, False), ({"environment": ""}, False),
    ({"environment": "bad/env"}, False), ({"environment": 1}, False),
    ({"account_id": "12345678901"}, False), ({"account_id": 123456789012}, False),
    ({"region": "us-east"}, False), ({"region": "cn-north-1"}, False),
    ({"partition": "aws-cn", "region": "cn-north-1"}, True), ({"partition": "aws-cn"}, False),
    ({"partition": "aws-us-gov", "region": "us-gov-west-1"}, True), ({"partition": "aws-us-gov"}, False),
    ({"partition": "aws-iso"}, False),
])
def test_relay_progress_scope_matrix(change, valid):
    from mock_journey.relay_progress import DynamoRelayProgress
    scope = {**RELAY, **change}
    if not valid:
        with pytest.raises(ValueError, match=r"\AInvalid relay progress scope\.\Z"):
            DynamoRelayProgress(_State(), **scope)
        return
    progress = DynamoRelayProgress(_State(), **scope)
    assert progress.key == {"PK": "RELAY_SCAN#" + hashlib.sha256(scope["environment"].encode()).hexdigest(),
                            "SK": "PROGRESS#v1"}
    expected = json.dumps([scope["partition"], scope["account_id"], scope["region"], "unit-relay-table",
                           scope["environment"], scope["queue_url"]], separators=(",", ":"))
    assert progress.binding_sha256 == hashlib.sha256(expected.encode()).hexdigest()


@pytest.mark.parametrize("table,valid", [("abc", True), ("ab", False), ("a" * 255, True), ("a" * 256, False),
                                         ("bad table", False)])
def test_relay_progress_table_rule(table, valid):
    from mock_journey.relay_progress import DynamoRelayProgress
    if valid:
        DynamoRelayProgress(_State(table), **RELAY)
    else:
        with pytest.raises(ValueError, match="Invalid relay progress scope."):
            DynamoRelayProgress(_State(table), **RELAY)


@pytest.mark.parametrize("table,environment,valid", [
    ("abc", "e", True), ("ab", "e", False), ("abc", "e" * 128, True), ("abc", "e" * 129, False),
    ("abc", "", False), ("abc", "bad env", False), (1, "e", False), ("abc", None, False),
])
def test_log_store_scope_and_namespace(table, environment, valid):
    from mock_journey.log_storage import DynamoLogStore, OperationalLogError
    if not valid:
        with pytest.raises(OperationalLogError):
            DynamoLogStore(object(), table, environment)
        return
    store = DynamoLogStore(object(), table, environment)
    assert store.namespace == "OPS#" + hashlib.sha256(environment.encode()).hexdigest()


@pytest.mark.parametrize("change,valid", [
    ({}, True), ({"environment": "e" * 129}, False), ({"account_id": "1234"}, False),
    ({"region": "cn-north-1"}, False), ({"partition": "aws-cn", "region": "cn-north-1"}, True),
    ({"partition": "aws-us-gov", "region": "us-gov-east-1"}, True), ({"partition": "aws-iso"}, False),
])
def test_aws_settings_scope_matrix(change, valid):
    from mock_journey.aws_settings import AwsSettings
    from tests.aws_runtime_support import configuration
    config = {**configuration("relay"), **change}
    if config["partition"] != "aws":
        suffix = "amazonaws.com.cn" if config["partition"] == "aws-cn" else "amazonaws.com"
        config["relay"] = {**config["relay"],
                           "queue_url": f"https://sqs.{config['region']}.{suffix}/123456789012/synthetic-jobs"}
    if valid:
        assert AwsSettings.parse(json.dumps(config), "relay").region == config["region"]
    else:
        with pytest.raises(ValueError, match="Invalid explicit AWS journey configuration."):
            AwsSettings.parse(json.dumps(config), "relay")


# -- strict JSON parsers --------------------------------------------------------------

@pytest.mark.parametrize("raw,valid", [
    ('{"a": 1}', True), ('{"a": 1, "a": 2}', False), ('{"a": NaN}', False), ('{"a": Infinity}', False),
    ('{"a": -Infinity}', False), ("{", False), (b'{"a": 1}', False), (None, False), ('[1, 2.5]', True),
])
def test_aws_strict_json(raw, valid):
    from mock_journey.aws_settings import strict_json
    if valid:
        assert strict_json(raw) == json.loads(raw)
        return
    with pytest.raises(ValueError) as error:
        strict_json(raw)
    assert str(error.value) == "Invalid explicit AWS journey configuration."
    assert error.value.__cause__ is None and error.value.__suppress_context__ is True


def test_typed_parse_json_messages():
    from mock_journey.typed import parse_json
    assert parse_json(b'{"a": [1, 2.5, null]}') == {"a": [1, 2.5, None]}
    with pytest.raises(ValueError, match=r"\ADuplicate JSON key\.\Z"):
        parse_json('{"a": 1, "a": 1}')
    with pytest.raises(ValueError, match=r"\ANonfinite JSON\.\Z"):
        parse_json('{"a": NaN}')
    with pytest.raises(json.JSONDecodeError):
        parse_json("{")
    with pytest.raises(UnicodeEncodeError):
        parse_json('{"a": "\\ud800"}')


def test_log_record_parser_error_types():
    from mock_journey.log_storage import OperationalLogError, _parse
    for raw in ('{"a": 1}', b'{"a": 1, "a": 1}', b"x" * 70000):
        with pytest.raises(OperationalLogError):
            _parse(raw)
    # Invalid JSON is not converted here; DynamoLogStore.write/read_page wrap it.
    with pytest.raises(json.JSONDecodeError):
        _parse(b"{")
    # NaN is parsed (no parse_constant); the record validator then rejects it.
    with pytest.raises(Exception) as error:
        _parse(b'{"a": NaN}')
    assert not isinstance(error.value, json.JSONDecodeError)


# -- legacy USER slots and the Dummy principal -----------------------------------------

def test_legacy_slot_template_and_key():
    from mock_journey.catalog import definition_key
    assert definition_key("mock-cpr", "adult") == "mock-cpr:adult"


def test_dummy_principal_value():
    from mock_journey.auth import PRINCIPAL
    assert PRINCIPAL == "dummy-tester"

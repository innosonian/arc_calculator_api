"""Surface of the course repository and the in-memory CourseStore fakes (S7-04/X3-04).

The surface and fake tests were written before course_state.py was split and
passed on the unsplit module. The public class name, constructor, method
names and signatures are copied by hand from the source; the fake semantics
table follows the CourseStore contract in course_repo_core's docstring (and
records the known differences from the DynamoDB mapping). The last section
pins the split itself: fakes live only under tests/, the public record helpers
and protocols reach course_state as the same objects, every method has one
definition along the MRO, and the use-case modules keep course_state's rule of
never importing state.py. Private method names, parameter names and the
mixin that owns each method are deliberately not pinned (C-10).
"""

import ast
import hashlib
import inspect
from pathlib import Path

import pytest

from mock_journey import course_records, course_repo_core, course_state
from mock_journey.course_contracts import (
    AssignmentBinding, AttemptTemplate, ContentReport, CourseView, GateView, InventoryTicket, InventoryView,
    LearnerContext, PublicIds, RefreshTicket, StartCommand, StartReceipt, StoredProgressReceipt,
)
from mock_journey.course_errors import CourseError
from mock_journey.course_policy import CoursePolicy
from mock_journey.course_settings import CourseSettings
from mock_journey.course_state import CourseBlobStore, CourseStore, DynamoCourseRepository
from mock_journey.models import AuthContext
from tests.course_store_fakes import InMemoryBlobStore, InMemoryCourseStore, _action_allowed


P = inspect.Parameter
EMPTY = inspect.Parameter.empty
PK, KW = P.POSITIONAL_OR_KEYWORD, P.KEYWORD_ONLY

PUBLIC = {
    "begin_inventory": ([("auth", PK, AuthContext), ("learner", PK, LearnerContext)], InventoryTicket),
    "begin_inventory_for_session": ([("auth", PK, AuthContext)], InventoryTicket | None),
    "apply_inventory": ([("auth", PK, AuthContext), ("ticket", PK, InventoryTicket),
                         ("result_or_error", PK, EMPTY)], EMPTY),
    "load_inventory": ([("auth", PK, AuthContext), ("learner", PK, LearnerContext)], InventoryView),
    "load_inventory_for_session": ([("auth", PK, AuthContext)], InventoryView),
    "ensure_epoch": ([("auth", PK, AuthContext), ("binding", PK, AssignmentBinding)], GateView),
    "begin_refresh": ([("auth", PK, AuthContext), ("binding", PK, AssignmentBinding),
                       ("inventory", PK, InventoryTicket)], RefreshTicket),
    "apply_refresh": ([("auth", PK, AuthContext), ("ticket", PK, RefreshTicket),
                       ("bundle_or_error", PK, EMPTY)], GateView),
    "load_view": ([("auth", PK, AuthContext), ("ids", PK, PublicIds)], CourseView),
    "load_start_view": ([("auth", PK, AuthContext), ("command", PK, StartCommand)], CourseView),
    "find_created": ([("auth", PK, AuthContext), ("command", PK, StartCommand), ("kind", KW, str)],
                     StartReceipt | None),
    "start": ([("auth", PK, AuthContext), ("command", PK, StartCommand), ("kind", KW, str),
               ("view", KW, CourseView), ("template", KW, AttemptTemplate | None)], EMPTY),
    "report": ([("auth", PK, AuthContext), ("course_id", KW, int), ("enrollment_id", KW, int),
                ("placement_id", KW, int), ("report", KW, ContentReport)], StoredProgressReceipt),
}


def _own_methods(cls):
    return {name for klass in cls.__mro__ if klass is not object
            for name, value in vars(klass).items() if inspect.isfunction(value)}


def test_class_identity_and_constructor():
    from mock_journey import course_wiring
    assert DynamoCourseRepository.__module__ == "mock_journey.course_state"
    assert DynamoCourseRepository.__qualname__ == "DynamoCourseRepository"
    assert course_wiring.DynamoCourseRepository is DynamoCourseRepository
    signature = inspect.signature(DynamoCourseRepository)
    assert [(p.name, p.kind, p.annotation, p.default) for p in signature.parameters.values()] == [
        ("store", PK, CourseStore, EMPTY), ("settings", PK, CourseSettings, EMPTY),
        ("policy", PK, CoursePolicy, EMPTY), ("blob_store", PK, CourseBlobStore, EMPTY),
        ("clock", KW, EMPTY, EMPTY), ("uuid_factory", KW, EMPTY, EMPTY),
    ]
    with pytest.raises(TypeError, match="^Course repository requires CourseSettings and CoursePolicy.$"):
        DynamoCourseRepository(object(), object(), object(), object(), clock=int, uuid_factory=int)


def test_public_method_set_is_present_and_no_public_method_was_added():
    public = {name for name in _own_methods(DynamoCourseRepository) if not name.startswith("_")}
    assert public == set(PUBLIC)


@pytest.mark.parametrize("name", sorted(PUBLIC))
def test_public_signatures(name):
    parameters, returns = PUBLIC[name]
    signature = inspect.signature(getattr(DynamoCourseRepository, name))
    assert [(p.name, p.kind, p.annotation) for p in signature.parameters.values()] == [
        ("self", PK, EMPTY), *parameters]
    assert all(p.default is EMPTY for p in signature.parameters.values())
    assert signature.return_annotation == returns


def test_every_method_is_defined_exactly_once_along_the_mro():
    # Mixins must not shadow one another; a monkeypatch on the class or an
    # instance still reaches the one definition.
    owners = {}
    for klass in DynamoCourseRepository.__mro__:
        for name, value in vars(klass).items():
            if inspect.isfunction(value):
                owners.setdefault(name, []).append(klass)
    assert {name: len(classes) for name, classes in owners.items() if len(classes) != 1} == {}


def test_injected_attributes_are_declared_and_set_by_the_shared_base():
    core = course_repo_core.CourseRepositoryCore
    assert DynamoCourseRepository.__init__ is core.__init__
    assert set(core.__annotations__) == {"store", "settings", "policy", "blob_store"}
    from tests.test_vcc_state import repository
    repo, store, blobs = repository(clock=5)
    assert (repo.store, repo.blob_store) == (store, blobs)
    assert type(repo.settings) is CourseSettings and type(repo.policy) is CoursePolicy
    assert callable(repo.clock) and callable(repo.uuid_factory)


def test_instance_patch_points_still_apply():
    from tests.test_vcc_state import repository
    repo, *_ = repository(clock=5)
    assert repo._now() == 5
    repo.clock = lambda: 7.9
    assert repo._now() == 7
    repo.clock = lambda: "x"
    with pytest.raises(CourseError) as raised:
        repo._now()
    assert raised.value.code == "TEMPORARILY_UNAVAILABLE"


# -- in-memory fakes (test support, not production) ---------------------------------

def put(item=None, **conditions):
    item = item or {"PK": "X#1", "SK": "S"}
    return {"op": "put", "key": {"PK": item["PK"], "SK": item["SK"]}, "item": item, **conditions}


def check(**conditions):
    return {"op": "condition_check", "key": {"PK": "X#1", "SK": "S"}, **conditions}


ROW = {"PK": "X#1", "SK": "S", "a": 1, "n": 5, "flag": True}
ACTION_TABLE = [
    # Unconditional put is an upsert here; DynamoCourseStore maps it to
    # attribute_not_exists(#pk). No repository write is unconditional today.
    ("put plain / missing", None, put(), True),
    ("put plain / present", ROW, put(), True),
    ("put if_not_exists / missing", None, put(if_not_exists=True), True),
    ("put if_not_exists / present", ROW, put(if_not_exists=True), False),
    ("put if_match / missing", None, put(if_match={"a": 1}), False),
    ("put if_match / equal", ROW, put(if_match={"a": 1}), True),
    ("put if_match / differs", ROW, put(if_match={"a": 2}), False),
    # A missing attribute compares equal to None here (dict.get).
    ("put if_match / absent field", ROW, put(if_match={"zz": None}), True),
    ("put if_missing_or_match / missing", None, put(if_missing_or_match={"a": 9}), True),
    ("put if_missing_or_match / equal", ROW, put(if_missing_or_match={"a": 1}), True),
    ("put if_missing_or_match / differs", ROW, put(if_missing_or_match={"a": 2}), False),
    ("check without if_match / missing", None, check(), False),
    ("check without if_match / present", ROW, check(), True),
    ("check if_match / missing", None, check(if_match={"a": 1}), False),
    ("check if_match / equal", ROW, check(if_match={"a": 1}), True),
    ("check if_greater / greater", ROW, check(if_match={"a": 1}, if_greater={"n": 4}), True),
    ("check if_greater / equal", ROW, check(if_match={"a": 1}, if_greater={"n": 5}), False),
    ("check if_greater / bool field", ROW, check(if_match={"a": 1}, if_greater={"flag": 0}), False),
    ("check if_greater / bool bound", ROW, check(if_match={"a": 1}, if_greater={"n": False}), False),
    ("check if_greater / missing field", ROW, check(if_match={"a": 1}, if_greater={"m": 0}), False),
    ("check if_greater / missing row", None, check(if_match={"a": 1}, if_greater={"n": 0}), False),
    ("if_not_exists wins over if_match", None, put(if_not_exists=True, if_match={"a": 1}), True),
]


@pytest.mark.parametrize("current,action,allowed", [case[1:] for case in ACTION_TABLE],
                         ids=[case[0] for case in ACTION_TABLE])
def test_fake_condition_table(current, action, allowed):
    assert _action_allowed(current, action) is allowed


def test_fake_transact_is_all_or_nothing_and_records_copies():
    store = InMemoryCourseStore()
    store.seed({"PK": "A#1", "SK": "S", "revision": 1})
    first = put({"PK": "B#1", "SK": "S", "v": 1}, if_not_exists=True)
    failing = put({"PK": "A#1", "SK": "S", "revision": 2}, if_match={"revision": 0})
    assert store.transact([first, failing]) is False
    assert store.get_item({"PK": "B#1", "SK": "S"}) is None
    assert store.get_item({"PK": "A#1", "SK": "S"}) == {"PK": "A#1", "SK": "S", "revision": 1}
    first["item"]["v"] = 99
    assert store.calls == [[put({"PK": "B#1", "SK": "S", "v": 1}, if_not_exists=True),
                            put({"PK": "A#1", "SK": "S", "revision": 2}, if_match={"revision": 0})]]
    ok = put({"PK": "A#1", "SK": "S", "revision": 2}, if_match={"revision": 1})
    assert store.transact([ok]) is True
    row = store.get_item({"PK": "A#1", "SK": "S"})
    row["revision"] = 7
    assert store.get_item({"PK": "A#1", "SK": "S"})["revision"] == 2
    seeded = {"PK": "C#1", "SK": "S", "x": [1]}
    store.seed(seeded)
    seeded["x"].append(2)
    assert store.get_item({"PK": "C#1", "SK": "S"})["x"] == [1]


def test_fake_transact_conflicts_and_shape_errors():
    store = InMemoryCourseStore()
    store.conflict_remaining = 2
    action = put({"PK": "A#1", "SK": "S"}, if_not_exists=True)
    assert [store.transact([action]) for _ in range(3)] == [False, False, True]
    assert store.conflict_remaining == 0 and len(store.calls) == 3
    store.always_conflict = True
    assert store.transact([put({"PK": "B#1", "SK": "S"}, if_not_exists=True)]) is False
    assert store.get_item({"PK": "B#1", "SK": "S"}) is None
    store.always_conflict = False
    with pytest.raises(ValueError, match="if_greater is only valid on condition_check"):
        store.transact([put({"PK": "B#1", "SK": "S"}, if_greater={"n": 1})])


def test_fake_blob_store():
    blobs = InMemoryBlobStore()
    body = b"{\"a\":1}"
    digest_value = blobs.put_bytes(body)
    assert digest_value == hashlib.sha256(body).hexdigest()
    assert blobs.get_bytes(digest_value) is body
    assert blobs.get_bytes("0" * 64) is None
    for bad in ("text", bytearray(b"x"), None):
        with pytest.raises(CourseError) as raised:
            blobs.put_bytes(bad)
        assert raised.value.code == "TEMPORARILY_UNAVAILABLE"


# -- the split (S7-04/X3-04) --------------------------------------------------------

ROOT = Path(__file__).resolve().parents[1]
PRODUCTION = ("calculators", "config", "data_handlers", "mock_journey", "models", "services", "transformers",
              "util", "local_server")
USE_CASE_MODULES = ("course_repo_core", "course_repo_inventory", "course_repo_refresh", "course_repo_start",
                    "course_repo_report")
PUBLIC_RE_EXPORTS = {
    course_records: ("bundle_record", "bundle_from_record", "assignment_record", "assignment_from_record",
                     "scope_identity_list", "start_request_digest", "report_request_digest"),
    course_repo_core: ("SCHEMA_VERSION", "DYNAMODB_ITEM_MAX_BYTES", "CourseStore", "CourseBlobStore"),
}


def _imports(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names, tree


def test_public_names_reach_course_state_as_the_same_objects():
    for module, names in PUBLIC_RE_EXPORTS.items():
        for name in names:
            assert getattr(course_state, name) is getattr(module, name), (module.__name__, name)
    assert (course_state.SCHEMA_VERSION, course_state.DYNAMODB_ITEM_MAX_BYTES) == (1, 409600)


def test_repository_modules_build_row_keys_from_storage_keys_only():
    # No module of the repository defines or aliases a row-key function of its own.
    for name in USE_CASE_MODULES:
        _, tree = _imports(ROOT / "mock_journey" / f"{name}.py")
        defined = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
        assert not {item for item in defined if item.endswith("_key") and item != "_action_key"}, name
        aliases = {node.targets[0].id for node in tree.body if isinstance(node, ast.Assign)
                   and isinstance(node.targets[0], ast.Name)}
        assert not {item for item in aliases if item.endswith("_key")}, name


def test_fakes_live_only_under_tests():
    for name in ("InMemoryBlobStore", "InMemoryCourseStore", "_action_allowed", "_fields_match", "_fields_greater"):
        assert not hasattr(course_state, name), name
    for package in PRODUCTION:
        for path in sorted((ROOT / package).rglob("*.py")):
            imported, tree = _imports(path)
            assert not {name for name in imported if name == "tests" or name.startswith("tests.")}, path
            defined = {node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)}
            assert not defined & {"InMemoryBlobStore", "InMemoryCourseStore"}, path
    for name in ("lambda_handler.py", "main.py", "submit_arc.py"):
        imported, _ = _imports(ROOT / name)
        assert not {item for item in imported if item == "tests" or item.startswith("tests.")}, name


@pytest.mark.parametrize("module_name", USE_CASE_MODULES + ("course_state",))
def test_use_case_modules_keep_the_injection_boundary(module_name):
    imported, _ = _imports(ROOT / "mock_journey" / f"{module_name}.py")
    assert not {name for name in imported if name == "mock_journey.state" or name.startswith("mock_journey.state.")}
    # The use-case modules depend on the core, never on course_state or on each other.
    if module_name != "course_state":
        siblings = {f"mock_journey.{name}" for name in USE_CASE_MODULES if name != "course_repo_core"}
        assert "mock_journey.course_state" not in imported
        assert not imported & (siblings - {f"mock_journey.{module_name}"})

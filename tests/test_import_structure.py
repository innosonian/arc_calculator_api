"""Static import structure guards (AST only; nothing here imports the scanned code).

Function-level (lazy) imports count as edges: they hide cycles from a plain
module-level scan but still form one when called.

- The Lambda entry module, ``main`` and the calculator/service packages are in
  no import cycle. ``mock_journey.legacy_bridge`` reads the legacy parsers from
  ``services.http.legacy_request`` rather than ``lambda_handler``.
- Cycles that still exist inside the application runtime are listed in
  ``KNOWN_CYCLES``. A new cycle, or a cycle that grows, fails; removing one is
  fine and the list may then shrink.
- Upward edges from the lower calculation layers (and services → application)
  are pinned in ``KNOWN_UPWARD_EDGES``. Several sources are hash-pinned
  (tests/fixtures/detection_revision/provenance.json) and keep their imports;
  new upward edges fail.
"""

import ast
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ("calculators", "config", "data_handlers", "models", "services", "transformers", "util",
            "mock_journey", "local_server")
TOP_LEVEL = ("lambda_handler", "main", "submit_arc")
CORE_PACKAGES = {"calculators", "config", "data_handlers", "models", "services", "transformers", "util"}
LOWER_LAYERS = {"calculators", "config", "data_handlers", "models", "transformers", "util"}
UPPER_LAYERS = {"services", "mock_journey", "local_server", *TOP_LEVEL}

KNOWN_CYCLES = (
    # Application runtime wiring: assembly/aws_runtime/worker/dispatch refer
    # back to each other through run() helpers and ExecutionCatalog (X3-01 4-5).
    frozenset({
        "mock_journey.assembly", "mock_journey.aws_runtime", "mock_journey.aws_settings",
        "mock_journey.calculation", "mock_journey.dispatch", "mock_journey.execution_definitions",
        "mock_journey.worker", "mock_journey.worker_runtime",
    }),
    frozenset({"local_server.charts", "local_server.http"}),
    frozenset({"local_server.cli", "local_server.runtime"}),
)
KNOWN_UPWARD_EDGES = frozenset({
    # Hash-pinned sources (config types live in services today).
    ("calculators.merge_calculator", "services.config"),
    ("config.borders", "services.http.schemas"),
    ("config.calculation_config", "services.http.schemas"),
    ("data_handlers.data_parser", "services.config"),
    ("data_handlers.vp_action", "services.config"),
    ("data_handlers.vp_action", "services.http.schemas"),
    # Module-level imports of pinned-function files and unpinned sources.
    ("calculators.action_evaluator", "services.config"),
    ("data_handlers.action_data", "services.config"),
    ("data_handlers.detection", "services.config"),
    ("data_handlers.chart_data", "services.calculation_context"),
    ("data_handlers.chart_data", "services.operational_logs"),
    # Lazy: the operational log allowlist reads the fixed public error tables.
    # Kept for now (A-02): removing it means moving the fixed code -> (status,
    # message) table below mock_journey (e.g. services/error_codes.py) and having
    # mock_journey/errors.py and course_errors.py re-export it, or registering the
    # set from assembly. Both touch mock_journey files, so it is a separate step;
    # the values are pinned by tests/test_operational_log_error_codes.py.
    ("services.operational_logs", "mock_journey.course_errors"),
})


def _modules():
    files = [ROOT / f"{name}.py" for name in TOP_LEVEL]
    for package in PACKAGES:
        files.extend(sorted((ROOT / package).rglob("*.py")))
    modules = {}
    for path in files:
        parts = list(path.relative_to(ROOT).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        modules[".".join(parts)] = path
    return modules


def _graph():
    modules = _modules()

    def resolve(name):
        while name:
            if name in modules:
                return name
            name = name.rpartition(".")[0]
        return None

    graph = {}
    for name, path in modules.items():
        edges = set()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                edges.update(resolve(alias.name) for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                for alias in node.names:
                    full = f"{node.module}.{alias.name}"
                    edges.add(full if full in modules else resolve(node.module))
        edges.discard(None)
        edges.discard(name)
        graph[name] = edges
    return graph


def _cycles(graph):
    index, low, stack, on_stack, found = {}, {}, [], set(), []

    def visit(node):
        index[node] = low[node] = len(index)
        stack.append(node)
        on_stack.add(node)
        for target in graph[node]:
            if target not in index:
                visit(target)
                low[node] = min(low[node], low[target])
            elif target in on_stack:
                low[node] = min(low[node], index[target])
        if low[node] == index[node]:
            component = set()
            while True:
                member = stack.pop()
                on_stack.discard(member)
                component.add(member)
                if member == node:
                    break
            if len(component) > 1:
                found.append(frozenset(component))

    limit = sys.getrecursionlimit()
    sys.setrecursionlimit(max(limit, 10_000))
    try:
        for node in sorted(graph):
            if node not in index:
                visit(node)
    finally:
        sys.setrecursionlimit(limit)
    return found


@pytest.fixture(scope="module")
def graph():
    return _graph()


def test_scanner_sees_lazy_imports(graph):
    # Negative control: a known function-level import is an edge.
    assert "mock_journey.handler" in graph["lambda_handler"]
    assert "mock_journey.course_errors" in graph["services.operational_logs"]
    assert "services.http.legacy_request" in graph["mock_journey.legacy_bridge"]


def test_entry_and_calculator_modules_are_in_no_cycle(graph):
    cycles = _cycles(graph)
    in_cycle = set().union(*cycles) if cycles else set()
    core = {name for name in graph if name in TOP_LEVEL or name.split(".")[0] in CORE_PACKAGES}
    assert not (in_cycle & core), sorted(in_cycle & core)
    assert "mock_journey.legacy_bridge" not in in_cycle
    assert "mock_journey.handler" not in in_cycle


def test_no_new_or_larger_cycles(graph):
    unexpected = [sorted(cycle) for cycle in _cycles(graph)
                  if not any(cycle <= known for known in KNOWN_CYCLES)]
    assert not unexpected


def test_cycle_detector_reports_a_synthetic_cycle():
    assert _cycles({"a": {"b"}, "b": {"c"}, "c": {"a"}, "d": {"a"}}) == [frozenset({"a", "b", "c"})]


def test_bridge_and_parser_modules_do_not_reach_the_entry_module(graph):
    def reachable(start):
        seen, todo = set(), [start]
        while todo:
            for target in graph[todo.pop()]:
                if target not in seen:
                    seen.add(target)
                    todo.append(target)
        return seen

    for start in ("services.http.legacy_request", "mock_journey.legacy_bridge", "mock_journey.course_wiring"):
        assert not reachable(start) & {"lambda_handler", "main"}, start


def test_no_new_upward_layer_edges(graph):
    edges = {
        (source, target) for source, targets in graph.items() for target in targets
        if (source.split(".")[0] in LOWER_LAYERS and target.split(".")[0] in UPPER_LAYERS)
        or (source.split(".")[0] == "services" and target.split(".")[0] in {"mock_journey", "local_server",
                                                                              *TOP_LEVEL})
    }
    assert edges <= KNOWN_UPWARD_EDGES, sorted(edges - KNOWN_UPWARD_EDGES)


def test_http_schema_module_imports_only_typing():
    # config/borders.py and config/calculation_config.py (pinned) import these
    # types; keeping schemas.py dependency-free prevents a config import cycle.
    tree = ast.parse((ROOT / "services/http/schemas.py").read_text(encoding="utf-8"))
    imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    imported |= {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert imported == {"typing"}


@pytest.mark.parametrize("module,forbidden", [
    ("services.http.legacy_request", ("lambda_handler", "main", "boto3", "botocore", "sentry_sdk")),
    ("mock_journey.legacy_bridge", ("lambda_handler", "main", "boto3", "botocore", "sentry_sdk")),
    ("config.guideline_registry", ("lambda_handler", "main", "services", "mock_journey", "boto3")),
    ("mock_journey.assembly", ("lambda_handler", "main", "sentry_sdk")),
])
def test_cold_import_does_not_load_the_entry_module_or_sdks(module, forbidden):
    # A new process: an already-populated sys.modules could hide the import.
    code = f"""
import builtins, importlib
original_import = builtins.__import__
forbidden = {set(forbidden)!r}
def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    if level == 0 and name.split('.')[0] in forbidden:
        raise AssertionError('Forbidden import: ' + name)
    return original_import(name, globals, locals, fromlist, level)
builtins.__import__ = guarded_import
importlib.import_module({module!r})
"""
    completed = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True,
                               timeout=30, check=False)
    assert completed.returncode == 0, completed.stderr


def test_cycle_evaluator_is_function_pinned_and_imports_only_used_names():
    # provenance.json pins the scoring methods' AST, not this module header, so
    # unused header imports may be removed without re-pinning (S1-09 step 1).
    import json

    provenance = json.loads((ROOT / "tests/fixtures/detection_revision/provenance.json").read_text())
    assert "calculators/cycle_evaluator.py" not in provenance["preserved_sha256"]
    assert "calculators/cycle_evaluator.py" not in provenance["preserved_ast_sha256"]
    assert "calculators/cycle_evaluator.py" in provenance["preserved_function_ast_sha256"]
    tree = ast.parse((ROOT / "calculators/cycle_evaluator.py").read_text(encoding="utf-8"))
    imported = {alias.asname or alias.name for node in tree.body if isinstance(node, ast.ImportFrom)
                for alias in node.names}
    used = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    used |= {node.value.id for node in ast.walk(tree)
             if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)}
    assert imported - used == set()

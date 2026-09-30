#!/usr/bin/env python3
"""Capture the behavior-preservation goldens from the current code (D125, E-08).

Goldens (tests/fixtures/):
  v2_baseline/        dummy_dev_definitions.json, flow_{responses,events,rows}.json,
                      user_slots.json, legacy_compat.json   (tests/v2_baseline_support.py)
                      write_requests.json                   (tests/write_baseline_support.py)
  worker_call_order/  spy_traces.json, memory_traces.json   (tests/worker_trace_support.py)

Usage (the project's Python interpreter, from the repository root)::

    python scripts/capture_baselines.py --output-dir DIR [--only KIND[:NAME] ...] [--check]

``--output-dir`` is required and must not point into the repository fixture
tree (tests/fixtures): the tool never overwrites a golden. It writes the
captured files under DIR in the fixture layout. ``--check`` additionally
compares every captured golden with the current fixture (``--fixture-root``,
default tests/fixtures) and exits 1 on any difference; use it to confirm that
a change left the goldens untouched, or, after an approved behavior change, to
see which goldens the change reaches before the user reviews the new files.

When to run it: only after a behavior change the user approved (D124/D125).
The goldens are characterization baselines; regenerating them from the
current output is never a fix for a failing baseline test. Moving captured
files into tests/fixtures is a separate step done after user review, and the
docstrings of the baseline tests name the source of the capture.

Method (the procedure the goldens were first captured with): every selected
script runs in two separate worker processes with different PYTHONHASHSEED
(1 and 987) and, in the second, util.uploader's wall clock shifted by three
days. A UUID / 32-hex / 64-hex value that appears in one run but not in the
other is run-dependent and becomes a placeholder labelled by first appearance
(``<uuid:N>``, ``<hex:N>``, ``<h:N>``); values present in both runs
(definition hashes, scope keys, seeded fixture rows) stay literal. Whatever
the runs' clocks produced becomes ``CPR-ACTION-<wallclock>`` and ``/<date>/``
in storage keys; ``resume_nonce`` values and the tails of ``accessToken`` /
``resumeCredential`` become ``<secret>``; plain ``*_ms`` integers become
``<ms>``. Both runs must then yield the identical template, otherwise the
tool reports the first value the placeholders do not cover. Worker call-order
traces are canonicalized by tests/worker_trace_support.canonical and must be
byte-identical between the runs. Rows of a v2 template are emitted in a
deterministic order (their placeholder key, then content); the matcher pairs
rows by key, so row order and label numbering are not part of the contract
and ``--check`` compares templates up to a consistent relabelling and row
order (it also reports when the written bytes are identical to the fixture).

Workers run with STAGE=test, PYTHONDONTWRITEBYTECODE=1 and the offline network
audit guard of the test suite; nothing here reaches AWS or a local server.
"""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile


REPO = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = REPO / "tests" / "fixtures"
SEEDS = ("1", "987")
CLOCK_SHIFT_DAYS = 3
V2_NAMES = ("dummy_dev_definitions", "flow", "legacy_active_session", "legacy_new_session")
TRACE_NAMES = ("spy", "memory")
V2_FLOW_SECTIONS = ("responses", "events", "rows", "user_slots")
V2_LEGACY_SECTIONS = ("responses", "events", "rows", "user_slots", "checkpoints")

_RAW_TOKEN = re.compile(
    r"(?P<uuid>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
    r"|(?P<h>(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f]))"
    r"|(?P<hex>(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f]))"
)
_LABEL_TOKEN = re.compile(r"<(?P<kind>uuid|hex|h):(?P<number>\d+)>")
_STEM = re.compile(r"CPR-ACTION-[0-9]{10}")
_DATE = re.compile(r"(?<=/)[0-9]{4}-[0-9]{2}-[0-9]{2}(?=/)")
_SECRET_TAIL = re.compile(r"[A-Za-z0-9_-]{16,128}\Z")
SECRET_VALUE_KEYS = frozenset({"resume_nonce"})
SECRET_TAIL_KEYS = frozenset({"accessToken", "resumeCredential"})


def write_flow_names():
    sys.path.insert(0, str(REPO))
    from tests.write_baseline_support import FLOWS
    return tuple(FLOWS)


# ---------------------------------------------------------------------------
# Placeholders
# ---------------------------------------------------------------------------

def _secret_of(key, value):
    """The secret string a value under ``key`` carries (whole nonce, typed nonce or credential tail), or None."""
    if key in SECRET_VALUE_KEYS:
        if type(value) is str:
            return value
        if type(value) is dict and set(value) == {"S"} and type(value["S"]) is str:
            return value["S"]
    if key in SECRET_TAIL_KEYS and type(value) is str:
        head, dot, tail = value.rpartition(".")
        if dot and _SECRET_TAIL.match(tail):
            return tail
    return None


def _tokens(value, found, key=None):
    """Every raw token (UUID, 32/64-hex, key stem, key date, secret) in ``value``, dict keys included."""
    if type(value) is str:
        for pattern in (_RAW_TOKEN, _STEM, _DATE):
            for match in pattern.finditer(value):
                found.add(match.group(0))
    secret = _secret_of(key, value)
    if secret is not None:
        found.add(secret)
    if type(value) is dict:
        for name, item in value.items():
            _tokens(name, found)
            _tokens(item, found, name)
    elif type(value) is list:
        for item in value:
            _tokens(item, found)
    return found


def run_dependent_tokens(first, second):
    """Raw tokens that one run produced and the other did not."""
    return _tokens(first, set()) ^ _tokens(second, set())


class Labeller:
    """Replace run-dependent tokens by placeholders labelled by first appearance.

    ``raw`` mode works on an observation (tokens are UUID/hex strings, secrets
    and wall-clock parts are still literal); template mode works on a stored
    template (tokens are ``<kind:N>`` labels) and only renumbers them, which
    gives the canonical form used to compare two templates.
    """

    def __init__(self, *, raw, dependent=frozenset(), mask_always=True):
        self.raw = raw
        self.dependent = dependent
        # v2 templates mask every key stem clock, key date and secret; the write-request
        # template only those that differed between the runs (seeded rows stay literal).
        self.mask_always = mask_always
        self.pattern = _RAW_TOKEN if raw else _LABEL_TOKEN
        self.labels = {}
        self.counts = {}

    def _masked(self, token):
        return self.mask_always or token in self.dependent

    def _label(self, kind, token, assign):
        if token not in self.labels:
            if not assign:
                return f"<{kind}>"
            self.counts[kind] = self.counts.get(kind, 0) + 1
            self.labels[token] = f"<{kind}:{self.counts[kind]}>"
        return self.labels[token]

    def string(self, text, assign=True):
        def replace(match):
            token = match.group(0)
            if self.raw:
                if token not in self.dependent:
                    return token
                return self._label(match.lastgroup, token, assign)
            return self._label(match.group("kind"), token, assign)
        def wallclock(match, placeholder):
            token = match.group(0)
            return placeholder if self._masked(token) else token

        text = self.pattern.sub(replace, text)
        if self.raw:
            text = _STEM.sub(lambda match: wallclock(match, "CPR-ACTION-<wallclock>"), text)
            text = _DATE.sub(lambda match: wallclock(match, "<date>"), text)
        return text

    def value(self, value, key=None, assign=True):
        secret = _secret_of(key, value) if self.raw else None
        if secret is not None and self._masked(secret):
            if type(value) is dict:
                return {"S": "<secret>"}  # a DynamoDB-typed attribute
            if key in SECRET_VALUE_KEYS:
                return "<secret>"
            return self.string(value[:-len(secret)], assign) + "<secret>"
        if type(value) is str:
            return self.string(value, assign)
        if type(value) is dict:
            return {self.string(name, assign): self.value(item, name, assign) for name, item in value.items()}
        if type(value) is list:
            return [self.value(item, None, assign) for item in value]
        if self.raw and type(value) is int and type(key) is str and key.endswith("_ms") and value >= 0:
            return "<ms>"
        return value

    def row_key(self, row):
        """Deterministic order of rows: bound labels, masked unbound tokens, then content."""
        return (self.string(f'{row["PK"]}/{row["SK"]}', assign=False),
                json.dumps(self.value(row, assign=False), sort_keys=True))

    def section(self, name, value):
        """Rows (and the USER rows of checkpoints) are stored with sorted keys; the matcher ignores dict order."""
        if name == "rows":
            return [_sorted_keys(self.value(row)) for row in sorted(value, key=self.row_key)]
        if name == "checkpoints":
            return [{"label": point["label"], "user": _sorted_keys(self.value(point["user"]))} for point in value]
        return self.value(value)


def _sorted_keys(value):
    if type(value) is dict:
        return {key: _sorted_keys(value[key]) for key in sorted(value)}
    if type(value) is list:
        return [_sorted_keys(item) for item in value]
    return value


def templatize_sections(observation, sections, dependent):
    """One run's v2 observation -> template (raw mode)."""
    labeller = Labeller(raw=True, dependent=dependent)
    return {name: labeller.section(name, observation[name]) for name in sections if name in observation}


def canonical_sections(template, sections):
    """A stored or captured v2 template -> canonical form (labels renumbered, rows reordered)."""
    labeller = Labeller(raw=False)
    return {name: labeller.section(name, template[name]) for name in sections if name in template}


def templatize_list(observed, dependent):
    return Labeller(raw=True, dependent=dependent, mask_always=False).value(observed)


def canonical_list(template):
    return Labeller(raw=False).value(template)


# A decoded GSI cursor (Relay checkpoint scans) keeps the key order of the query response, which the
# in-memory table derives from a set: not code order, and not compared (tests/write_baseline_support.py).
CURSOR_KEYS = frozenset({"PK", "SK", "GSI1PK", "GSI1SK"})


def first_difference(expected, observed, path="$"):
    """Path and values of the first difference between two JSON values (None when equal).

    Dict key order is part of the comparison, except for the GSI cursor maps.
    """
    if type(expected) is not type(observed):
        return f"{path}: {json.dumps(expected)[:200]} != {json.dumps(observed)[:200]}"
    if type(expected) is dict:
        if list(expected) != list(observed) and not set(expected) == set(observed) == CURSOR_KEYS:
            return f"{path}: keys {list(expected)[:12]} != {list(observed)[:12]}"
        for key in expected:
            found = first_difference(expected[key], observed[key], f"{path}.{key}")
            if found:
                return found
        return None
    if type(expected) is list:
        if len(expected) != len(observed):
            return f"{path}: {len(expected)} items != {len(observed)} items"
        for index, (left, right) in enumerate(zip(expected, observed)):
            found = first_difference(left, right, f"{path}[{index}]")
            if found:
                return found
        return None
    if expected != observed:
        return f"{path}: {json.dumps(expected)[:200]} != {json.dumps(observed)[:200]}"
    return None


class CaptureError(Exception):
    """The two runs do not agree on a template."""


def template_of_sections(first, second, sections):
    dependent = run_dependent_tokens(first, second)
    left = templatize_sections(first, sections, dependent)
    right = templatize_sections(second, sections, dependent)
    difference = first_difference(left, right)
    if difference:
        raise CaptureError("the runs differ in a value the placeholders do not cover: " + difference)
    return left


def template_of_list(first, second):
    dependent = run_dependent_tokens(first, second)
    left, right = templatize_list(first, dependent), templatize_list(second, dependent)
    difference = first_difference(left, right)
    if difference:
        raise CaptureError("the runs differ in a value the placeholders do not cover: " + difference)
    return left


def template_of_identical(first, second):
    difference = first_difference(first, second)
    if difference:
        raise CaptureError("the runs differ: " + difference)
    return first


# ---------------------------------------------------------------------------
# File formats (byte-identical to the fixture layout)
# ---------------------------------------------------------------------------

def _pretty(value, level, compact_keys, compact_entries=False):
    pad, inner = " " * level, " " * (level + 1)
    if type(value) is dict:
        if not value:
            return "{}"
        lines = [f"{inner}{json.dumps(key)}: {_pretty(item, level + 1, compact_keys, key in compact_keys)}"
                 for key, item in value.items()]
        return "{\n" + ",\n".join(lines) + "\n" + pad + "}"
    if type(value) is list:
        if not value:
            return "[]"
        if compact_entries:
            lines = [inner + json.dumps(item, separators=(",", ":")) for item in value]
        else:
            lines = [inner + _pretty(item, level + 1, compact_keys) for item in value]
        return "[\n" + ",\n".join(lines) + "\n" + pad + "]"
    return json.dumps(value)


def dumps_pretty(value):
    """json.dumps(indent=1) plus a trailing newline (the v2 baseline files)."""
    return json.dumps(value, indent=1) + "\n"


def dumps_compact_entries(value, compact_keys):
    """Indent-1 layout whose lists under ``compact_keys`` hold one compact object per line."""
    return _pretty(value, 0, frozenset(compact_keys)) + "\n"


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

def parse_selection(only):
    """--only values -> {kind: [names]} (every name when no --only)."""
    kinds = {"v2_baseline": list(V2_NAMES), "write_requests": None, "worker_call_order": list(TRACE_NAMES)}
    if not only:
        return {kind: names for kind, names in kinds.items()}
    selected = {}
    for entry in only:
        kind, _, name = entry.partition(":")
        if kind not in kinds:
            raise ValueError(f"unknown golden kind {kind!r} (choose from {', '.join(kinds)})")
        available = kinds[kind]
        if kind == "write_requests":
            available = list(write_flow_names())
        if name and name not in available:
            raise ValueError(f"unknown {kind} name {name!r} (choose from {', '.join(available)})")
        selected.setdefault(kind, [])
        for chosen in ([name] if name else available):
            if chosen not in selected[kind]:
                selected[kind].append(chosen)
    return selected


# ---------------------------------------------------------------------------
# Worker process: one run of the selected scripts
# ---------------------------------------------------------------------------

def _shift_wall_clock(days):
    """util.uploader's wall clock (key stems, date prefixes, created_at) moves by ``days``."""
    import datetime as datetime_module
    from types import SimpleNamespace
    import util.uploader as uploader

    base = datetime_module.datetime

    class Shifted(base):
        @classmethod
        def now(cls, tz=None):
            return base.now(tz) + datetime_module.timedelta(days=days)

    uploader.datetime = SimpleNamespace(datetime=Shifted, UTC=datetime_module.UTC,
                                        timedelta=datetime_module.timedelta)


def run_worker(selection, clock_shift_days):
    """Run the selected scripts in this process; return the raw observations."""
    sys.path.insert(0, str(REPO))
    from tests.network_guard_support import offline_network

    if clock_shift_days:
        _shift_wall_clock(clock_shift_days)
    raw = {}
    with offline_network():
        if "v2_baseline" in selection:
            from tests.journey_support import JourneyStore
            from tests import v2_baseline_support as v2
            scripts = {"dummy_dev_definitions": lambda: v2.definition_observation(),
                       "flow": lambda: v2.golden_flow(JourneyStore.memory()),
                       "legacy_active_session": lambda: v2.legacy_active_session(JourneyStore.memory()),
                       "legacy_new_session": lambda: v2.legacy_new_session(JourneyStore.memory())}
            raw["v2_baseline"] = {name: scripts[name]() for name in selection["v2_baseline"]}
        if "write_requests" in selection:
            from tests.write_baseline_support import FLOWS, observe
            names = selection["write_requests"] or list(FLOWS)
            raw["write_requests"] = {name: observe(name) for name in names}
        if "worker_call_order" in selection:
            from tests.worker_trace_support import MEMORY_SCENARIOS, SPY_SCENARIOS, run_memory, run_spy
            traces = {}
            if "spy" in selection["worker_call_order"]:
                traces["spy"] = {name: run_spy(name) for name in SPY_SCENARIOS}
            if "memory" in selection["worker_call_order"]:
                traces["memory"] = {name: run_memory(name) for name in MEMORY_SCENARIOS}
            raw["worker_call_order"] = traces
    return raw


def _spawn_workers(selection, directory, python):
    outputs = []
    processes = []
    for index, seed in enumerate(SEEDS):
        output = Path(directory) / f"run_{index}.json"
        command = [python, "-B", str(Path(__file__).resolve()), "--worker", "--raw-output", str(output),
                   "--clock-shift-days", str(CLOCK_SHIFT_DAYS if index else 0)]
        for kind, names in selection.items():
            for name in names or [""]:
                command += ["--only", f"{kind}:{name}" if name else kind]
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONHASHSEED": seed, "STAGE": "test",
               "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(REPO)}
        for name in ("HOME", "TMPDIR", "LANG", "LC_ALL", "SYSTEMROOT"):
            if name in os.environ:
                env[name] = os.environ[name]
        # The calculator's own diagnostic prints go to the worker log, not to this tool's output.
        log = open(Path(directory) / f"run_{index}.log", "w")
        processes.append((subprocess.Popen(command, env=env, cwd=str(REPO), stdout=log, stderr=subprocess.STDOUT), log))
        outputs.append(output)
    for process, log in processes:
        code = process.wait()
        log.close()
        if code != 0:
            raise CaptureError(f"a capture worker failed (exit {code}):\n" + Path(log.name).read_text()[-4000:])
    return [json.loads(output.read_text()) for output in outputs]


# ---------------------------------------------------------------------------
# Templates, files and the fixture comparison
# ---------------------------------------------------------------------------

def build_templates(first, second):
    """Raw observations of the two runs -> {kind: {name: template}}."""
    templates = {}
    if "v2_baseline" in first:
        section = {}
        for name in first["v2_baseline"]:
            left, right = first["v2_baseline"][name], second["v2_baseline"][name]
            if name == "dummy_dev_definitions":
                section[name] = template_of_identical(left, right)
            elif name == "flow":
                section[name] = template_of_sections(left, right, V2_FLOW_SECTIONS)
            else:
                section[name] = template_of_sections(left, right, V2_LEGACY_SECTIONS)
        templates["v2_baseline"] = section
    if "write_requests" in first:
        templates["write_requests"] = {name: template_of_list(first["write_requests"][name],
                                                              second["write_requests"][name])
                                       for name in first["write_requests"]}
    if "worker_call_order" in first:
        templates["worker_call_order"] = {name: template_of_identical(first["worker_call_order"][name],
                                                                      second["worker_call_order"][name])
                                          for name in first["worker_call_order"]}
    return templates


def golden_files(templates):
    """{relative path: payload} in the fixture layout (partial payloads under --only)."""
    files = {}
    v2 = templates.get("v2_baseline", {})
    if "dummy_dev_definitions" in v2:
        files["v2_baseline/dummy_dev_definitions.json"] = v2["dummy_dev_definitions"]
    slots = {}
    if "flow" in v2:
        flow = v2["flow"]
        files["v2_baseline/flow_responses.json"] = flow["responses"]
        files["v2_baseline/flow_events.json"] = flow["events"]
        files["v2_baseline/flow_rows.json"] = flow["rows"]
        slots["flow"] = flow["user_slots"]
    legacy = {}
    for name in ("legacy_active_session", "legacy_new_session"):
        if name in v2:
            template = dict(v2[name])
            slots[name] = template.pop("user_slots")
            legacy[name] = template
    if slots:
        files["v2_baseline/user_slots.json"] = slots
    if legacy:
        files["v2_baseline/legacy_compat.json"] = legacy
    if "write_requests" in templates:
        files["v2_baseline/write_requests.json"] = templates["write_requests"]
    for name, traces in templates.get("worker_call_order", {}).items():
        files[f"worker_call_order/{name}_traces.json"] = traces
    return files


def render_file(relative, payload):
    if relative.startswith("worker_call_order/"):
        return dumps_compact_entries(payload, {"trace"})
    if relative == "v2_baseline/write_requests.json":
        return dumps_compact_entries(payload, set(payload))
    return dumps_pretty(payload)


def _canonical_file(relative, payload):
    """Canonical form of one golden file's payload (labels renumbered, rows reordered)."""
    if relative == "v2_baseline/flow_rows.json":
        return canonical_sections({"rows": payload}, ("rows",))["rows"]
    if relative in ("v2_baseline/flow_responses.json", "v2_baseline/flow_events.json"):
        return canonical_list(payload)
    if relative == "v2_baseline/legacy_compat.json":
        return {name: canonical_sections(template, V2_LEGACY_SECTIONS) for name, template in payload.items()}
    if relative == "v2_baseline/write_requests.json":
        return {name: canonical_list(writes) for name, writes in payload.items()}
    if relative in ("v2_baseline/user_slots.json", "v2_baseline/dummy_dev_definitions.json"):
        return payload
    return payload  # worker_call_order traces are already canonical


def _flow_relabelled(files):
    """The flow's three files share one label space: canonicalize them together when all are present."""
    names = ("v2_baseline/flow_responses.json", "v2_baseline/flow_events.json", "v2_baseline/flow_rows.json")
    if all(name in files for name in names):
        template = {"responses": files[names[0]], "events": files[names[1]], "rows": files[names[2]]}
        canonical = canonical_sections(template, ("responses", "events", "rows"))
        return dict(zip(names, (canonical["responses"], canonical["events"], canonical["rows"])))
    return {}


def compare_with_fixtures(files, fixture_root):
    """[(relative, verdict, detail)] for every captured file against the fixture root.

    Verdicts: "identical" (same bytes), "equivalent" (same template up to
    label numbering and row order), "different", "missing" (no fixture file).
    Partial payloads (``--only``) compare only the captured entries.
    """
    fixture_root = Path(fixture_root)
    flow_captured = _flow_relabelled(files)
    stored = {relative: json.loads((fixture_root / relative).read_text())
              for relative in files if (fixture_root / relative).exists()}
    flow_stored = _flow_relabelled(stored)
    report = []
    for relative, payload in files.items():
        path = fixture_root / relative
        if relative not in stored:
            report.append((relative, "missing", str(path)))
            continue
        fixture = stored[relative]
        if relative in ("v2_baseline/user_slots.json", "v2_baseline/legacy_compat.json",
                        "v2_baseline/write_requests.json"):
            fixture_part = {name: fixture[name] for name in payload if name in fixture}
            absent = [name for name in payload if name not in fixture]
            if absent:
                report.append((relative, "missing", f"fixture has no entry {absent}"))
                continue
        else:
            fixture_part = fixture
        if render_file(relative, payload) == path.read_text():
            report.append((relative, "identical", ""))
            continue
        left = flow_captured.get(relative, _canonical_file(relative, payload))
        right = flow_stored.get(relative, _canonical_file(relative, fixture_part))
        difference = first_difference(right, left)
        report.append((relative, "equivalent" if difference is None else "different", difference or ""))
    return report


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def refuse_fixture_path(output_dir, fixture_root=FIXTURE_ROOT):
    """The output folder must not be the repository fixture tree or anything inside it."""
    output = Path(output_dir).resolve()
    fixtures = Path(fixture_root).resolve()
    if output == fixtures or fixtures in output.parents:
        raise ValueError(f"--output-dir must not be inside the repository fixtures ({fixtures})")
    return output


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--output-dir", type=Path, help="Required. Folder for the captured files (never tests/fixtures).")
    parser.add_argument("--only", action="append", default=[],
                        help="KIND or KIND:NAME to capture (v2_baseline[:flow|legacy_active_session|"
                             "legacy_new_session|dummy_dev_definitions], write_requests[:FLOW], "
                             "worker_call_order[:spy|memory]); repeatable. Default: everything.")
    parser.add_argument("--check", action="store_true", help="Compare the capture with the current fixtures; exit 1 on a difference.")
    parser.add_argument("--fixture-root", type=Path, default=FIXTURE_ROOT, help="Fixture tree for --check.")
    parser.add_argument("--python", default=sys.executable, help="Interpreter for the two worker processes.")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--raw-output", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--clock-shift-days", type=int, default=0, help=argparse.SUPPRESS)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        selection = parse_selection(args.only)
    except ValueError as error:
        parser.error(str(error))
    if args.worker:
        raw = run_worker(selection, args.clock_shift_days)
        args.raw_output.write_text(json.dumps(raw))
        return 0
    if args.output_dir is None:
        parser.error("the following arguments are required: --output-dir")
    try:
        output = refuse_fixture_path(args.output_dir)
    except ValueError as error:
        parser.error(str(error))
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="arc-capture-") as directory:
        try:
            first, second = _spawn_workers(selection, directory, args.python)
            templates = build_templates(first, second)
        except CaptureError as error:
            print(f"capture failed: {error}", file=sys.stderr)
            return 1
    files = golden_files(templates)
    for relative, payload in files.items():
        path = output / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_file(relative, payload))
        print(f"wrote {path}")
    if not args.check:
        return 0
    failed = False
    for relative, verdict, detail in compare_with_fixtures(files, args.fixture_root):
        failed = failed or verdict not in ("identical", "equivalent")
        print(f"{verdict:>10}  {relative}" + (f"  {detail}" if detail else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

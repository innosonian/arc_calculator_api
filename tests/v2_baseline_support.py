"""Scripted /api/v2 observations and the template matcher for the U0 baseline.

The fixtures under tests/fixtures/v2_baseline/ are characterization baselines
captured from the code before /mock/v1 removal (not calculation goldens). A
test runs the same script on today's code and matches the observation against
the stored template; it never compares a run with itself.

Template placeholders stand for values that are random or wall-clock derived
in every run. Each labelled placeholder binds one value, consistently and
one-to-one, across the whole document (so "ATTEMPT#<uuid:7>" must refer to the
same attempt that an earlier response returned):

  <uuid:N>   a lowercase UUID               <hex:N>  32 lowercase hex chars
  <h:N>      64 lowercase hex chars (a digest of random or wall-clock input)
  <secret>   a token/credential/nonce tail  <wallclock>  10-digit epoch second
  <date>     YYYY-MM-DD (wall clock)        <ms>     any non-negative int duration

Everything else, including deterministic digests such as definition_hash and
scope_key, must match literally. Diagnostic *_ms durations are <ms> in the
template; the calculation body is reduced before matching (calculation_summary).
"""

import hashlib
import json
from pathlib import Path
import re
import uuid

from tests.journey_support import EventLog, V2Journey, dummy_course
from tests.legacy_rows_support import legacy_resume_credential, seed_legacy_rows


GOLDEN = Path(__file__).parent / "fixtures" / "v2_baseline"

_PLACEHOLDER = re.compile(r"<(uuid|hex|h):(\d+)>|<(secret|wallclock|date|ms)>")
_PATTERNS = {
    "uuid": r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    "hex": r"[0-9a-f]{32}", "h": r"[0-9a-f]{64}", "secret": r"[A-Za-z0-9_-]{16,128}",
    "wallclock": r"[0-9]{10}", "date": r"[0-9]{4}-[0-9]{2}-[0-9]{2}",
}


LEGACY_SECTIONS = ("responses", "events", "rows", "user_slots", "checkpoints")


def load(name):
    return json.loads((GOLDEN / name).read_text())


def flow_template():
    return {"responses": load("flow_responses.json"), "events": load("flow_events.json"),
            "rows": load("flow_rows.json"), "user_slots": load("user_slots.json")["flow"]}


def legacy_template(name):
    return {**load("legacy_compat.json")[name], "user_slots": load("user_slots.json")[name]}


# ---------------------------------------------------------------------------
# Observation shaping (deterministic transforms applied before matching)
# ---------------------------------------------------------------------------

def calculation_summary(calculation):
    """The v2 layer passes the stored calculator JSON through unchanged.

    The baseline keeps its top-level keys, total score and chart URL, not the
    scoring detail that belongs to calculator goldens (the whole body is
    compared across a restart in tests/test_journey_support.py).
    """
    if type(calculation) is not dict:
        return calculation
    total = (calculation.get("cpr_score") or {}).get("total_score") or {}
    return {"<calculation>": {"keys": sorted(calculation), "overall": total.get("overall"),
                              "chart_dataset_url": calculation.get("chart_dataset_url")}}


def _shape_body(body):
    if type(body) is dict and type(body.get("data")) is dict and "calculation" in body["data"]:
        body = {**body, "data": {**body["data"], "calculation": calculation_summary(body["data"]["calculation"])}}
    return body


def split_rows(rows):
    """Row snapshot without USER.slots, plus USER.slots recorded on its own.

    /mock/v1 removal approves exactly one storage difference: a new USER row
    has no slots. Keeping slots out of the row template makes that change a
    one-entry edit of user_slots ({"slots_present": false}).
    """
    shaped, slots = [], {}
    for row in rows:
        row = dict(row)
        if row["PK"].startswith("USER#"):
            key = f'{row["PK"]}/{row["SK"]}'
            slots[key] = ({"slots_present": True, "slots": row.pop("slots")} if "slots" in row
                          else {"slots_present": False})
        shaped.append(row)
    return shaped, slots


class Recorder:
    """Wrap a V2Journey so every HTTP reply is recorded in call order."""

    def __init__(self, harness):
        self.h = harness
        self.responses = []
        self.checkpoints = []
        original = harness.call

        def call(method, path, **kwargs):
            reply = original(method, path, **kwargs)
            self.responses.append({"method": method, "path": path, "status": reply.status,
                                   "headers": reply.headers, "body": _shape_body(reply.body)})
            return reply

        harness.call = call

    def checkpoint(self, label):
        """Record the full USER row (slots included) at a named point of a legacy script."""
        self.checkpoints.append({"label": label, "user": self.h.store.row("USER#dummy-tester", "STATE")})

    def observation(self):
        rows, slots = split_rows(self.h.store.rows())
        observed = {"responses": self.responses, "events": list(self.h.events.records),
                    "rows": rows, "user_slots": slots}
        if self.checkpoints:
            observed["checkpoints"] = self.checkpoints
        return observed


def _harness(store, **options):
    h = V2Journey(store, events=EventLog(), **options)
    return h, Recorder(h)


# ---------------------------------------------------------------------------
# Scripts
# ---------------------------------------------------------------------------

def definition_observation():
    """DummyDev 15-course bundles: definitionHash and exact execution definition bytes."""
    from mock_journey.catalog import Catalog, PROGRAMS, TARGETS
    from mock_journey.course_settings import fixture_course_settings
    from mock_journey.dev_course import DummyDevCourseProvider
    from mock_journey.execution_definitions import execution_catalog

    execution = execution_catalog()
    provider = DummyDevCourseProvider(settings=fixture_course_settings(), execution=execution)
    catalog = Catalog(execution)
    courses = []
    for binding in provider.list_assignments(provider.learner):
        bundle = provider.fetch_bundle(binding)
        mapping = provider.mapping_document["mappings"][bundle.placements[0].source_id]
        courses.append({
            "course_id": bundle.public_ids.course_id, "enrollment_id": bundle.public_ids.enrollment_id,
            "program_id": mapping["program_id"], "target": mapping["target"],
            "definition_hash": bundle.definition_hash,
            "catalog_definition": catalog.definition(mapping["program_id"], mapping["target"]),
            "placements": [{
                "source_id": placement.source_id, "public_link_id": placement.public_link_id,
                "kind": placement.kind, "execution_json": placement.execution_json.decode("utf-8"),
            } for placement in bundle.placements],
        })
    return {
        "catalog_pairs": [[program[0], target] for program in PROGRAMS for target in TARGETS],
        "courses": courses,
        "mapping_document_sha256": hashlib.sha256(json.dumps(
            provider.mapping_document, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
    }


def golden_flow(store):
    """One learner: practice (replay, reauthorize, cancel, redo), final assessment, logout, re-login.

    Covers login, session, course list/detail, attempt create and replay,
    attempt view, same-session reauthorize, cancel, upload, pending result,
    Relay -> queue -> Worker calculation, result, upload replay, chart link,
    refresh, logout, revoked-session rejection and cross-session reauthorize
    of an evaluated attempt. Returns the observation (responses, events, rows,
    user_slots).
    """
    h, recorder = _harness(store)
    course = dummy_course("mock-compression-only", "adult")

    first = h.login()
    h.session(first.token)
    h.courses(first.token)
    h.course(first.token, course)
    request_id = str(uuid.uuid4())
    practice = h.start(first.token, course, course.practice_link_id, client_request_id=request_id)
    h.advance(1)
    h.start(first.token, course, course.practice_link_id, client_request_id=request_id, expected=200)
    h.attempt(first.token, practice["attemptId"])
    h.reauthorize(first.token, practice["attemptId"], practice["resumeCredential"])
    h.cancel(first.token, practice["attemptId"])
    h.attempt(first.token, practice["attemptId"])
    h.advance(1)
    redo = h.start(first.token, course, course.practice_link_id)
    upload = h.upload_event(first.token, redo["attemptId"], redo["condition"])
    h.expect(202, "POST", upload["path"], event=upload)
    h.result(first.token, redo["attemptId"], expected=202)
    h.advance(1)
    assert set(h.relay()) == {h.job_id(redo["attemptId"])}
    assert h.deliver() == {"batchItemFailures": []}
    h.result(first.token, redo["attemptId"])
    h.expect(200, "POST", upload["path"], event=upload)
    h.chart_link(first.token, redo["attemptId"])
    h.advance(1)
    final = h.start(first.token, course, course.final_link_id)
    h.upload(first.token, final["attemptId"], final["condition"])
    h.advance(1)
    assert set(h.relay()) == {h.job_id(final["attemptId"])}
    assert h.deliver() == {"batchItemFailures": []}
    h.result(first.token, final["attemptId"])
    h.course(first.token, course)
    h.courses(first.token)
    h.refresh(first.token)
    h.logout(first.token)
    h.session(first.token, expected=403)
    h.advance(1)
    second = h.login()
    h.attempt(second.token, redo["attemptId"], expected=404)
    h.reauthorize(second.token, redo["attemptId"], redo["resumeCredential"])
    h.result(second.token, redo["attemptId"])
    h.logout(second.token)
    return recorder.observation()


def legacy_active_session(store, *, seed=seed_legacy_rows):
    """R1-R6 while the legacy /mock/v1 session is still active (the app keeps its token).

    Legacy attempts are viewable, the evaluated result and chart link are
    served, the queued legacy job is finished by Relay -> queue -> Worker with
    legacy slot progress, the created attempt is cancelled, and logout resets
    the legacy slots. ``seed`` is the fixture seeding function; the write
    baseline passes one whose writes are excluded from the recording (E-07).
    """
    seeded = seed(store)
    h, recorder = _harness(store, objects=seeded.objects, start=seeded.meta["capture_clock"] + 60)
    attempts = seeded.attempts
    token = seeded.issue_session_token()
    recorder.checkpoint("seeded")
    h.session(token)
    for label in ("created", "queued", "evaluated"):
        h.attempt(token, attempts[label]["attempt_id"])
    h.result(token, attempts["evaluated"]["attempt_id"])
    h.chart_link(token, attempts["evaluated"]["attempt_id"])
    h.result(token, attempts["queued"]["attempt_id"], expected=202)
    h.result(token, attempts["created"]["attempt_id"], expected=409)
    h.advance(1)
    sent = h.relay()
    assert set(sent) == {attempts["queued"]["job_id"]}, sent
    assert h.deliver() == {"batchItemFailures": []}
    recorder.checkpoint("queued_finished")
    h.result(token, attempts["queued"]["attempt_id"])
    h.attempt(token, attempts["queued"]["attempt_id"])
    h.cancel(token, attempts["created"]["attempt_id"])
    recorder.checkpoint("created_cancelled")
    h.attempt(token, attempts["created"]["attempt_id"])
    h.logout(token)
    recorder.checkpoint("logged_out")
    return recorder.observation()


def legacy_new_session(store, *, seed=seed_legacy_rows):
    """R1-R6 after the legacy session expired: a /api/v2 login reauthorizes legacy attempts.

    The existing USER row (with slots) is kept by login, another session cannot
    see the legacy attempts until reauthorize, and the reauthorized attempts
    can be cancelled, finished by the Worker and read. ``seed`` as in
    ``legacy_active_session``.
    """
    seeded = seed(store)
    h, recorder = _harness(store, objects=seeded.objects,
                           start=seeded.meta["session_expires_at"] + 60)
    attempts = seeded.attempts
    recorder.checkpoint("seeded")
    session = h.login()
    recorder.checkpoint("v2_login")
    h.courses(session.token)
    h.attempt(session.token, attempts["created"]["attempt_id"], expected=404)
    for label in ("created", "queued", "evaluated"):
        h.reauthorize(session.token, attempts[label]["attempt_id"],
                      legacy_resume_credential(seeded, label))
    h.cancel(session.token, attempts["created"]["attempt_id"])
    recorder.checkpoint("created_cancelled")
    h.advance(1)
    assert h.work(attempts["queued"]["attempt_id"]) is True
    recorder.checkpoint("queued_finished")
    h.result(session.token, attempts["queued"]["attempt_id"])
    h.result(session.token, attempts["evaluated"]["attempt_id"])
    h.chart_link(session.token, attempts["queued"]["attempt_id"])
    h.logout(session.token)
    recorder.checkpoint("logged_out")
    return recorder.observation()


# ---------------------------------------------------------------------------
# Template matching
# ---------------------------------------------------------------------------

class _Bindings:
    def __init__(self):
        self.forward, self.backward = {}, {}

    def copy(self):
        other = _Bindings()
        other.forward, other.backward = dict(self.forward), dict(self.backward)
        return other

    def bind(self, label, value):
        if label in self.forward:
            return self.forward[label] == value
        if value in self.backward:
            return False
        self.forward[label], self.backward[value] = value, label
        return True


def _string_regex(template):
    parts, groups, position = [], [], 0
    for match in _PLACEHOLDER.finditer(template):
        parts.append(re.escape(template[position:match.start()]))
        if match.group(1):
            kind, label = match.group(1), f"{match.group(1)}:{match.group(2)}"
            groups.append(label)
            parts.append(f"({_PATTERNS[kind]})")
        else:
            if match.group(3) == "ms":
                raise AssertionError("<ms> is only a whole-value placeholder")
            groups.append(None)
            parts.append(f"({_PATTERNS[match.group(3)]})")
        position = match.end()
    parts.append(re.escape(template[position:]))
    return re.compile("".join(parts) + r"\Z", re.S), groups


def _match(expected, observed, bindings, path):
    if type(expected) is str and _PLACEHOLDER.search(expected):
        if expected == "<ms>":
            if type(observed) is int and observed >= 0:
                return
            raise AssertionError(f"{path}: expected a duration, observed {observed!r}")
        if type(observed) is not str:
            raise AssertionError(f"{path}: expected {expected!r}, observed {observed!r}")
        regex, groups = _string_regex(expected)
        found = regex.match(observed)
        if found is None:
            raise AssertionError(f"{path}: expected {expected!r}, observed {observed!r}")
        for label, value in zip(groups, found.groups()):
            if label is not None and not bindings.bind(label, value):
                raise AssertionError(f"{path}: {label} is inconsistent ({observed!r})")
        return
    if type(expected) is dict:
        if type(observed) is not dict:
            raise AssertionError(f"{path}: expected an object, observed {observed!r}")
        if set(expected) != set(observed):
            raise AssertionError(f"{path}: keys differ (missing={sorted(set(expected) - set(observed))}, "
                                 f"unexpected={sorted(set(observed) - set(expected))})")
        for key in expected:
            _match(expected[key], observed[key], bindings, f"{path}.{key}")
        return
    if type(expected) is list:
        if type(observed) is not list or len(expected) != len(observed):
            raise AssertionError(f"{path}: expected {len(expected)} items, observed "
                                 f"{len(observed) if type(observed) is list else observed!r}")
        for index, (left, right) in enumerate(zip(expected, observed)):
            _match(left, right, bindings, f"{path}[{index}]")
        return
    if type(expected) is not type(observed) or expected != observed:
        raise AssertionError(f"{path}: expected {expected!r}, observed {observed!r}")


def _row_key(row):
    return f'{row["PK"]}/{row["SK"]}'


def _match_rows(expected_rows, observed_rows, bindings, path):
    remaining = list(observed_rows)
    for index, expected in enumerate(expected_rows):
        candidates = []
        for row in remaining:
            trial = bindings.copy()
            try:
                _match(_row_key(expected), _row_key(row), trial, f"{path}[{index}].key")
            except AssertionError:
                continue
            candidates.append(row)
        if not candidates:
            raise AssertionError(f"{path}[{index}]: no stored row matches {_row_key(expected)!r}")
        errors = []
        for row in candidates:
            trial = bindings.copy()
            try:
                _match(expected, row, trial, f"{path}[{_row_key(expected)}]")
            except AssertionError as error:
                errors.append(str(error))
                continue
            bindings.forward, bindings.backward = trial.forward, trial.backward
            remaining.remove(row)
            break
        else:
            raise AssertionError(errors[0])
    if remaining:
        raise AssertionError(f"{path}: unexpected stored rows {[_row_key(row) for row in remaining]}")


def assert_matches(template, observation, *, sections=("responses", "events", "rows", "user_slots")):
    """Match an observation to a stored template with one-to-one placeholder bindings.

    Responses and events are matched first (their order is part of the
    contract), so rows are then resolved against identifiers already bound.
    """
    bindings = _Bindings()
    for section in sections:
        if section == "rows":
            _match_rows(template["rows"], observation["rows"], bindings, "rows")
        else:
            _match(template[section], observation[section], bindings, section)
        if section == "responses":
            _check_wire_order(template["responses"], observation["responses"])
    return bindings


def _check_wire_order(expected, observed):
    """The envelope and its data/error object have a fixed field order on the wire.

    Nested objects copied from stored maps (evaluation, progressApplication)
    follow the storage backend's map order, so only these two levels are checked.
    """
    for index, (left, right) in enumerate(zip(expected, observed)):
        body_left, body_right = left["body"], right["body"]
        if type(body_left) is not dict:
            continue
        if list(body_left) != list(body_right):
            raise AssertionError(f"responses[{index}].body: wire key order differs {list(body_right)}")
        for key in ("data", "error"):
            if type(body_left.get(key)) is dict and list(body_left[key]) != list(body_right[key]):
                raise AssertionError(f"responses[{index}].body.{key}: wire key order differs {list(body_right[key])}")

"""Reject unsafe configuration before any runtime dependency is constructed."""

from dataclasses import FrozenInstanceError, MISSING, fields, replace
import json
from pathlib import Path
import subprocess
import sys

import pytest

from mock_journey.settings import ApiSettings, RelaySettings, StateSettings, StorageSettings, WorkerSettings


STATE = StateSettings("explicit-local-table", 8)
STORAGE = StorageSettings("test", "explicit-local-bucket", "rt/data", 100, 200)
API = ApiSettings(STATE, STORAGE, "explicit-local-environment", 300)
WORKER = WorkerSettings(STATE, STORAGE, 7, 9)
RELAY = RelaySettings(STATE, "https://queue.example.test/explicit-account/queue", 11, 13, 3, 2)
SETTINGS = (STATE, STORAGE, API, WORKER, RELAY)


def reject(value, **changes):
    with pytest.raises(ValueError) as raised:
        replace(value, **changes)
    assert str(raised.value) == "Invalid journey settings."
    assert raised.value.__cause__ is None
    return raised.value


@pytest.mark.parametrize("value", SETTINGS)
def test_every_value_is_explicit_and_frozen(value):
    for field in fields(value):
        assert field.default is MISSING and field.default_factory is MISSING
    with pytest.raises(FrozenInstanceError):
        setattr(value, fields(value)[0].name, "replacement")


@pytest.mark.parametrize("value,field", [
    (STATE, "max_conflict_retries"), (STORAGE, "input_bytes"), (STORAGE, "artifact_bytes"),
    (API, "payload_limit"), (WORKER, "lease_seconds"), (WORKER, "retry_seconds"),
    (RELAY, "lease_seconds"), (RELAY, "retry_seconds"), (RELAY, "page_size"), (RELAY, "max_pages"),
])
def test_positive_limits_reject_coercion_bool_and_invalid_types(value, field):
    for invalid in (True, False, 0, -1, 1.0, "1", None, [], {}):
        reject(value, **{field: invalid})


def test_state_retains_existing_bounded_conflict_budget():
    assert replace(STATE, max_conflict_retries=1).max_conflict_retries == 1
    reject(STATE, max_conflict_retries=9)


@pytest.mark.parametrize("value,field", [
    (STATE, "table_name"), (STORAGE, "stage"), (STORAGE, "bucket"),
    (STORAGE, "directory"), (API, "environment"), (RELAY, "queue_url"),
])
def test_strings_require_exact_nonempty_utf8_without_coercion(value, field):
    class StringSubclass(str):
        pass

    for invalid in ("", None, 1, True, b"text", StringSubclass("text"), "PRIVATE\ud800MARKER"):
        reject(value, **{field: invalid})


def test_environment_length_is_existing_character_limit_and_values_are_not_normalized():
    assert replace(API, environment="한" * 128).environment == "한" * 128
    reject(API, environment="한" * 129)
    assert replace(API, environment=" e\u0301 ").environment == " e\u0301 "
    assert replace(STATE, table_name="Explicit/Table:Binding").table_name == "Explicit/Table:Binding"
    assert replace(STORAGE, bucket="Explicit:Bucket:Binding").bucket == "Explicit:Bucket:Binding"


@pytest.mark.parametrize("field", ["stage", "directory"])
def test_storage_segments_keep_existing_path_rules(field):
    for invalid in (".", "..", "/", "/leading", "trailing/", "a//b", "a/../b", "a\\b", "a b", "한글", "a" * 129):
        reject(STORAGE, **{field: invalid})
    accepted = "a_-0" if field == "stage" else "a_-0/b_1/c-2"
    assert getattr(replace(STORAGE, **{field: accepted}), field) == accepted
    assert getattr(replace(STORAGE, **{field: "a" * 128}), field) == "a" * 128


def test_storage_and_role_limits_preserve_only_existing_relations():
    reject(STORAGE, input_bytes=201)
    assert replace(STORAGE, artifact_bytes=100).artifact_bytes == 100
    # No guessed ingress/measurement ratio, lease/retry ratio, or upper limit.
    assert replace(API, payload_limit=1).payload_limit == 1
    assert replace(WORKER, lease_seconds=1000, retry_seconds=1).retry_seconds == 1
    assert replace(RELAY, page_size=10**12, max_pages=10**12).max_pages == 10**12
    # test is valid syntax; write-confirmation/persistence remain storage concerns.
    assert STORAGE.stage == "test"


@pytest.mark.parametrize("value,field", [(API, "state"), (API, "storage"), (WORKER, "state"),
                                         (WORKER, "storage"), (RELAY, "state")])
def test_roles_reject_dicts_wrong_settings_and_subclassed_settings(value, field):
    expected = getattr(value, field)
    subclass = type("SubclassedSettings", (type(expected),), {})
    inherited = subclass(**{item.name: getattr(expected, item.name) for item in fields(expected)})
    for invalid in (None, {}, API, inherited):
        reject(value, **{field: invalid})


@pytest.mark.parametrize("url", [
    "http://queue.example.test/q", "HTTPS://queue.example.test/q", "//queue.example.test/q",
    "https://", "https:///queue", "https://:443/queue", "https://user:PRIVATE@queue.example.test/q",
    "https://@queue.example.test/q", "https://queue.example.test/q?", "https://queue.example.test/q?token=PRIVATE",
    "https://queue.example.test/q#", "https://queue.example.test/q#PRIVATE", "https://queue.example.test\\evil/q",
    " https://queue.example.test/q", "https://queue.example.test/a b", "https://queue.example.test/a\tb",
    "https://queue.example.test/a\nb", "https://queue.example.test/a\rb", "https://queue.example.test/a\0b",
    "https://queue.example.test/a\x7fb", "https://queue.example.test/a\x80b", "https://queue.example.test/a\u00a0b",
    "https://queue.example.test:PRIVATE/q", "https://queue.example.test:65536/q", "https://queue.example.test:-1/q",
    "https://[invalid]/q", "https://[::1/q", "https://queue.example.test\uff0fPRIVATE/q",
    "https://[::1]PRIVATE/q", "https://[::1]PRIVATE:443/q",
])
def test_queue_destination_rejects_ambiguous_or_credential_bearing_syntax(url):
    error = reject(RELAY, queue_url=url)
    assert "PRIVATE" not in str(error)


@pytest.mark.parametrize("url", [
    "https://queue.example.test/explicit-account/queue",
    "https://queue.example.test:443/q", "https://[::1]:443/q", "https://127.0.0.1/q",
    "https://queue.example.test/path%20component", "https://queue.example.test/path@component",
])
def test_queue_validation_preserves_syntax_without_destination_approval(url):
    assert replace(RELAY, queue_url=url).queue_url == url


def test_each_role_requires_only_its_own_constructor_settings():
    assert {field.name for field in fields(RelaySettings)} == {
        "state", "queue_url", "lease_seconds", "retry_seconds", "page_size", "max_pages",
    }
    assert {field.name for field in fields(WorkerSettings)} == {"state", "storage", "lease_seconds", "retry_seconds"}
    assert {field.name for field in fields(ApiSettings)} == {"state", "storage", "environment", "payload_limit"}


def test_isolated_import_and_validation_do_not_read_environment_or_construct_sdk_clients():
    # Run separately so previous imports cannot hide an accidental SDK import.
    root = str(Path(__file__).resolve().parents[1])
    script = """
import os, sys
sys.path.insert(0, ROOT)
class NoEnvironment(dict):
    def __getitem__(self, key): raise AssertionError('Environment read')
    def get(self, key, *args): raise AssertionError('Environment read')
    def __iter__(self): raise AssertionError('Environment read')
    def __contains__(self, key): raise AssertionError('Environment read')
os.environ = NoEnvironment()
def guard(event, args):
    if event.startswith('socket.') or event in ('subprocess.Popen', 'os.system'):
        raise AssertionError('Unexpected I/O')
    if event == 'import' and args[0].split('.')[0] in ('boto3', 'botocore', 'sentry_sdk'):
        raise AssertionError('SDK import')
sys.addaudithook(guard)
from mock_journey.settings import StateSettings, StorageSettings, ApiSettings, WorkerSettings, RelaySettings
state = StateSettings('explicit-table', 8)
storage = StorageSettings('test', 'explicit-bucket', 'rt/data', 1, 2)
ApiSettings(state, storage, 'explicit-environment', 3)
WorkerSettings(state, storage, 4, 5)
RelaySettings(state, 'https://queue.example.test/q', 6, 7, 8, 9)
assert not {'boto3', 'botocore', 'sentry_sdk'} & sys.modules.keys()
""".replace("ROOT", json.dumps(root))
    completed = subprocess.run([sys.executable, "-I", "-S", "-B", "-c", script], env={},
                               capture_output=True, text=True, timeout=10)
    assert completed.returncode == 0, completed.stderr

"""Private file storage counterexamples. No DB, SDK calls or network services."""

import json
import multiprocessing
import os
import socket
import stat
import struct
from urllib.parse import urlsplit

from botocore.exceptions import ClientError
import pytest

from local_server import object_storage as objects
from local_server.charts import LocalChartService
from local_server.database import prepare_material
from mock_journey import typed
from mock_journey.errors import JourneyError


OPTIONS = dict(bucket="local-objects", directory="calculator_result/interpreted_rtdata/arc",
               stage="local-test", artifact_limit=200_000, quota_bytes=2_000_000)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Filesystem test attempted network access.")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(objects.uploader, "client", forbidden)


@pytest.fixture
def store(tmp_path):
    root = tmp_path.resolve() / "installation"
    root.mkdir(mode=0o700)
    local = prepare_material(root)
    material = objects.prepare_object_material(local)
    client = objects.LocalObjectClient(material, **OPTIONS)
    yield local, material, client
    client.close()


def key(client, name="entry.bin"):
    return client.prefix + "private/" + name


def put(client, name="entry.bin", body=b"measurement", metadata=None):
    return client.put_object(Bucket=client.bucket, Key=key(client, name), Body=body, Metadata=metadata)


def read(client, name="entry.bin"):
    return client.get_object(Bucket=client.bucket, Key=key(client, name))["Body"].read()


def path(client, name="entry.bin"):
    return client.material.object_dir / (client._key(client.bucket, key(client, name)) + ".object")


def unavailable(operation, *args, **kwargs):
    with pytest.raises(ClientError) as failure:
        operation(*args, **kwargs)
    assert failure.value.response["Error"]["Code"] == "LocalStorageUnavailable"
    assert "measurement" not in str(failure.value)


def chart(client, name="CPR-ACTION-1700000000-12345678-1234-4234-8234-123456789abc", body=None):
    value = {"values": [None, 1, 1.5, True, "호흡"]} if body is None else body
    chart_key = client.prefix + "_no_org/2023-11-14/" + name + ".json"
    client.put_object(Bucket=client.bucket, Key=chart_key, Body=typed.json_bytes(value))
    return chart_key, typed.json_bytes(value)


def test_material_has_independent_stable_secret_without_database(store):
    local, material, client = store
    assert not local.database_initialized
    assert material.signing_key != local.resume_key
    assert "signing_key=" not in repr(material)
    assert objects.prepare_object_material(local) == material
    assert stat.S_IMODE(material.object_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((material.data_dir / objects._MATERIAL).stat().st_mode) == 0o600
    assert stat.S_IMODE((material.object_dir / "objects.lock").stat().st_mode) == 0o600


def test_ready_checks_owned_lock_without_listing_objects(store, monkeypatch):
    client = store[2]
    def forbidden(*args):
        pytest.fail("Readiness listed private files.")
    monkeypatch.setattr(objects.os, "listdir", forbidden)
    assert client.ready() is True
    client.close()
    with pytest.raises(objects.LocalObjectError):
        client.ready()


def test_injected_legacy_storage_preserves_typed_bytes_without_sdk_or_logs(store, capsys, caplog):
    client = store[2]
    signer = LocalChartService(client, base_url="http://127.0.0.1:8000", clock=lambda: 1000)
    legacy = objects.LocalLegacyBindings(client, signer)
    stem = "CPR-ACTION-1700000000-12345678-1234-4234-8234-123456789abc"
    values = {"score": 80.0, "actions": 80, "nullable": None, "label": "개인"}
    base = legacy.upload_raw_input(b"private-binary", b"private-aed", {"name": "PRIVATE-MARKER"},
                                   stage=client.stage, key_stem=stem, org="_no_org", directory=client.directory)
    published = legacy.upload_json_file(values, directory=client.directory, stage=client.stage,
                                        key_stem=stem, org="_no_org")
    assert client.get_object(Bucket=client.bucket, Key=base + ".bin")["Body"].read() == b"private-binary"
    assert signer.read_path(urlsplit(legacy.create_signed_url(published)).path) == typed.json_bytes(values)
    assert legacy.upload_json_file(values, directory=client.directory, stage=client.stage,
                                  key_stem=stem, org="_no_org") == published
    captured = capsys.readouterr()
    assert captured.out == captured.err == caplog.text == ""


@pytest.mark.parametrize("kind", ["missing_key", "missing_directory", "foreign_key", "wrong_key", "uninitialized"])
def test_missing_or_foreign_installation_is_never_adopted(store, kind):
    local, material, client = store
    client.close()
    record = material.data_dir / objects._MATERIAL
    if kind == "missing_key":
        record.unlink()
    elif kind == "missing_directory":
        (material.object_dir / "objects.lock").unlink()
        material.object_dir.rmdir()
    else:
        value = json.loads(record.read_text())
        if kind == "foreign_key":
            value["installation_id"] = "f" * 32
        elif kind == "wrong_key":
            value["chart_key"] = "private-invalid-key"
        else:
            value["initialized"] = False
        record.write_text(json.dumps(value))
    with pytest.raises(objects.LocalObjectError):
        objects.prepare_object_material(local)


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "directory", "public"])
def test_material_unsafe_files_fail_closed(store, tmp_path, kind):
    local, material, client = store
    record = material.data_dir / objects._MATERIAL
    if kind == "symlink":
        outside = tmp_path / "secret"
        record.rename(outside)
        record.symlink_to(outside)
    elif kind == "hardlink":
        os.link(record, tmp_path / "duplicate")
    elif kind == "fifo":
        record.unlink()
        os.mkfifo(record, 0o600)
    elif kind == "directory":
        record.unlink()
        record.mkdir(mode=0o700)
    else:
        record.chmod(0o644)
    with pytest.raises(objects.LocalObjectError):
        objects.prepare_object_material(local)


def test_roundtrip_immutable_metadata_restart_and_input_preservation(store):
    local, material, client = store
    metadata = {"arc-binding": "a" * 64, "arc-sha256": "b" * 64}
    put(client, metadata=metadata)
    original = path(client).read_bytes()
    assert put(client, metadata=metadata) == {}
    assert path(client).read_bytes() == original
    assert metadata == {"arc-binding": "a" * 64, "arc-sha256": "b" * 64}
    unavailable(put, client, body=b"different")
    unavailable(put, client, metadata={})
    client.close()
    replacement = objects.LocalObjectClient(objects.prepare_object_material(local), **OPTIONS)
    try:
        assert read(replacement) == b"measurement"
        assert replacement.get_object(Bucket=client.bucket, Key=key(client))["Metadata"] == metadata
    finally:
        replacement.close()


@pytest.mark.parametrize("bad", ["../secret", "/private", "a//b", "a/../b", "a/./b", "a\\b", "a%2fb", "a?b", "a\x00b", "한글"])
def test_keys_cannot_become_paths(store, bad):
    client = store[2]
    unavailable(client.put_object, Bucket=client.bucket, Key=client.prefix + bad, Body=b"private")


@pytest.mark.parametrize("body", [None, "secret", bytearray(b"abc"), {}, 1, True])
def test_body_type_not_coerced(store, body):
    unavailable(put, store[2], body=body)


@pytest.mark.parametrize("metadata", [{"Authorization": "secret"}, {"arc-binding": "secret"}, [], {"arc-chart": 1}])
def test_metadata_never_accepts_credentials_or_wrong_types(store, metadata):
    unavailable(put, store[2], metadata=metadata)


@pytest.mark.parametrize("kind", ["append", "truncate", "checksum", "header_duplicate", "header_bool", "header_foreign", "header_extra", "header_nonfinite"])
def test_envelope_damage_is_unavailable_not_absence(store, kind):
    client = store[2]
    put(client)
    file = path(client)
    raw = file.read_bytes()
    offset = len(objects._MAGIC) + 4
    length = struct.unpack("!I", raw[len(objects._MAGIC):offset])[0]
    if kind == "append":
        raw += b"x"
    elif kind == "truncate":
        raw = raw[:-1]
    elif kind == "checksum":
        raw = raw[:-1] + b"!"
    else:
        value = json.loads(raw[offset:offset + length])
        if kind == "header_bool":
            value["size"] = True
        elif kind == "header_foreign":
            value["installation_id"] = "0" * 32
        elif kind == "header_extra":
            value["private"] = "do not expose"
        header = typed.json_bytes(value)
        if kind == "header_duplicate":
            header = header[:-1] + b', "size": 11}'
        elif kind == "header_nonfinite":
            header = header.replace(b'"size": 11', b'"size": NaN')
        raw = objects._MAGIC + struct.pack("!I", len(header)) + header + raw[offset + length:]
    file.write_bytes(raw)
    unavailable(read, client)
    unavailable(put, client)


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "directory", "public"])
def test_object_files_cannot_escape_private_regular_storage(store, tmp_path, kind):
    client = store[2]
    put(client)
    file = path(client)
    if kind == "symlink":
        outside = tmp_path / "outside"
        file.rename(outside)
        file.symlink_to(outside)
    elif kind == "hardlink":
        os.link(file, tmp_path / "outside")
    elif kind == "fifo":
        file.unlink()
        os.mkfifo(file, 0o600)
    elif kind == "directory":
        file.unlink()
        file.mkdir(mode=0o700)
    else:
        file.chmod(0o644)
    unavailable(read, client)
    unavailable(put, client)


def test_only_missing_object_maps_to_no_such_key(store):
    client = store[2]
    with pytest.raises(ClientError) as failure:
        read(client)
    assert failure.value.response["Error"]["Code"] == "NoSuchKey"
    (client.material.object_dir / "objects.lock").unlink()
    unavailable(read, client)


def test_quota_counts_envelopes_and_stale_temps_but_preserves_read_and_replay(store):
    local, material, client = store
    put(client)
    assert path(client).stat().st_size > len(b"measurement")
    original = path(client).read_bytes()
    client.close()
    client = objects.LocalObjectClient(material, **{**OPTIONS, "quota_bytes": 1})
    try:
        assert read(client) == b"measurement"
        assert put(client) == {}
        unavailable(put, client, name="other.bin")
        assert path(client).read_bytes() == original
    finally:
        client.close()
    stale = material.object_dir / (".object-tmp-" + "a" * 32)
    stale.write_bytes(b"private partial" * 100)
    stale.chmod(0o600)
    client = objects.LocalObjectClient(material, **{**OPTIONS, "quota_bytes": len(original) + stale.stat().st_size})
    try:
        unavailable(put, client, name="other.bin")
        assert stale.exists()
    finally:
        client.close()


def test_limit_counts_actual_bytes_and_never_truncates(store):
    client = store[2]
    assert put(client, body=b"x" * OPTIONS["artifact_limit"]) == {}
    unavailable(put, client, name="oversize.bin", body=b"x" * (OPTIONS["artifact_limit"] + 1))
    assert len(read(client)) == OPTIONS["artifact_limit"]


def test_failed_file_fsync_leaves_no_committed_object_or_owned_temp(store, monkeypatch):
    client = store[2]
    original = objects.os.fsync
    def fail(fd):
        if stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("PRIVATE PATH MUST NOT APPEAR")
        return original(fd)
    monkeypatch.setattr(objects.os, "fsync", fail)
    unavailable(put, client)
    assert not path(client).exists()
    assert list(client.material.object_dir.glob(".object-tmp-*")) == []


def test_failed_directory_fsync_can_retry_same_committed_bytes(store, monkeypatch):
    client = store[2]
    original = objects.os.fsync
    def fail(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("PRIVATE PATH MUST NOT APPEAR")
        return original(fd)
    monkeypatch.setattr(objects.os, "fsync", fail)
    unavailable(put, client)
    assert path(client).exists()
    monkeypatch.setattr(objects.os, "fsync", original)
    assert put(client) == {}
    assert read(client) == b"measurement"


def _child_write(material, opts, name, value, barrier, results):
    client = objects.LocalObjectClient(material, **opts)
    try:
        barrier.wait(timeout=10)
        try:
            put(client, name=name, body=value)
            results.put("ok")
        except ClientError as failure:
            results.put(failure.response["Error"]["Code"])
    finally:
        client.close()


@pytest.mark.parametrize("case", ["same", "collision", "quota"])
def test_cross_process_immutable_put_and_quota(store, case):
    material = store[1]
    context = multiprocessing.get_context("spawn")
    barrier, results = context.Barrier(2), context.Queue()
    opts = dict(OPTIONS)
    if case == "quota":
        opts["quota_bytes"] = 2300  # one 1400-byte body + full header, never two
    names = ["same.bin", "other.bin" if case == "quota" else "same.bin"]
    values = [b"x" * 1400, (b"y" if case == "collision" else b"x") * 1400]
    children = [context.Process(target=_child_write, args=(material, opts, names[i], values[i], barrier, results)) for i in range(2)]
    for child in children:
        child.start()
    for child in children:
        child.join(timeout=20)
        if child.is_alive():
            child.terminate()
            child.join(timeout=5)
        assert child.exitcode == 0
    statuses = sorted(results.get(timeout=2) for _ in children)
    assert statuses == (["ok", "ok"] if case == "same" else ["LocalStorageUnavailable", "ok"])
    assert sum(file.stat().st_size for file in material.object_dir.iterdir()) <= opts["quota_bytes"]


def _child_crash(material, after_rename):
    client = objects.LocalObjectClient(material, **OPTIONS)
    original = objects.os.replace
    def crash(*args, **kwargs):
        if after_rename:
            original(*args, **kwargs)
        os._exit(73)
    objects.os.replace = crash
    put(client)


@pytest.mark.parametrize("after_rename", [False, True])
def test_process_crash_never_exposes_partial_object(store, after_rename):
    material, client = store[1:]
    child = multiprocessing.get_context("spawn").Process(target=_child_crash, args=(material, after_rename))
    child.start()
    child.join(timeout=20)
    if child.is_alive():
        child.terminate()
        child.join(timeout=5)
    assert child.exitcode == 73
    leftovers = list(material.object_dir.glob(".object-tmp-*"))
    if after_rename:
        assert read(client) == b"measurement"
        assert leftovers == []
        assert put(client) == {}  # repeats directory fsync before success
    else:
        assert len(leftovers) == 1
        assert stat.S_IMODE(leftovers[0].stat().st_mode) == 0o600
        with pytest.raises(ClientError) as failure:
            read(client)
        assert failure.value.response["Error"]["Code"] == "NoSuchKey"
        assert put(client) == {}
        assert leftovers[0].exists()  # no automatic deletion of crash evidence


def test_pending_empty_storage_initialization_can_resume(store):
    local, material, client = store
    client.close()
    (material.object_dir / "objects.lock").unlink()
    record = material.data_dir / objects._MATERIAL
    value = json.loads(record.read_text())
    value["initialized"] = False
    record.write_text(json.dumps(value))
    assert objects.prepare_object_material(local) == material
    assert json.loads(record.read_text())["initialized"] is True


def test_chart_capability_stable_across_restart_and_exact_expiry(store):
    local, material, client = store
    chart_key, body = chart(client)
    now = [1000]
    signer = LocalChartService(client, base_url="http://127.0.0.1:8000", clock=lambda: now[0])
    url = signer.create_signed_url(chart_key)
    capability_path = urlsplit(url).path
    assert signer.read_path(capability_path) == body
    client.close()
    again = objects.LocalObjectClient(objects.prepare_object_material(local), **OPTIONS)
    try:
        signer = LocalChartService(again, base_url="http://127.0.0.1:8000", clock=lambda: now[0])
        now[0] = 1299
        assert signer.read_path(capability_path) == body
        now[0] = 1300
        with pytest.raises(JourneyError) as failure:
            signer.read_path(capability_path)
        assert failure.value.code == "NOT_FOUND"
    finally:
        again.close()


@pytest.mark.parametrize("change", ["signature", "issued", "expires", "identity", "digest", "host", "installation"])
def test_chart_capability_binding_cannot_be_changed(store, tmp_path, change):
    client = store[2]
    chart_key, _ = chart(client)
    signer = LocalChartService(client, base_url="http://127.0.0.1:8000", clock=lambda: 1000)
    capability = urlsplit(signer.create_signed_url(chart_key)).path
    if change == "host":
        signer = LocalChartService(client, base_url="http://127.0.0.1:8001", clock=lambda: 1000)
    elif change == "installation":
        root = tmp_path.resolve() / "other"
        root.mkdir(mode=0o700)
        other = objects.LocalObjectClient(objects.prepare_object_material(prepare_material(root)), **OPTIONS)
        signer = LocalChartService(other, base_url="http://127.0.0.1:8000", clock=lambda: 1000)
    else:
        parts = capability.split(".")
        position = {"signature": -1, "issued": 1, "expires": 2, "identity": 3, "digest": 4}[change]
        value = parts[position]
        parts[position] = ("1" if value[0] != "1" else "2") + value[1:]
        capability = ".".join(parts)
    try:
        with pytest.raises(JourneyError) as failure:
            signer.read_path(capability)
        assert failure.value.code == "NOT_FOUND"
    finally:
        if change == "installation":
            other.close()


@pytest.mark.parametrize("suffix", [".bin", ".aed.bin", ".meta.json", ".request.json", "/candidate.json", ".json"])
def test_raw_meta_candidate_and_bad_chart_json_cannot_be_signed(store, suffix):
    client = store[2]
    chart_key = client.prefix + "_no_org/2023-11-14/CPR-ACTION-1700000000-12345678-1234-4234-8234-123456789abc" + suffix
    client.put_object(Bucket=client.bucket, Key=chart_key, Body=b"42")
    signer = LocalChartService(client, base_url="http://127.0.0.1:8000", clock=lambda: 1000)
    with pytest.raises(JourneyError) as failure:
        signer.create_signed_url(chart_key)
    assert failure.value.code == "TEMPORARILY_UNAVAILABLE"


@pytest.mark.parametrize("value", [True, None, "1000", float("nan"), float("inf"), -1])
def test_invalid_clock_is_fixed_unavailable(store, value):
    signer = LocalChartService(store[2], base_url="http://127.0.0.1:8000", clock=lambda: value)
    with pytest.raises(JourneyError) as failure:
        signer.read_path("/local/v1/charts/private")
    assert failure.value.code == "TEMPORARILY_UNAVAILABLE"

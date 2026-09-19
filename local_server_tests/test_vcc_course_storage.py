"""Course blob restart tests use the real private filesystem adapter, no sockets."""

from types import SimpleNamespace

import pytest

from local_server.database import prepare_material
from local_server.object_storage import LocalObjectClient, prepare_object_material
from mock_journey.course_storage import CourseBlobStore
from mock_journey.storage import JourneyStorage


def test_private_course_blob_survives_closed_store_and_new_instance(tmp_path):
    root = tmp_path / "installation"
    root.mkdir(mode=0o700)
    material = prepare_object_material(prepare_material(root))
    options = dict(bucket="course-test", directory="private/course-test", stage="test",
                   artifact_limit=10000, quota_bytes=100000)
    legacy = SimpleNamespace(bucket=options["bucket"], directory=options["directory"])
    def blobs(client):
        return CourseBlobStore(JourneyStorage(client, legacy_bindings=legacy, stage="test",
            limits={"input_bytes":1000,"artifact_bytes":10000}))
    body = b'{"schema":"course-test","progress":null}'
    first = LocalObjectClient(material, **options)
    try:
        ref = blobs(first).put_bytes(body)
        assert blobs(first).put_bytes(body) == ref
    finally:
        first.close()
    second = LocalObjectClient(material, **options)
    try:
        assert blobs(second).get_bytes(ref) == body
        assert blobs(second).get_bytes("0"*64) is None
        assert len(list(material.object_dir.glob("*.object"))) == 1
    finally:
        second.close()


def test_course_blob_wrong_hash_or_tamper_fails_closed():
    from mock_journey.course_errors import CourseError
    from tests.test_mock_storage import setup_storage
    storage, client, *_ = setup_storage()
    blobs = CourseBlobStore(storage)
    ref = blobs.put_bytes(b'{"course":1}')
    path = (storage.bucket, storage.prefix+"_course/"+ref+".json")
    client.objects[path]["Body"] = b'{"course":2}'
    with pytest.raises(CourseError) as failed:
        blobs.get_bytes(ref)
    assert failed.value.code == "TEMPORARILY_UNAVAILABLE"
    with pytest.raises(CourseError):
        blobs.get_bytes("../other")

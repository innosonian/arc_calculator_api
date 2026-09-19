"""Test-only process entrypoint; private fixture credentials arrive via stdin."""

import hashlib
import json
import os
from pathlib import Path
import sys

import boto3
from botocore.config import Config

from local_server.charts import LocalChartService
from local_server.database import prepare_material
from local_server.object_storage import LocalLegacyBindings, LocalObjectClient, prepare_object_material
from mock_journey.course_response import course_detail_data
from tests.vcc_runtime_support import runtime


def open_files(directory):
    material = prepare_object_material(prepare_material(Path(directory)))
    objects = LocalObjectClient(material, bucket="vcc-process-test", directory="private/vcc-process",
        stage="development", artifact_limit=8_000_000, quota_bytes=64_000_000)
    charts = LocalChartService(objects, base_url="http://127.0.0.1:8123", clock=lambda: 1_800_000_000)
    return objects, LocalLegacyBindings(objects, charts)


def main():
    config = json.load(sys.stdin)
    # This helper accepts only the parent runner's loopback fixture endpoint.
    from urllib.parse import urlsplit
    endpoint = urlsplit(config["endpoint"])
    assert endpoint.scheme == "http" and endpoint.hostname == "127.0.0.1" and endpoint.port
    client = boto3.client("dynamodb", endpoint_url=config["endpoint"], region_name="us-east-2",
        aws_access_key_id="ARCLocalTestAccess", aws_secret_access_key="ARCLocalTestSecret",
        aws_session_token="ARCLocalTestSession", config=Config(proxies={}, connect_timeout=2,
            read_timeout=5, retries={"total_max_attempts": 1}))
    objects, bindings = open_files(config["directory"])
    try:
        env = runtime(client, config["table"], objects=objects, legacy_bindings=bindings,
                      existing_token=config["token"])
        env.now[0] = config["now"]
        calls = []
        calculate = env.adapter.calculate
        def counted(*args):
            calls.append(1)
            return calculate(*args)
        env.adapter.calculate = counted
        for job_id in config["jobs"]:
            assert env.worker.process(job_id) is True
        hashes, states = [], []
        for attempt_id in config["attempts"]:
            status, body = env.app.calculation.result(env.auth, attempt_id)
            assert status == 200 and type(body) is bytes
            hashes.append(hashlib.sha256(body).hexdigest())
            states.append(env.app.state.get_attempt(env.auth, attempt_id)["state"])
        view = env.app.course_service.get_course(env.auth, course_id=101, enrollment_id=501)
        item = next(row for row in course_detail_data(view)["courseItems"] if row["courseItemLinkId"] == 1003)
        print(json.dumps({"pid": os.getpid(), "calls": len(calls), "states": states,
            "result_hashes": hashes, "bundle_hash": view.bundle.definition_hash,
            "course_completed": item["isCompleted"]}))
    finally:
        objects.close()
        client.close()


if __name__ == "__main__":
    main()

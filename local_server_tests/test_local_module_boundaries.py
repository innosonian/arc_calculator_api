"""Import boundaries and re-exported names of the local server modules.

The CLI must not load application code or an SDK before it isolates the
environment; charts no longer reaches back into the HTTP adapter; the shared
primitives are used under their public names (no former-name aliases).
"""

import json
from pathlib import Path
import subprocess
import sys

from local_server import addresses, charts, cli, constants, database, object_storage, private_fs, runtime
from local_server import http as local_http


ROOT = Path(__file__).resolve().parents[1]


def loaded_after(module):
    code = ("import json, sys; import " + module + "; "
            "print(json.dumps(sorted(name for name in sys.modules if name.split('.')[0] in "
            "('local_server', 'mock_journey', 'services', 'boto3', 'botocore', 'waitress'))))")
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, timeout=60, check=True)
    return set(json.loads(result.stdout))


def test_cli_import_loads_no_application_or_sdk_module():
    assert loaded_after("local_server.cli") == {
        "local_server", "local_server.addresses", "local_server.cli", "local_server.constants",
        "local_server.private_fs"}


def test_shared_local_modules_import_only_the_standard_library():
    for module in ("local_server.constants", "local_server.addresses", "local_server.private_fs"):
        assert loaded_after(module) <= {"local_server", "local_server.constants", "local_server.addresses",
                                        "local_server.private_fs"}


def test_charts_and_runtime_do_not_import_the_http_adapter():
    assert "local_server.http" not in loaded_after("local_server.charts")
    assert "local_server.http" not in loaded_after("local_server.runtime")
    assert "local_server.cli" not in loaded_after("local_server.runtime")


def test_shared_primitives_are_used_under_their_public_names():
    assert local_http._address is addresses.local_address and local_http._port is addresses.local_port
    assert cli._private_ipv4 is addresses.private_ipv4
    assert local_http.BODY_LIMIT is constants.BODY_LIMIT and local_http.HEADER_LIMIT is constants.HEADER_LIMIT
    # One directory sync, one private-file creation skeleton and one
    # installation-record reader, each imported by its public name.
    assert database.sync_directory is private_fs.sync_directory is object_storage.sync_directory
    assert object_storage.create_private_file is private_fs.create_private_file
    assert database.read_installation_record.__module__ == "local_server.database"
    for module, alias in ((cli, "_PRIVATE"), (local_http, "_PRIVATE_NETWORKS"), (database, "_sync_directory"),
                          (database, "_read_record"), (object_storage, "_sync")):
        assert not hasattr(module, alias), alias
    assert charts.LocalChartService.path_prefix == "/local/v1/charts/"
    assert runtime.LocalOptions().response_body_limit == 8_000_000 + local_http.BODY_LIMIT


def test_private_ipv4_and_local_address_rules():
    assert [cli._private_ipv4(value) for value in (
        "10.0.0.1", "172.31.255.255", "192.168.1.1", "172.32.0.1", "127.0.0.1", "010.0.0.1", "8.8.8.8",
        "10.0.0.1 ", 167772161)] == [True, True, True, False, False, False, False, False, False]
    for value in ("127.0.0.1", "10.1.2.3", "192.168.0.10"):
        assert local_http._address(value) == value
    for value in ("127.0.0.2", "0.0.0.0", "8.8.8.8", "localhost", "010.1.2.3", None, 167772161):
        try:
            local_http._address(value)
        except ValueError as error:
            assert str(error) == "A literal local IPv4 address is required."
        else:
            raise AssertionError(value)

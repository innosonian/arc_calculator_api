"""Literal local defaults, timings, TTL and Waitress wiring (no sockets, DB or worker).

Values are written out here as independent expectations (DECISIONS D29 chart
TTL 300 seconds; the local 1,000,000/8,000,000/1 GiB/60-second defaults), so
naming or centralizing the constants cannot silently change them.
"""

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from local_server import cli
from local_server import http as local_http
from local_server import object_storage as objects
from local_server import runtime
from local_server.charts import LocalChartService
from local_server.database import prepare_material
from mock_journey.errors import JourneyError
from mock_journey.settings import StateSettings, StorageSettings


ROOT = Path(__file__).resolve().parents[1]
DEFAULTS = {"calculation_body_bytes": 1_000_000, "artifact_bytes": 8_000_000,
            "storage_quota_bytes": 1_073_741_824, "worker_lease_seconds": 60,
            "worker_retry_seconds": 5, "worker_poll_seconds": 0.25}


def test_cli_and_local_options_share_the_literal_defaults():
    args = cli.argument_parser().parse_args([])
    parsed = {name: getattr(args, name) for name in DEFAULTS}
    assert parsed == DEFAULTS
    assert [type(value) for value in parsed.values()] == [int, int, int, int, int, float]
    assert (args.host, args.port, args.db_port, args.allow_client, args.allow_insecure_lan) == (
        "127.0.0.1", 8000, 8001, [], False)
    assert args.data_dir == ROOT / "var/local-server"
    assert args.dynamodb_home == ROOT / "var/dynamodb-local-3.3.1"
    options = runtime.LocalOptions()
    assert {name: getattr(options, name) for name in DEFAULTS} == DEFAULTS
    assert runtime.LocalOptions(*parsed.values()) == options
    assert type(options.worker_poll_seconds) is float


def test_argparse_types_and_help_are_unchanged():
    parser = cli.argument_parser()
    types = {action.dest: action.type for action in parser._actions if action.dest in DEFAULTS}
    assert types == {"calculation_body_bytes": int, "artifact_bytes": int, "storage_quota_bytes": int,
                     "worker_lease_seconds": int, "worker_retry_seconds": int, "worker_poll_seconds": float}
    text = parser.format_help()
    assert "--calculation-body-bytes CALCULATION_BODY_BYTES" in text
    assert "--control-only" not in text and "--course-v2" not in text
    assert "1000000" not in text and "0.25" not in text


@pytest.mark.parametrize("decoded,encoded", [(1, 4), (2, 4), (3, 4), (4, 8), (1000, 1336),
                                             (1_000_000, 1_333_336), (2_097_152, 2_796_204)])
def test_local_payload_limit_is_four_times_ceiling_of_thirds(decoded, encoded):
    options = runtime.LocalOptions(calculation_body_bytes=decoded, artifact_bytes=max(decoded, 8))
    assert options.payload_limit == encoded and type(options.payload_limit) is int


def test_response_limit_adds_one_control_body():
    assert runtime.LocalOptions().response_body_limit == 8_000_000 + 16 * 1024
    assert local_http.BODY_LIMIT == 16384 and local_http.HEADER_LIMIT == 16384


def test_settings_keep_local_conflict_retry_and_storage_values():
    api, worker = runtime._settings(SimpleNamespace(table_name="arc_mock_local_v1"),
                                    SimpleNamespace(environment="local-" + "a" * 32), runtime.LocalOptions())
    state = StateSettings("arc_mock_local_v1", 4)
    storage = StorageSettings("local", "arc-local-private", "calculator_result/interpreted_rtdata/arc",
                              1_000_000, 8_000_000)
    assert (api.state, api.storage, api.environment, api.payload_limit) == (
        state, storage, "local-" + "a" * 32, 1_333_336)
    assert (worker.state, worker.storage, worker.lease_seconds, worker.retry_seconds) == (state, storage, 60, 5)
    assert runtime.LOCAL_COURSE_LIMITS["max_conflict_retries"] == 4


def test_ready_banner_text_is_exact():
    assert cli.READY_BANNER == (
        "Available: /api/v2 login, 15 temporary Dummy courses after Dummy login, measured binary calculation, "
        "stored results and 300-second charts.",
        "CPR completion follows the cycle rule (D136); ARC submission remains disabled.",
    )


@pytest.fixture
def installation(tmp_path):
    root = tmp_path.resolve() / "installation"
    root.mkdir(mode=0o700)
    local = prepare_material(root)
    return local, objects.prepare_object_material(local)


@pytest.mark.parametrize("lease,interval,timeout", [(60, 15.0, 15), (100, 25.0, 15), (40, 10.0, 10.0)])
def test_worker_lease_renewal_and_relay_page_values(installation, monkeypatch, lease, interval, timeout):
    import mock_journey.assembly
    import local_server.execution
    import local_server.lease

    local, object_material = installation
    captured = {}

    def guard(**kwargs):
        captured["guard"] = kwargs
        return "guard"

    def build_worker(settings, **kwargs):
        captured["worker"] = (settings, kwargs)
        return SimpleNamespace(jobs="jobs")

    def runner(jobs, worker, **kwargs):
        captured["runner"] = (jobs, kwargs)
        return "runner"
    monkeypatch.setattr(local_server.lease, "LocalLeaseGuardFactory", guard)
    monkeypatch.setattr(mock_journey.assembly, "build_worker", build_worker)
    monkeypatch.setattr(local_server.execution, "LocalJobRunner", runner)
    options = runtime.LocalOptions(worker_lease_seconds=lease)
    database = SimpleNamespace(table_name="arc_mock_local_v1", client="client", operations="operations")
    result, owned = runtime.build_local_worker(database, local, options, "127.0.0.1", 8000,
                                               object_material=object_material)
    try:
        assert result == "runner"
        assert captured["guard"] == {"lease_seconds": lease, "interval_seconds": interval,
                                     "renewal_timeout_seconds": timeout}
        assert [type(value) for value in captured["guard"].values()] == [int, float, type(timeout)]
        assert captured["runner"] == ("jobs", {"lease_seconds": lease, "retry_seconds": 5,
                                               "page_size": 20, "max_pages": 5})
        settings, kwargs = captured["worker"]
        assert settings.state == StateSettings("arc_mock_local_v1", 4)
        assert kwargs["lease_guard_factory"] == "guard" and kwargs["operations"] == "operations"
    finally:
        owned.close()


@pytest.fixture
def charts(installation):
    _, material = installation
    client = objects.LocalObjectClient(material, bucket="local-objects",
                                       directory="calculator_result/interpreted_rtdata/arc", stage="local-test",
                                       artifact_limit=200_000, quota_bytes=2_000_000)
    now = [1_700_000_000]
    service = LocalChartService(client, base_url="http://127.0.0.1:8000", clock=lambda: now[0])
    key = client.prefix + "_no_org/2023-11-14/CPR-ACTION-1700000000-12345678-1234-4234-8234-123456789abc.json"
    client.put_object(Bucket=client.bucket, Key=key, Body=b'{"values": [1]}')
    yield service, key, now
    client.close()


def test_chart_capability_ttl_is_exactly_300_seconds(charts):
    service, key, now = charts
    url = service.create_signed_url(key)
    assert re.fullmatch(r"http://127\.0\.0\.1:8000/local/v1/charts/v1\.1700000000\.1700000300\.[0-9a-f]{64}"
                        r"\.[0-9a-f]{64}\.[A-Za-z0-9_-]{43}", url)
    assert service.create_signed_url(key, expires_in=300) == url
    for bad in (299, 301, 300.0, True, None):
        with pytest.raises(JourneyError) as error:
            service.create_signed_url(key, expires_in=bad)
        assert error.value.code == "TEMPORARILY_UNAVAILABLE"
    path = url[len(service.base_url):]
    now[0] = 1_700_000_299
    assert service.read_path(path) == b'{"values": [1]}'
    now[0] = 1_700_000_300
    with pytest.raises(JourneyError) as error:
        service.read_path(path)
    assert error.value.code == "NOT_FOUND"
    # A correctly signed claim with any other lifetime is still refused.
    now[0] = 1_700_000_000
    claim = path[len(service.path_prefix):].rsplit(".", 1)[0].replace(".1700000300.", ".1700000299.", 1)
    with pytest.raises(JourneyError):
        service.read_path(service.path_prefix + claim + "." + service._signature(claim))


def test_legacy_binding_signs_with_the_300_second_default(installation):
    _, material = installation
    client = objects.LocalObjectClient(material, bucket="b", directory="d", stage="s",
                                       artifact_limit=10, quota_bytes=10)
    try:
        calls = []
        signer = SimpleNamespace(client=client, create_signed_url=lambda key, **kw: calls.append((key, kw)) or "url")
        binding = objects.LocalLegacyBindings(client, signer)
        assert binding.create_signed_url("k") == "url"
        assert calls == [("k", {"expires_in": 300})]
    finally:
        client.close()


def test_waitress_listener_options_are_exact(monkeypatch):
    import waitress.server

    captured = {}

    def without_socket(application, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace()
    monkeypatch.setattr(waitress.server, "create_server", without_socket)

    def application(environ, start_response):
        return []
    application._local_calculation_body_limit = 1_000_000
    application._local_response_body_limit = 8_016_384
    server = local_http.create_server(application, "127.0.0.1", 8000)
    assert captured == {
        "host": "127.0.0.1", "port": 8000, "ipv6": False, "threads": 4, "connection_limit": 32, "backlog": 32,
        "max_request_header_size": 16384, "max_request_body_size": 1_000_001, "inbuf_overflow": 1_000_001,
        "outbuf_overflow": 8_016_384 + 16384 + 1, "outbuf_high_watermark": 65536, "channel_timeout": 10,
        "cleanup_interval": 1, "channel_request_lookahead": 0, "expose_tracebacks": False, "ident": "",
        "trusted_proxy": None, "clear_untrusted_proxy_headers": True, "log_untrusted_proxy_headers": False,
    }
    assert server.channel_class.parser_class.__name__ == "StrictParser"
    captured.clear()

    def control(environ, start_response):
        return []
    local_http.create_server(control, "127.0.0.1", 8000)
    assert (captured["max_request_body_size"], captured["inbuf_overflow"], captured["outbuf_overflow"]) == (
        16385, 16385, 65536)


def test_waitress_version_pin_matches_requirements_and_is_checked_first(monkeypatch):
    pins = re.findall(r"^waitress==(\S+)$", (ROOT / "requirements-local.txt").read_text(), re.M)
    assert pins == [local_http.WAITRESS_VERSION] == ["3.0.2"]
    import waitress.server
    monkeypatch.setattr(local_http, "version", lambda name: "3.0.3")
    monkeypatch.setattr(waitress.server, "create_server", lambda *a, **k: pytest.fail("unpinned server"))

    def application(environ, start_response):
        return []
    with pytest.raises(RuntimeError) as error:
        local_http.create_server(application, "127.0.0.1", 8000)
    assert str(error.value) == "Install the pinned local HTTP server dependency."


def test_pinned_waitress_private_surface_used_by_the_adapter_exists():
    """The parser guard and shutdown use these private Waitress 3.0.2 names."""
    from waitress import wasyncore
    from waitress.channel import HTTPChannel
    import waitress.parser as parser
    from waitress.server import BaseWSGIServer
    from waitress.task import ThreadedTaskDispatcher
    from waitress.trigger import trigger

    assert callable(parser.get_header_lines) and hasattr(parser.HEADER_FIELD_RE, "match")
    assert {"name", "value"} <= set(parser.HEADER_FIELD_RE.groupindex)
    assert callable(parser.HTTPRequestParser.parse_header)
    assert issubclass(parser.TransferEncodingNotImplemented, Exception) and issubclass(parser.ParsingError, Exception)
    assert hasattr(HTTPChannel, "parser_class") and hasattr(HTTPChannel, "logger")
    assert callable(wasyncore.dispatcher.close) and callable(wasyncore.close_all)
    assert issubclass(BaseWSGIServer, wasyncore.dispatcher)
    assert callable(ThreadedTaskDispatcher.shutdown) and callable(trigger.pull_trigger)
    dispatcher = ThreadedTaskDispatcher()
    assert dispatcher.threads == set() or dispatcher.threads == {}

"""Strict operator-supplied AWS settings; no SDK, defaults, or resource discovery."""

from dataclasses import dataclass
import json
import math
import re
from typing import TYPE_CHECKING, NamedTuple
from urllib.parse import urlsplit

from mock_journey.aws_scope import (
    ACCOUNT_ID_PATTERN, ENVIRONMENT_PATTERN, PARTITIONS, REGION_PATTERN, TABLE_NAME_PATTERN,
    partition_matches_region,
)
from mock_journey.settings import (
    StateSettings, StorageSettings, ApiSettings, WorkerSettings, RelaySettings, base64_body_bytes,
    lease_renewal_exceeds, require_positive,
)
from mock_journey.typed import strict_loads

if TYPE_CHECKING:  # annotation only; no import-time dependency
    from mock_journey.course_settings import CourseSettings


def invalid():
    return ValueError("Invalid explicit AWS journey configuration.")


def _object(value, fields):
    if type(value) is not dict or set(value) != set(fields.split()):
        raise invalid()


def _positive(value, *, integer=False):
    return require_positive(value, invalid, number=not integer, finite=True)


def _text(value, pattern):
    if type(value) is not str or not re.fullmatch(pattern, value):
        raise invalid()
    return value


def strict_json(raw):
    try:
        if type(raw) is not str:
            raise invalid()
        return strict_loads(raw, duplicate=invalid, nonfinite=invalid)
    except Exception:
        raise invalid() from None


class WorkerTiming(NamedTuple):
    """Worker lease renewal and admission timing (a tuple: indexes 0..2 keep their meaning)."""
    renewal_interval_seconds: float
    renewal_timeout_seconds: float
    processing_reserve_ms: int


class RelayTiming(NamedTuple):
    """Relay admission timing (a one-item tuple)."""
    processing_reserve_ms: int


class ExecutionVersions(NamedTuple):
    """API/Worker adapter binding: (current, projection, retained) in this order."""
    current_adapter_version: str
    projection_version: str
    retained_adapter_versions: tuple


@dataclass(frozen=True)
class SdkSettings:
    connect_timeout: float
    read_timeout: float
    total_max_attempts: int
    retry_mode: str

    @classmethod
    def parse(cls, value):
        _object(value, "connect_timeout read_timeout total_max_attempts retry_mode")
        _positive(value["connect_timeout"])
        _positive(value["read_timeout"])
        _positive(value["total_max_attempts"], integer=True)
        if value["retry_mode"] not in ("standard", "legacy"):
            raise invalid()
        return cls(**value)

    def client_config(self, *, s3=False):
        from botocore.config import Config
        return Config(connect_timeout=self.connect_timeout, read_timeout=self.read_timeout,
                      retries={"total_max_attempts": self.total_max_attempts, "mode": self.retry_mode},
                      proxies={}, ignore_configured_endpoint_urls=True,
                      **({"signature_version": "s3v4"} if s3 else {}))

    def relay_call_budget_ms(self):
        """Conservative configured I/O/retry allowance, not a wall-clock bound.

        Standard retry jitter is bounded by min(2**retry_index, 20). Legacy
        DynamoDB and SQS use at most 2**retry_index. DNS, credentials, process
        pauses and server behaviour still require actual deadline checks.
        """
        attempts = self.total_max_attempts
        if self.retry_mode == "standard" and attempts > 6:
            backoff = 31 + 20 * (attempts - 6)
        else:
            backoff = math.ldexp(1.0, attempts - 1) - 1
        seconds = attempts * (self.connect_timeout + self.read_timeout) + backoff
        if not math.isfinite(seconds):
            raise invalid()
        return math.ceil(seconds * 1000)


@dataclass(frozen=True)
class LogSettings:
    capacity: int
    max_bytes: int
    flush_budget_ms: int
    response_reserve_ms: int
    sdk: SdkSettings

    @classmethod
    def parse(cls, value):
        _object(value, "capacity max_bytes flush_budget_ms response_reserve_ms sdk")
        for key in ("capacity", "max_bytes", "flush_budget_ms", "response_reserve_ms"):
            _positive(value[key], integer=True)
        if value["capacity"] > 4096:
            raise invalid()
        return cls(**{**value, "sdk": SdkSettings.parse(value["sdk"])})


@dataclass(frozen=True)
class RelayBudget:
    call_ms: int
    step_ms: int
    acquire_ms: int
    reserve_ms: int

    @classmethod
    def derive(cls, sdk, state, relay, logs, processing_reserve_ms):
        call_ms = sdk.relay_call_budget_ms()
        # OUTBOX worst path: claim, mark-sent and release each make at most
        # two SDK calls per conflict attempt. Add query, base read, SQS send,
        # pass-start, checkpoint and release. JOB needs fewer calls.
        step_ms = (6 * state.max_conflict_retries + 6) * call_ms
        acquire_ms = 2 * state.max_conflict_retries * call_ms
        reserve_ms = processing_reserve_ms + logs.flush_budget_ms + logs.response_reserve_ms
        # Integer epoch leases can lose almost one second at acquisition.
        if relay.lease_seconds * 1000 <= step_ms + reserve_ms + 1000:
            raise invalid()
        return cls(call_ms, step_ms, acquire_ms, reserve_ms)


@dataclass(frozen=True)
class AwsSettings:
    role: str
    account_id: str
    partition: str
    environment: str
    region: str
    state: StateSettings
    role_settings: object
    sdk: SdkSettings
    logs: LogSettings
    execution: tuple | None  # ExecutionVersions (API/Worker) or None (Relay)
    timing: tuple | None  # WorkerTiming, RelayTiming or None (API)
    # Required for API/Worker (the Dummy Dev course section); always None for Relay.
    course: "CourseSettings | None" = None

    @property
    def relay_budget(self):
        if self.role != "relay":
            raise invalid()
        return RelayBudget.derive(self.sdk, self.state, self.role_settings, self.logs,
                                  self.timing.processing_reserve_ms)

    @classmethod
    def parse(cls, raw, role):
        try:
            value = strict_json(raw)
            if role not in ("api", "worker", "relay"):
                raise invalid()
            # API and Worker serve only the course_v2 contract, so the explicit
            # Dummy course section is mandatory there. Relay never takes it.
            extra = "storage execution course " if role != "relay" else ""
            _object(value, "schema role account_id partition environment region state sdk logs " + extra + role)
            if type(value["schema"]) is not int or value["schema"] != 1 or value["role"] != role:
                raise invalid()
            account = _text(value["account_id"], ACCOUNT_ID_PATTERN)
            partition = value["partition"]
            if partition not in PARTITIONS:
                raise invalid()
            region = _text(value["region"], REGION_PATTERN)
            if not partition_matches_region(partition, region):
                raise invalid()
            environment = _text(value["environment"], ENVIRONMENT_PATTERN)
            _object(value["state"], "table_name max_conflict_retries")
            _text(value["state"]["table_name"], TABLE_NAME_PATTERN)
            state = StateSettings(**value["state"])
            sdk, logs = SdkSettings.parse(value["sdk"]), LogSettings.parse(value["logs"])
            execution = timing = course = None
            if role != "relay":
                _object(value["storage"], "stage bucket directory input_bytes artifact_bytes")
                _text(value["storage"]["bucket"], r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]")
                if ".." in value["storage"]["bucket"] or re.fullmatch(r"[0-9]+(?:\.[0-9]+){3}", value["storage"]["bucket"]):
                    raise invalid()
                storage = StorageSettings(**value["storage"])
                if storage.stage in ("test", "local"):
                    raise invalid()
                from mock_journey.contracts import CURRENT_ADAPTER_VERSION, RETAINED_ADAPTER_VERSIONS
                from mock_journey.execution_definitions import PROJECTION_VERSION
                supplied = value["execution"]
                _object(supplied, "current_adapter_version projection_version retained_adapter_versions")
                retained = supplied["retained_adapter_versions"]
                # D127: the operator's retained list must equal the code registry
                # exactly (same versions, same order). An empty or partial list
                # would leave stored candidates of a retained version unverifiable
                # and its in-flight attempts uncalculable (D138: v4 is retained);
                # the order is compared because the registry tuple is the order the
                # Worker registers adapters in and the bundle check compares the
                # roles' ExecutionVersions tuples position by position.
                if (supplied["current_adapter_version"] != CURRENT_ADAPTER_VERSION
                        or supplied["projection_version"] != PROJECTION_VERSION
                        or type(retained) is not list or any(type(v) is not str for v in retained)
                        or tuple(retained) != RETAINED_ADAPTER_VERSIONS):
                    raise invalid()
                execution = ExecutionVersions(supplied["current_adapter_version"], supplied["projection_version"],
                                              tuple(retained))
                from mock_journey.course_settings import CourseSettings
                from mock_journey.dev_course import CATALOG_VERSION, MODE, validate_dummy_catalog
                from mock_journey.execution_definitions import execution_catalog
                supplied_course = value["course"]
                _object(supplied_course, "mode catalog_version settings")
                if (supplied_course["mode"] != MODE or supplied_course["catalog_version"] != CATALOG_VERSION
                        or storage.stage not in ("dev", "development")):
                    raise invalid()
                _object(supplied_course["settings"], " ".join(CourseSettings.__dataclass_fields__))
                course = CourseSettings(**supplied_course["settings"])
                # Offline validation (shared with the local server) proves that
                # explicit limits can hold the complete synthetic catalog: the
                # transaction action lower bound (D105, a final assessment start
                # writes 8) and every serialized bundle snapshot within the
                # operator's artifact quota. No SDK or file I/O; any failure is
                # the same configuration error.
                validate_dummy_catalog(course, execution=execution_catalog(),
                                       artifact_bytes=storage.artifact_bytes)
            if role == "api":
                _object(value[role], "payload_limit")
                role_settings = ApiSettings(state, storage, environment, **value[role])
                # payload_limit bounds the encoded body; it must at least hold
                # the base64 form of the decoded CPR+AED input quota.
                if role_settings.payload_limit < base64_body_bytes(storage.input_bytes):
                    raise invalid()
            elif role == "worker":
                _object(value[role], "lease_seconds retry_seconds renewal_interval_seconds renewal_timeout_seconds processing_reserve_ms")
                options = value[role]
                for key in options:
                    _positive(options[key], integer=key in ("lease_seconds", "retry_seconds", "processing_reserve_ms"))
                interval, timeout = options["renewal_interval_seconds"], options["renewal_timeout_seconds"]
                if lease_renewal_exceeds(options["lease_seconds"], interval, timeout):
                    raise invalid()
                role_settings = WorkerSettings(state, storage, options["lease_seconds"], options["retry_seconds"])
                timing = WorkerTiming(interval, timeout, options["processing_reserve_ms"])
            else:
                _object(value[role], "queue_url lease_seconds retry_seconds page_size max_pages processing_reserve_ms")
                options = value[role]
                _positive(options["processing_reserve_ms"], integer=True)
                url = urlsplit(options["queue_url"])
                suffix = "amazonaws.com.cn" if partition == "aws-cn" else "amazonaws.com"
                expected_host = f"sqs.{region}.{suffix}"
                if (url.scheme != "https" or url.netloc != expected_host or url.query or url.fragment
                        or not re.fullmatch(r"/" + account + r"/[A-Za-z0-9_-]{1,80}", url.path)):
                    raise invalid()
                role_settings = RelaySettings(state, **{k: v for k, v in options.items() if k != "processing_reserve_ms"})
                timing = RelayTiming(options["processing_reserve_ms"])
                RelayBudget.derive(sdk, state, role_settings, logs, options["processing_reserve_ms"])
            return cls(role, account, partition, environment, region, state, role_settings, sdk, logs, execution, timing, course)
        except Exception:
            raise invalid() from None

    def check_environment(self, environ):
        try:
            if any(key.startswith("AWS_ENDPOINT_URL") and value for key, value in environ.items()):
                raise invalid()
            expected = {"AWS_REGION": self.region, "AWS_DEFAULT_REGION": self.region,
                        "ARC_MOCK_REGION": self.region, "ARC_MOCK_ENVIRONMENT": self.environment,
                        "ARC_MOCK_TABLE_NAME": self.state.table_name}
            if self.role != "relay":
                expected.update(STAGE=self.role_settings.storage.stage, ARC_STORAGE_REGION=self.region)
            if any(environ.get(key) and environ[key] != value for key, value in expected.items()):
                raise invalid()
        except Exception:
            raise invalid() from None

    def check_context(self, context):
        """Compare AWS-supplied ARN when present; this is not an IAM access check."""
        arn = getattr(context, "invoked_function_arn", None)
        if arn is not None:
            parts = arn.split(":") if type(arn) is str else []
            if (len(parts) < 7 or parts[:5] != ["arn", self.partition, "lambda", self.region, self.account_id]
                    or parts[5] != "function" or not parts[6]):
                raise invalid()


def main(argv=None):
    """Offline syntax/binding validation only; does not read secrets or call AWS."""
    import argparse
    from pathlib import Path
    parser = argparse.ArgumentParser(description="Validate one private role JSON without contacting AWS.")
    parser.add_argument("--role", required=True, choices=("api", "worker", "relay"))
    parser.add_argument("--config", required=True, help="Private role JSON file; contents are never printed")
    args = parser.parse_args(argv)
    try:
        AwsSettings.parse(Path(args.config).read_text(encoding="utf-8"), args.role)
    except Exception:
        print(json.dumps({"status": "configuration_invalid", "aws_access_checked": False}))
        return 2
    print(json.dumps({"status": "configuration_valid", "role": args.role,
                      "aws_access_checked": False, "secrets_checked": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

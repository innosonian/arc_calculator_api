"""Lazy role-specific AWS composition of the existing application components."""

from contextlib import contextmanager, nullcontext
import base64
import os
import threading

from mock_journey.aws_settings import AwsSettings, strict_json, invalid
from mock_journey.errors import JourneyError
from services.operational_logs import write_diagnostic


_runtimes = {}
_lock = threading.Lock()


def _keys(environ, environment):
    try:
        from mock_journey.auth import AuthManager
        supplied = strict_json(environ["ARC_MOCK_RESUME_KEYS"])
        version = environ["ARC_MOCK_RESUME_KEY_VERSION"]
        if type(supplied) is not dict or any(type(value) is not str for value in supplied.values()):
            raise invalid()
        values = {key: base64.b64decode(value, validate=True) for key, value in supplied.items()}
        # Key shape and current-version validation precede client construction.
        AuthManager(None, environment, values, version)
        return values, version
    except Exception:
        raise invalid() from None


def _close(client):
    try:
        client.close()
    except Exception:
        pass


class AwsRoleRuntime:
    def __init__(self, settings, target, clients, operations, guard=None):
        self.settings, self.target, self.clients = settings, target, tuple(clients)
        self.operations, self.guard = operations, guard
        # D141: one operational record per execution environment when the
        # document still carries an (ignored) `execution` block; no value of it.
        self.notice_pending = bool(getattr(settings, "execution_block_ignored", False))

    def bind_target(self):
        """Attach this runtime to its already assembled target, before any invocation.

        The target is built before its runtime, so this back-reference cannot be
        a constructor argument. ``invocation(target, context)`` reads
        ``target.aws_runtime``. ``target.invocation`` has no reader in this
        repository; it is kept unchanged pending the S9-05 question.
        """
        self.target.aws_runtime = self
        self.target.invocation = self.invocation
        return self

    @contextmanager
    def invocation(self, context):
        try:
            self.settings.check_context(context)
        except Exception:
            raise JourneyError("TEMPORARILY_UNAVAILABLE") from None
        lease = self.guard.invocation(context) if self.guard is not None else nullcontext()
        with lease, self.operations.invocation(context):
            if self.notice_pending:
                self.notice_pending = False
                write_diagnostic("info", "execution_block_ignored", {})
            yield self.target


def build_runtime(role, environ, *, client_factory=None):
    """No source-specific fake fallback. Inject SDK fakes only at this factory seam."""
    clients = []
    try:
        if environ.get("ARC_MOCK_ENABLED") != "true":
            raise invalid()
        settings = AwsSettings.parse(environ.get("ARC_JOURNEY_CONFIG"), role)
        settings.check_environment(environ)
        keys = _keys(environ, settings.environment) if role == "api" else None
        execution = course_provider = None
        if role != "relay":
            from mock_journey.execution_definitions import execution_catalog
            execution = execution_catalog()
        if role == "api":
            # AwsSettings.parse already required and validated the Dummy course
            # section for API/Worker; only the API serves course_v2 HTTP.
            from mock_journey.dev_course import DummyDevCourseProvider
            course_provider = DummyDevCourseProvider(settings=settings.course, execution=execution)
        from mock_journey.assembly import build_course_application, build_worker, build_relay
        from mock_journey.aws_storage import AwsLegacyBindings
        from mock_journey.aws_logs import InvocationLogs
        from mock_journey.log_storage import DynamoLogStore
        if client_factory is None:
            import boto3
            client_factory = boto3.client

        def client(service, sdk):
            return client_factory(service, region_name=settings.region, config=sdk.client_config(s3=service == "s3"))

        def log_store():
            # This closure is invoked only on the one bounded log writer.
            log_client = client("dynamodb", settings.logs.sdk)
            try:
                return DynamoLogStore(log_client, settings.state.table_name, settings.environment)
            except Exception:
                _close(log_client)
                raise

        operations = InvocationLogs(log_store, role=role, settings=settings.logs)
        dynamodb = client("dynamodb", settings.sdk)
        clients.append(dynamodb)
        guard = None
        if role == "relay":
            sqs = client("sqs", settings.sdk)
            clients.append(sqs)
            target = build_relay(settings.role_settings, dynamodb_client=dynamodb, sqs_client=sqs,
                                 progress_scope={"environment": settings.environment,
                                                 "partition": settings.partition,
                                                 "account_id": settings.account_id,
                                                 "region": settings.region},
                                 processing_reserve_ms=settings.timing.processing_reserve_ms,
                                 relay_budget=settings.relay_budget)
        else:
            s3 = client("s3", settings.sdk)
            clients.append(s3)
            legacy = AwsLegacyBindings(s3, settings.role_settings.storage)
            if role == "api":
                target = build_course_application(
                    settings.role_settings, dynamodb_client=dynamodb, s3_client=s3,
                    legacy_bindings=legacy, resume_keys=keys[0], current_key_version=keys[1],
                    execution=execution, operations=operations, provider=course_provider,
                    course_settings=settings.course, mapping_document=course_provider.mapping_document)
            else:
                from mock_journey.assembly import worker_adapters, worker_required_bindings
                from mock_journey.aws_lease import AwsLeaseGuardFactory
                current, projection, retained = settings.execution
                adapters = worker_adapters(current, retained, projection=projection,
                                           stage=settings.role_settings.storage.stage)
                guard = AwsLeaseGuardFactory(lease_seconds=settings.role_settings.lease_seconds,
                                            interval_seconds=settings.timing.renewal_interval_seconds,
                                            renewal_timeout_seconds=settings.timing.renewal_timeout_seconds,
                                            response_reserve_ms=settings.logs.response_reserve_ms)
                target = build_worker(settings.role_settings, dynamodb_client=dynamodb, s3_client=s3,
                                      legacy_bindings=legacy, adapters=adapters,
                                      required_bindings=worker_required_bindings(execution, retained,
                                                                                 projection=projection),
                                      lease_guard_factory=guard, operations=operations,
                                      processing_reserve_ms=settings.timing.processing_reserve_ms)
        return AwsRoleRuntime(settings, target, clients, operations, guard).bind_target()
    except Exception:
        for owned in reversed(clients):
            _close(owned)
        raise JourneyError("TEMPORARILY_UNAVAILABLE") from None


def get_runtime(role):
    with _lock:
        runtime = _runtimes.get(role)
        if runtime is None:
            runtime = build_runtime(role, os.environ)
            _runtimes[role] = runtime
        return runtime


def invocation(target, context):
    runtime = getattr(target, "aws_runtime", None)
    return runtime.invocation(context) if runtime is not None else nullcontext()

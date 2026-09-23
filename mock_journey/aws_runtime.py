"""Lazy role-specific AWS composition of the existing application components."""

from contextlib import contextmanager, nullcontext
import base64
import os
import threading

from mock_journey.aws_settings import AwsSettings, strict_json, invalid
from mock_journey.errors import JourneyError


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

    @contextmanager
    def invocation(self, context):
        try:
            self.settings.check_context(context)
        except Exception:
            raise JourneyError("TEMPORARILY_UNAVAILABLE") from None
        lease = self.guard.invocation(context) if self.guard is not None else nullcontext()
        with lease, self.operations.invocation(context):
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
            if settings.course is not None:
                from mock_journey.dev_course import DummyDevCourseProvider
                course_provider = DummyDevCourseProvider(settings=settings.course, execution=execution)
        from mock_journey.assembly import build_application, build_course_application, build_worker, build_relay
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
                                                 "region": settings.region})
            target.processing_reserve_ms = settings.timing[0]
            target.relay_budget = settings.relay_budget
        else:
            s3 = client("s3", settings.sdk)
            clients.append(s3)
            legacy = AwsLegacyBindings(s3, settings.role_settings.storage)
            if role == "api":
                builder = build_application if course_provider is None else build_course_application
                options = {} if course_provider is None else {
                    "provider": course_provider, "course_settings": settings.course,
                    "mapping_document": course_provider.mapping_document,
                }
                target = builder(settings.role_settings, dynamodb_client=dynamodb, s3_client=s3,
                                 legacy_bindings=legacy, resume_keys=keys[0], current_key_version=keys[1],
                                 execution=execution, operations=operations, **options)
            else:
                from mock_journey.internal_calculator import InternalCalculator
                from mock_journey.aws_lease import AwsLeaseGuardFactory
                current, projection, retained = settings.execution
                adapters = [InternalCalculator(version=version, projection_version=projection,
                                               stage=settings.role_settings.storage.stage, allow_pending_cycle_goal=True)
                            for version in (current, *retained)]
                guard = AwsLeaseGuardFactory(lease_seconds=settings.role_settings.lease_seconds,
                                            interval_seconds=settings.timing[0], renewal_timeout_seconds=settings.timing[1],
                                            response_reserve_ms=settings.logs.response_reserve_ms)
                target = build_worker(settings.role_settings, dynamodb_client=dynamodb, s3_client=s3,
                                      legacy_bindings=legacy, adapters=adapters,
                                      required_bindings=execution.required_bindings + tuple((v, projection) for v in retained),
                                      lease_guard_factory=guard, operations=operations)
                target.processing_reserve_ms = settings.timing[2]
        runtime = AwsRoleRuntime(settings, target, clients, operations, guard)
        target.aws_runtime = runtime
        # Relay has no handler-owned log scope, so expose the invocation wrapper.
        target.invocation = runtime.invocation
        return runtime
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

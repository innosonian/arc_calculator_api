"""Explicit role composition; never discovers clients, secrets or policy.

These factories assemble existing components. An ExecutionCatalog proves only
local shape/version consistency. Deployment runtime requires explicit storage,
execution definitions, and the program completion policy it intends to use.
"""

from mock_journey.bootstrap import configure_imports

configure_imports()

from types import MappingProxyType
import time

from mock_journey.auth import AuthManager
from mock_journey.calculation import CalculationService
from mock_journey.catalog import Catalog, PROGRAMS, TARGETS, definition_key, definition_keys
from mock_journey.contracts import CalculatorRegistry
from mock_journey.dispatch import OutboxRelay, QueueSender
from mock_journey.jobs import DynamoJobRepository
from mock_journey.projection import ProjectionSchema, SCALAR, definition_core_schema, project
from mock_journey.service import JourneyService
from mock_journey.settings import ApiSettings, WorkerSettings, RelaySettings
from mock_journey.state import DynamoStateRepository
from mock_journey.storage import JourneyStorage
from mock_journey import typed
from mock_journey.worker import JourneyWorker
from mock_journey.course_wiring import assemble_course
from mock_journey.course_storage import CourseBlobStore
from mock_journey.course_submission import CourseCompletionPlan


def _invalid():
    return ValueError("Invalid explicit journey composition.")


def _schema_tree(value):
    if type(value) is str and value == SCALAR:
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for key, nested in value.items():
            key.encode("utf-8")
            _schema_tree(nested)
        return
    if type(value) is list and len(value) == 1:
        _schema_tree(value[0])
        return
    raise _invalid()


_DEFINITION_SCHEMA = definition_core_schema()


class ExecutionCatalog:
    """Snapshot all 15 supplied definitions and explicit projection schemas.

    No definition, metric field, score, input mapping or old version is filled
    in automatically. Caller-owned dictionaries cannot change accepted input
    definitions by later mutation. Adapters are supplied only to worker wiring.
    """

    def __init__(self, definitions, schemas):
        try:
            expected = set(definition_keys())
            if type(definitions) is not dict or set(definitions) != expected or type(schemas) is not dict:
                raise _invalid()
            schema_bytes = {}
            for version, schema in schemas.items():
                if type(schema) is not ProjectionSchema or type(version) is not str or version != schema.version:
                    raise _invalid()
                typed.json_bytes(version)
                _schema_tree(schema.metric_fields)
                # Re-run the existing metric-name validation after detaching a
                # caller's mutable dataclass fields; preserve JSON leaf types.
                copied = typed.parse_json(typed.json_bytes(schema.metric_fields))
                ProjectionSchema(version, copied)
                schema_bytes[version] = typed.json_bytes(copied)
            definition_bytes = {}
            for key, value in definitions.items():
                # Reuse the accepted input shape, without constructing dummy
                # measurement bytes. Removal of a credential or any coercion
                # is a configuration error, not an invisible correction.
                projected = project(value, _DEFINITION_SCHEMA)
                if typed.canonical_bytes(projected) != typed.canonical_bytes(value):
                    raise _invalid()
                definition_bytes[key] = typed.json_bytes(value)
            self._definitions = MappingProxyType(definition_bytes)
            catalog = Catalog(self)
            bindings = set()
            for program, *_ in PROGRAMS:
                for target in TARGETS:
                    definition = typed.parse_json(catalog.definition(program, target))
                    if definition["projection_version"] not in schema_bytes:
                        raise _invalid()
                    bindings.add((definition["adapter_version"], definition["projection_version"]))
            self._schema_bytes = MappingProxyType(schema_bytes)
            self._required_bindings = tuple(sorted(bindings))
        except Exception:
            # Configuration may contain secret markers or private paths.
            # Never echo the supplied value or a dependency exception.
            raise _invalid() from None

    def get_definition(self, program_id, target):
        raw = self._definitions.get(definition_key(program_id, target))
        if raw is None:
            raise _invalid()
        return typed.parse_json(raw)

    @property
    def schemas(self):
        return {version: ProjectionSchema(version, typed.parse_json(raw))
                for version, raw in self._schema_bytes.items()}

    @property
    def required_bindings(self):
        return self._required_bindings


def _state(settings, client, clock):
    if client is None or not callable(clock):
        raise _invalid()
    return DynamoStateRepository(client, settings.table_name, clock=clock,
                                max_conflict_retries=settings.max_conflict_retries)


def _storage(settings, client, legacy_bindings):
    if client is None or legacy_bindings is None:
        raise _invalid()
    return JourneyStorage(client, legacy_bindings=legacy_bindings, stage=settings.stage,
                          limits={"input_bytes": settings.input_bytes, "artifact_bytes": settings.artifact_bytes},
                          namespace={"bucket": settings.bucket, "directory": settings.directory})


def _build_core(settings, *, dynamodb_client, s3_client, legacy_bindings, resume_keys,
                current_key_version, execution, clock, operations, blob_store=None):
    """Shared API parts (auth, state, calculation) under the course application.

    Private: the only public API role is build_course_application. No SQS
    client or calculator adapter is requested. The job repository receives
    the one course blob store (``blob_store`` or a store over this storage)
    that the course repository is then assembled with.
    """
    if type(settings) is not ApiSettings or type(execution) is not ExecutionCatalog:
        raise _invalid()
    if type(resume_keys) is not dict or type(current_key_version) is not str:
        raise _invalid()
    # Every failure below surfaces as the same sanitized _invalid(), so
    # building the state before the key-validating AuthManager is not observable.
    state = _state(settings.state, dynamodb_client, clock)
    auth = AuthManager(state, settings.environment, resume_keys, current_key_version, clock=clock)
    storage = _storage(settings.storage, s3_client, legacy_bindings)
    jobs = DynamoJobRepository(state, course_blobs=blob_store if blob_store is not None else CourseBlobStore(storage))
    calculation = CalculationService(state, jobs, storage, execution.schemas,
                                     payload_limit=settings.payload_limit, clock=clock)
    return JourneyService(state, auth, calculation, operations=operations)


def build_course_application(settings, *, dynamodb_client, s3_client, legacy_bindings, resume_keys,
                             current_key_version, execution, provider, course_settings,
                             clock=time.time, operations=None, uuid_factory=None,
                             mapping_document=None, dummy_learner=None, blob_store=None):
    """API role: the /api/v2 course application over the approved 15-definition catalog."""
    import uuid as uuid_module
    try:
        journey = _build_core(
            settings, dynamodb_client=dynamodb_client, s3_client=s3_client,
            legacy_bindings=legacy_bindings, resume_keys=resume_keys,
            current_key_version=current_key_version, execution=execution,
            clock=clock, operations=operations, blob_store=blob_store,
        )
        # The course repository shares the job repository's blob store object.
        return assemble_course(
            journey, provider=provider, course_settings=course_settings, clock=clock,
            uuid_factory=uuid_factory or uuid_module.uuid4, mapping_document=mapping_document,
            dummy_learner=dummy_learner, blob_store=journey.calculation.jobs.course_blobs,
        )
    except Exception:
        raise _invalid() from None


def internal_calculator(version, *, projection, stage):
    """The InternalCalculator of one registered adapter version (its fixed meaning).

    A pending adapter (v2 verify-only, v3 still calculating) keeps a cycles
    goal pending_policy and has no resolver; the cycle-goal adapter (v4) is
    bound to the D136 closed-cycle rule. An unregistered version is a
    composition error, never a guess.
    """
    from mock_journey.contracts import CYCLE_GOAL_ADAPTER_VERSION, PENDING_GOAL_ADAPTER_VERSIONS
    from mock_journey.internal_calculator import InternalCalculator
    if version in PENDING_GOAL_ADAPTER_VERSIONS:
        return InternalCalculator(version=version, projection_version=projection, stage=stage,
                                  allow_pending_cycle_goal=True)
    if version == CYCLE_GOAL_ADAPTER_VERSION:
        from mock_journey.cycle_goal import closed_cycle_count
        return InternalCalculator(version=version, projection_version=projection, stage=stage,
                                  cycle_goal_resolver=closed_cycle_count)
    raise _invalid()


def worker_adapters(current, retained, *, projection, stage):
    """Internal calculators for the current and each retained adapter version, in that order.

    One list for the AWS and the local worker composition. Retained versions
    are named by the caller; nothing is discovered from stored jobs.
    """
    return [internal_calculator(version, projection=projection, stage=stage)
            for version in (current, *retained)]


def worker_required_bindings(execution, retained, *, projection):
    """build_worker's required_bindings: the catalog's bindings, then each retained (version, projection)."""
    return execution.required_bindings + tuple((version, projection) for version in retained)


def build_worker(settings, *, dynamodb_client, s3_client, legacy_bindings, adapters,
                 required_bindings, clock=time.time, lease_guard_factory=None, operations=None,
                 completion_plan=None, processing_reserve_ms=None):
    """Worker role: explicitly retain every supplied current/old binding.

    Pass execution.required_bindings plus the verified retained-job bindings
    (worker_required_bindings). This function does not query a table to
    discover which versions to retain. It never receives the API's
    login/resume keys or creates an HTTP adapter. ``processing_reserve_ms``
    is the AWS invocation time admission read by ``worker.handle``; None
    (local) admits every record.
    """
    try:
        if type(settings) is not WorkerSettings or type(adapters) not in (list, tuple):
            raise _invalid()
        if type(required_bindings) not in (tuple, list) or not required_bindings:
            raise _invalid()
        for pair in required_bindings:
            if type(pair) is not tuple or len(pair) != 2 or any(type(v) is not str or not v for v in pair):
                raise _invalid()
            typed.json_bytes(list(pair))
        for adapter in adapters:
            if any(not callable(getattr(adapter, name, None)) for name in ("calculate", "validate_response", "get_chart")):
                raise _invalid()
        registry = CalculatorRegistry(adapters)
        for version, projection in required_bindings:
            registry.resolve(version, projection)
        state = _state(settings.state, dynamodb_client, clock)
        storage = _storage(settings.storage, s3_client, legacy_bindings)
        worker = JourneyWorker(DynamoJobRepository(state, course_blobs=CourseBlobStore(storage)), storage, registry,
                               lease_seconds=settings.lease_seconds, retry_seconds=settings.retry_seconds,
                               clock=clock, lease_guard_factory=lease_guard_factory, operations=operations,
                               completion_plan=completion_plan if completion_plan is not None else CourseCompletionPlan())
        if processing_reserve_ms is not None:
            # JourneyWorker takes no such constructor argument; the attribute
            # stays absent (getattr default None) for local compositions.
            worker.processing_reserve_ms = processing_reserve_ms
        return worker
    except Exception:
        raise _invalid() from None


def build_relay(settings, *, dynamodb_client, sqs_client, clock=time.time, progress_scope=None,
                processing_reserve_ms=None, relay_budget=None):
    """Relay role only: no user keys, S3 binding, profile or calculator.

    ``processing_reserve_ms``/``relay_budget`` are the AWS invocation time
    admission (AwsSettings); None (local) admits every step.
    """
    try:
        if type(settings) is not RelaySettings or sqs_client is None:
            raise _invalid()
        if progress_scope is not None:
            if (type(progress_scope) is not dict
                    or set(progress_scope) != {"environment", "partition", "account_id", "region"}):
                raise _invalid()
            from mock_journey.relay_progress import DynamoRelayProgress, RelayGuardedClient
            if dynamodb_client is None:
                raise _invalid()
            dynamodb_client = RelayGuardedClient(dynamodb_client)
            sqs_client = RelayGuardedClient(sqs_client)
        state = _state(settings.state, dynamodb_client, clock)
        progress = None
        if progress_scope is not None:
            progress = DynamoRelayProgress(state, queue_url=settings.queue_url, **progress_scope)
        return OutboxRelay(DynamoJobRepository(state), QueueSender(sqs_client, settings.queue_url),
                           lease_seconds=settings.lease_seconds, retry_seconds=settings.retry_seconds,
                           page_size=settings.page_size, max_pages=settings.max_pages, clock=clock,
                           progress=progress, processing_reserve_ms=processing_reserve_ms,
                           relay_budget=relay_budget)
    except Exception:
        raise _invalid() from None

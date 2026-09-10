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
from mock_journey.catalog import Catalog, PROGRAMS, TARGETS, slot_key
from mock_journey.contracts import CalculatorRegistry
from mock_journey.dispatch import OutboxRelay, QueueSender
from mock_journey.jobs import DynamoJobRepository
from mock_journey.projection import ProjectionSchema, SCALAR, _CONDITION, _DOCUMENT, _project
from mock_journey.service import JourneyService
from mock_journey.settings import ApiSettings, WorkerSettings, RelaySettings
from mock_journey.state import DynamoStateRepository
from mock_journey.storage import JourneyStorage
from mock_journey import typed
from mock_journey.worker import JourneyWorker


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


_DEFINITION_SCHEMA = {
    "condition": _CONDITION,
    "calculation_profile": {key: _DOCUMENT[key] for key in ("Custom", "Open_Skill", "Usage", "Organization")},
    **{key: SCALAR for key in ("profile_version", "adapter_version", "projection_version")},
}


class ExecutionCatalog:
    """Snapshot all 15 supplied definitions and explicit projection schemas.

    No definition, metric field, score, input mapping or old version is filled
    in automatically. Caller-owned dictionaries cannot change accepted input
    definitions by later mutation. Adapters are supplied only to worker wiring.
    """

    def __init__(self, definitions, schemas):
        try:
            expected = set(Catalog().slot_keys)
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
                projected = _project(value, _DEFINITION_SCHEMA)
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
        raw = self._definitions.get(slot_key(program_id, target))
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


def build_application(settings, *, dynamodb_client, s3_client, legacy_bindings, resume_keys,
                      current_key_version, execution, clock=time.time, operations=None):
    """API role only: no SQS client or calculator adapter is requested."""
    try:
        if type(settings) is not ApiSettings or type(execution) is not ExecutionCatalog:
            raise _invalid()
        if type(resume_keys) is not dict or type(current_key_version) is not str:
            raise _invalid()
        auth = AuthManager(None, settings.environment, resume_keys, current_key_version, clock=clock)
        state = _state(settings.state, dynamodb_client, clock)
        storage = _storage(settings.storage, s3_client, legacy_bindings)
        jobs = DynamoJobRepository(state)
        calculation = CalculationService(state, jobs, storage, execution.schemas,
                                         payload_limit=settings.payload_limit, clock=clock)
        auth.state = state
        return JourneyService(state, auth, Catalog(execution), calculation, operations=operations)
    except Exception:
        raise _invalid() from None


def build_worker(settings, *, dynamodb_client, s3_client, legacy_bindings, adapters,
                 required_bindings, clock=time.time, lease_guard_factory=None, operations=None):
    """Worker role: explicitly retain every supplied current/old binding.

    Pass execution.required_bindings plus the verified retained-job bindings.
    This function does not query a table to discover which versions to retain.
    It never receives the API's login/resume keys or creates an HTTP adapter.
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
        return JourneyWorker(DynamoJobRepository(state), storage, registry,
                             lease_seconds=settings.lease_seconds, retry_seconds=settings.retry_seconds,
                             clock=clock, lease_guard_factory=lease_guard_factory, operations=operations)
    except Exception:
        raise _invalid() from None


def build_relay(settings, *, dynamodb_client, sqs_client, clock=time.time):
    """Relay role only: no user keys, S3 binding, profile or calculator."""
    try:
        if type(settings) is not RelaySettings or sqs_client is None:
            raise _invalid()
        state = _state(settings.state, dynamodb_client, clock)
        return OutboxRelay(DynamoJobRepository(state), QueueSender(sqs_client, settings.queue_url),
                           lease_seconds=settings.lease_seconds, retry_seconds=settings.retry_seconds,
                           page_size=settings.page_size, max_pages=settings.max_pages, clock=clock)
    except Exception:
        raise _invalid() from None

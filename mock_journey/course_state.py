"""Epoch-scoped course control rows. Inject a CourseStore; do not import state.py helpers.

``DynamoCourseRepository`` is composed from the use-case mixins; its
constructor, the ``CourseStore``/``CourseBlobStore`` protocols, the guarded
reads and the commit live in ``course_repo_core``. Row keys are
``mock_journey.storage_keys`` and stored record shapes are
``mock_journey.course_records``; production code and the tests import both
directly.
"""

from mock_journey.course_records import (  # noqa: F401 (re-exported record helpers)
    assignment_from_record, assignment_record, bundle_from_record, bundle_record, report_request_digest,
    scope_identity_list, start_request_digest,
)
from mock_journey.course_repo_core import (  # noqa: F401 (re-exported protocols and constants)
    DYNAMODB_ITEM_MAX_BYTES, SCHEMA_VERSION, CourseBlobStore, CourseStore,
)
from mock_journey.course_repo_inventory import InventoryOps
from mock_journey.course_repo_refresh import RefreshOps
from mock_journey.course_repo_report import ReportOps
from mock_journey.course_repo_start import StartOps


class DynamoCourseRepository(InventoryOps, RefreshOps, StartOps, ReportOps):
    """T4 course repository. Clock and UUID factories are keyword-only injections.

    Use-case methods live in course_repo_inventory/refresh/start/report; the
    constructor, shared reads, guards and commit in
    course_repo_core.CourseRepositoryCore.
    """

"""Stored course record shapes and request digests, shared by course_state and jobs.

Moved verbatim from course_state.py (P4); course_state re-exports every name,
so ``course_state.bundle_record`` and friends are the same objects. The field
names and nesting are the stored bundle/assignment/receipt formats.
"""

from datetime import datetime, timezone
from functools import partial

from mock_journey.course_contracts import (
    AssignmentBinding, ContentReport, CourseBundle, CourseScope, LearnerContext, Placement, PublicIds,
    StartCommand, sealed_bundle,
)
from mock_journey.course_policy import learner_key, scope_key
from mock_journey.course_primitives import fail
from mock_journey.typed import digest, json_bytes, parse_json


_unavailable = partial(fail, "TEMPORARILY_UNAVAILABLE")


def _json_field(value):
    return json_bytes(value).decode("utf-8")


def _parse_field(value):
    if type(value) is bytes:
        return parse_json(value)
    if type(value) is str:
        return parse_json(value.encode("utf-8"))
    if type(value) in (dict, list):
        return parse_json(json_bytes(value))
    _unavailable()


def _rfc3339(seconds):
    return datetime.fromtimestamp(int(seconds), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def start_request_digest(command: StartCommand) -> str:
    return digest({
        "clientRequestId": command.request_id,
        "courseId": command.course_id,
        "enrollmentId": command.enrollment_id,
        "courseItemLinkId": command.placement_id,
        "definitionHash": command.definition_hash,
    })


def report_request_digest(course_id, enrollment_id, placement_id, report: ContentReport) -> str:
    event = {"type": report.event_type}
    if report.event_type == "video_segments":
        event["intervalsMs"] = [list(pair) for pair in report.intervals_ms]
    elif report.event_type == "document_confirmed":
        event["displayReportId"] = report.display_report_id
    return digest({
        "courseId": course_id,
        "enrollmentId": enrollment_id,
        "courseItemLinkId": placement_id,
        "startId": report.start_id,
        "reportId": report.report_id,
        "contentVersion": report.content_version,
        "event": event,
    })


def bundle_record(bundle: CourseBundle) -> dict:
    return {
        "scope": {
            "learner": {
                "provider": bundle.scope.learner.provider,
                "tenant_id": bundle.scope.learner.tenant_id,
                "learner_id": bundle.scope.learner.learner_id,
                "principal": bundle.scope.learner.principal,
                "is_dummy": bundle.scope.learner.is_dummy,
            },
            "enrollment_id": bundle.scope.enrollment_id,
            "course_id": bundle.scope.course_id,
        },
        "public_ids": {
            "course_id": bundle.public_ids.course_id,
            "enrollment_id": bundle.public_ids.enrollment_id,
            "progress_id": bundle.public_ids.progress_id,
        },
        "source_revision": bundle.source_revision,
        "mapping_version": bundle.mapping_version,
        "definition_hash": bundle.definition_hash,
        "placements": [_placement_record(item) for item in bundle.placements],
        "course_json": parse_json(bundle.course_json),
        "source_progress_json": parse_json(bundle.source_progress_json),
    }


def _placement_record(item: Placement) -> dict:
    return {
        "source_id": item.source_id,
        "public_link_id": item.public_link_id,
        "public_item_id": item.public_item_id,
        "position": item.position,
        "kind": item.kind,
        "content_version": item.content_version,
        "content_identity": parse_json(item.content_identity_json),
        "detail": parse_json(item.detail_json),
        "execution": None if item.execution_json is None else parse_json(item.execution_json),
        "execution_status": item.execution_status,
        "duration_ms": item.duration_ms,
    }


def bundle_from_record(record) -> CourseBundle:
    payload = record if type(record) is dict else _parse_field(record)
    learner = payload["scope"]["learner"]
    scope = CourseScope(
        LearnerContext(
            learner["provider"], learner["tenant_id"], learner["learner_id"],
            learner["principal"], learner["is_dummy"],
        ),
        payload["scope"]["enrollment_id"], payload["scope"]["course_id"],
    )
    public = payload["public_ids"]
    placements = []
    for item in payload["placements"]:
        execution = item["execution"]
        placements.append(Placement(
            source_id=item["source_id"],
            public_link_id=item["public_link_id"],
            public_item_id=item["public_item_id"],
            position=item["position"],
            kind=item["kind"],
            content_version=item["content_version"],
            content_identity_json=item["content_identity"],
            detail_json=item["detail"],
            execution_json=execution,
            execution_status=item["execution_status"],
            duration_ms=item["duration_ms"],
        ))
    return sealed_bundle(CourseBundle(
        scope=scope,
        public_ids=PublicIds(public["course_id"], public["enrollment_id"], public["progress_id"]),
        source_revision=payload["source_revision"],
        mapping_version=payload["mapping_version"],
        definition_hash=payload["definition_hash"],
        placements=tuple(placements),
        course_json=payload["course_json"],
        source_progress_json=payload["source_progress_json"],
    ))


def assignment_record(binding: AssignmentBinding) -> dict:
    learner = binding.scope.learner
    return {
        "provider": learner.provider,
        "tenant_id": learner.tenant_id,
        "learner_id": learner.learner_id,
        "principal": learner.principal,
        "is_dummy": learner.is_dummy,
        "enrollment_id": binding.scope.enrollment_id,
        "course_id": binding.scope.course_id,
        "public_course_id": binding.public_ids.course_id,
        "public_enrollment_id": binding.public_ids.enrollment_id,
        "progress_id": binding.public_ids.progress_id,
        "scope_identity": scope_identity_list(binding.scope),
        "scope_key": scope_key(binding.scope),
        "learner_key": learner_key(learner),
    }


def scope_identity_list(scope: CourseScope):
    return [
        scope.learner.provider, scope.learner.tenant_id, scope.learner.learner_id,
        scope.enrollment_id, scope.course_id,
    ]


def assignment_from_record(record) -> AssignmentBinding:
    stored_key = record.get("scope_key")
    learner = LearnerContext(
        record["provider"], record["tenant_id"], record["learner_id"],
        record["principal"], record["is_dummy"],
    )
    scope = CourseScope(learner, record["enrollment_id"], record["course_id"])
    computed = scope_key(scope)
    if stored_key is not None and stored_key != computed:
        _unavailable()
    if record.get("learner_key") not in (None, learner_key(learner)):
        _unavailable()
    return AssignmentBinding(
        scope,
        PublicIds(record["public_course_id"], record["public_enrollment_id"], record["progress_id"]),
    )

"""Content report use-case of DynamoCourseRepository.

Split out of course_state.py (S7-04/X3-04); method bodies are unchanged.
Each retry calls _report_once, which reads its own snapshot and commits
against it.
"""

from copy import deepcopy

from mock_journey.course_contracts import ContentReport, StoredProgressReceipt
from mock_journey.course_errors import CourseError
from mock_journey.course_policy import merge_intervals, placement_key
from mock_journey.course_records import _json_field, _parse_field, _unavailable, report_request_digest
from mock_journey.course_repo_core import DYNAMODB_ITEM_MAX_BYTES, SCHEMA_VERSION, CourseRepositoryCore, _put
from mock_journey.storage_keys import (
    course_head_key, course_item_key, course_report_key, course_start_key, start_locator_key,
)
from mock_journey.models import AuthContext
from mock_journey.typed import canonical_bytes, digest, json_bytes, parse_json


class ReportOps(CourseRepositoryCore):
    """report: idempotent REPORT receipt, START evidence and current-epoch progress."""

    def report(
        self, auth: AuthContext, *, course_id: int, enrollment_id: int, placement_id: int, report: ContentReport,
    ) -> StoredProgressReceipt:
        if type(report) is not ContentReport:
            raise CourseError("INVALID_REQUEST")
        for _ in range(self.settings.max_conflict_retries):
            result = self._report_once(auth, course_id, enrollment_id, placement_id, report)
            if result is not None:
                return result
        raise CourseError("TEMPORARILY_UNAVAILABLE")

    def _report_once(self, auth, course_id, enrollment_id, placement_id, report):
        session, user, now = self._session_user(auth)
        locator = self._get(start_locator_key(report.start_id))
        if locator is None or locator.get("principal") != auth.principal:
            raise CourseError("NOT_FOUND")
        if locator.get("bound_session_id") != auth.session_id:
            raise CourseError("NOT_FOUND")
        scope_key_value = locator["scope_key"]
        start_epoch = locator["epoch"]
        start = self._get(course_start_key(scope_key_value, start_epoch, report.start_id))
        if start is None:
            raise CourseError("NOT_FOUND")
        if start.get("bound_session_id") != auth.session_id:
            raise CourseError("NOT_FOUND")
        if start.get("course_id") != course_id or start.get("enrollment_id") != enrollment_id:
            raise CourseError("NOT_FOUND")
        if start.get("public_link_id") != placement_id:
            raise CourseError("NOT_FOUND")
        computed = placement_key(scope_key_value, start["source_id"])
        if start.get("placement_key") != computed:
            _unavailable()
        request_digest = report_request_digest(course_id, enrollment_id, placement_id, report)
        existing = self._get(course_report_key(scope_key_value, start_epoch, report.start_id, report.report_id))
        if existing is not None:
            if existing.get("request_digest") != request_digest:
                raise CourseError("IDEMPOTENCY_CONFLICT")
            return StoredProgressReceipt(
                scope_key_value, report.start_id, report.report_id,
                _parse_field(existing["response_json"]),
            )
        historical = start_epoch != user["epoch"]
        head = None
        bundle = None
        evidence = _start_evidence(start, historical=historical, current_version=None)
        if not historical:
            head = self._get(course_head_key(scope_key_value, user["epoch"]))
            bundle = self._load_bundle(head) if head else None
            if bundle is not None:
                current = None
                for item in bundle.placements:
                    if (item.public_link_id == placement_id and type(item.source_id) is type(start["source_id"])
                            and item.source_id == start["source_id"]):
                        current = item.content_version
                        saved_identity = start.get("content_identity_hash")
                        evidence["current_content_identity_matches"] = (
                            saved_identity == digest(parse_json(item.content_identity_json))
                            if type(saved_identity) is str
                            else start.get("definition_hash") == bundle.definition_hash
                        )
                        break
                evidence["current_content_version"] = current
                evidence["current_content_missing"] = current is None
        receipt_body = parse_json(self.policy.evaluate_content(json_bytes(evidence), report))
        report_row = {
            **course_report_key(scope_key_value, start_epoch, report.start_id, report.report_id),
            "schema_version": SCHEMA_VERSION,
            "start_id": report.start_id,
            "report_id": report.report_id,
            "request_digest": request_digest,
            "event_type": report.event_type,
            "content_version": report.content_version,
            "response_json": _json_field(receipt_body),
            "historical_only": historical,
        }
        new_start = deepcopy(start)
        new_start["revision"] = start["revision"] + 1
        new_start["report_count"] = start.get("report_count", 0) + 1
        if report.event_type == "video_segments" and not historical:
            merged = merge_intervals(list(start.get("merged_intervals_ms") or []) + [list(pair) for pair in report.intervals_ms])
            new_start["merged_intervals_ms"] = merged
            if receipt_body.get("isCompleted") is True:
                new_start["completed"] = True
        if report.event_type == "document_displayed" and not historical:
            displayed = list(start.get("displayed") or [])
            displayed.append({"report_id": report.report_id, "content_version": report.content_version})
            new_start["displayed"] = displayed
            if receipt_body.get("isCompleted") is True:
                new_start["completed"] = True
        if report.event_type == "document_confirmed" and not historical:
            pending = list(start.get("pending_confirmations") or [])
            pending.append({
                "report_id": report.report_id,
                "display_report_id": report.display_report_id,
                "content_version": report.content_version,
            })
            new_start["pending_confirmations"] = pending
            if receipt_body.get("isCompleted") is True:
                new_start["completed"] = True
        # Typed JSON includes names, escaped text and per-value tags. This is a
        # conservative budget below DynamoDB's item limit, not an exact storage
        # size estimate. Reject before any REPORT/START/progress write.
        if len(canonical_bytes(new_start)) > DYNAMODB_ITEM_MAX_BYTES:
            raise CourseError("PROGRESS_CAPACITY_EXCEEDED")
        actions = self._session_user_guards(auth, user) + [
            _put(report_row, if_not_exists=True),
            _put(new_start, if_match={"revision": start["revision"]}),
        ]
        if historical:
            if self._commit(actions):
                return StoredProgressReceipt(scope_key_value, report.start_id, report.report_id, receipt_body)
            return None
        if head is None:
            _unavailable()
        if bundle is None:
            raise CourseError("ARC_PROGRESS_UNAVAILABLE")
        place_key = start["placement_key"]
        item_row = self._get(course_item_key(scope_key_value, user["epoch"], place_key))
        completed_now = receipt_body.get("isCompleted") is True
        if item_row is None:
            item_row = {
                **course_item_key(scope_key_value, user["epoch"], place_key),
                "schema_version": SCHEMA_VERSION,
                "scope_key": scope_key_value,
                "placement_key": place_key,
                "epoch": user["epoch"],
                "source_id": start["source_id"],
                "public_link_id": placement_id,
                "content_version": start["content_version"],
                "completed": completed_now,
                "passed": None,
                "revision": 0,
            }
            actions.append(_put(item_row, if_not_exists=True))
        else:
            new_item = deepcopy(item_row)
            new_item["revision"] = item_row["revision"] + 1
            if item_row.get("completed") is True:
                new_item["completed"] = True
            else:
                new_item["completed"] = completed_now
            actions.append(_put(new_item, if_match={"revision": item_row["revision"]}))
        progress = _parse_field(head["progress_json"])
        completed = list(progress.get("completed_placements") or [])
        if completed_now and place_key not in completed:
            if item_row.get("completed") is True or completed_now:
                completed.append(place_key)
        items = dict(progress.get("items") or {})
        items[place_key] = {
            "source_id": start["source_id"],
            "public_link_id": placement_id,
            "kind": start.get("content_kind"),
            "content_version": start["content_version"],
            "completed": True if (item_row.get("completed") is True or completed_now) else False,
            "passed": None,
        }
        progress["completed_placements"] = completed
        progress["items"] = items
        aggregated = parse_json(self.policy.aggregate_progress(bundle, json_bytes(progress)))
        new_head = deepcopy(head)
        new_head["revision"] = head["revision"] + 1
        new_head["completed_placements"] = aggregated["completed_placements"]
        new_head["course_complete"] = aggregated["course_complete"]
        new_head["progress_json"] = _json_field(aggregated)
        new_head["updated_at"] = now
        actions.append(_put(new_head, if_match={"revision": head["revision"], "epoch": user["epoch"]}))
        receipt_body = dict(receipt_body)
        if receipt_body.get("application") != "historical_only":
            receipt_body["courseStatus"] = aggregated["course_status"]
        report_row["response_json"] = _json_field(receipt_body)
        if self._commit(actions):
            return StoredProgressReceipt(scope_key_value, report.start_id, report.report_id, receipt_body)
        return None


def _start_evidence(start, *, historical, current_version):
    return {
        "start_id": start["start_id"],
        "public_link_id": start["public_link_id"],
        "kind": start.get("content_kind"),
        "content_version": start["content_version"],
        "current_content_version": current_version,
        "duration_ms": start.get("duration_ms"),
        "historical_only": historical,
        "completed": start.get("completed") is True,
        "course_status": "IN_PROGRESS",
        "merged_intervals_ms": list(start.get("merged_intervals_ms") or []),
        "displayed": list(start.get("displayed") or []),
        "pending_confirmations": list(start.get("pending_confirmations") or []),
        "report_count": start.get("report_count") or 0,
    }

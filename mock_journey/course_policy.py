"""Pure VCC course start, content, and aggregate policy. No SDK, files, sockets, or env."""

from mock_journey.course_contracts import (
    CONTRACT_VERSION, COURSE_STATUSES, EXCLUSION_REASON_ORDER, POLICY_VERSION, START_ROLES,
    ContentReport, CourseBinding, CourseBundle, CourseView, Placement, StartCommand,
    owned_json_bytes, parse_owned, require_hash, require_member, require_public_id,
    require_uuid, scope_identity, learner_identity, validate_availability,
)
from mock_journey.course_errors import CourseError
from mock_journey.course_settings import CourseSettings
from mock_journey.typed import digest, json_bytes, parse_json


PUBLIC_SYMBOLS = (
    "CoursePolicy", "learner_key", "scope_key", "placement_key", "merge_intervals",
    "empty_progress", "progress_receipt",
)


def learner_key(learner) -> str:
    return digest(learner_identity(learner))


def scope_key(scope) -> str:
    return digest(scope_identity(scope))


def placement_key(scope_key_value: str, source_placement_id) -> str:
    require_hash(scope_key_value)
    return digest([scope_key_value, source_placement_id])


def merge_intervals(intervals):
    """Union overlapping and adjacent [start,end) millisecond ranges. Repeats do not grow coverage."""
    ordered = sorted(
        ((int(start), int(end)) for start, end in intervals if type(start) is int and type(end) is int and start < end),
        key=lambda item: (item[0], item[1]),
    )
    merged = []
    for start, end in ordered:
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        elif end > merged[-1][1]:
            merged[-1][1] = end
    return merged


def video_fully_covered(merged, duration_ms):
    if type(duration_ms) is not int or duration_ms <= 0:
        return False
    return merged == [[0, duration_ms]]


def empty_progress():
    return {
        "contract_version": CONTRACT_VERSION,
        "policy_version": POLICY_VERSION,
        "course_status": "NOT_STARTED",
        "course_complete": False,
        "completed_placements": [],
        "passed_final": False,
        "items": {},
        "final": {
            "phase": "free",
            "active_attempt_id": None,
            "passed_attempt_id": None,
            "goal_status": None,
            "program_completed": None,
        },
    }


def progress_receipt(*, start_id, report_id, course_item_link_id, is_completed, is_passed, course_status, application):
    return {
        "startId": start_id,
        "reportId": report_id,
        "courseItemLinkId": course_item_link_id,
        "isCompleted": is_completed,
        "isPassed": is_passed,
        "courseStatus": course_status,
        "application": application,
    }


def _fail(code="INVALID_REQUEST"):
    raise CourseError(code)


def _progress(value):
    parsed = value if type(value) is dict else parse_owned(owned_json_bytes(value))
    if type(parsed) is not dict:
        _fail()
    return parsed


def _placement_for(bundle: CourseBundle, public_link_id: int) -> Placement | None:
    for item in bundle.placements:
        if item.public_link_id == public_link_id:
            return item
    return None


def _last_assessment(bundle: CourseBundle) -> Placement | None:
    if not bundle.placements:
        return None
    last = bundle.placements[-1]
    return last if last.kind == "assessment" else None


def _item_key(bundle: CourseBundle, item: Placement) -> str:
    return placement_key(scope_key(bundle.scope), item.source_id)


def _completed_keys(bundle: CourseBundle, progress):
    keys = set()
    listed = progress.get("completed_placements")
    if type(listed) is list:
        for value in listed:
            if type(value) is str and value:
                keys.add(value)
            elif type(value) is int:
                item = _placement_for(bundle, value)
                if item is not None:
                    keys.add(_item_key(bundle, item))
    items = progress.get("items")
    records = []
    if type(items) is dict:
        for key, record in items.items():
            if type(record) is dict:
                records.append((key, record))
            elif record is True:
                item = _placement_for(bundle, int(key)) if type(key) is str and key.isdigit() else None
                if item is not None:
                    keys.add(_item_key(bundle, item))
    elif type(items) is list:
        for record in items:
            if type(record) is dict:
                records.append((None, record))
    for key, record in records:
        completed = record.get("completed") is True
        if not completed:
            continue
        if type(key) is str and len(key) == 64:
            keys.add(key)
        link = record.get("public_link_id")
        if type(link) is not int:
            link = record.get("courseItemLinkId")
        item = _placement_for(bundle, link) if type(link) is int else None
        if item is not None and "source_id" in record and (
                type(record["source_id"]) is not type(item.source_id) or record["source_id"] != item.source_id):
            item = None
        if item is None and type(record.get("source_id")) is not type(None):
            for candidate in bundle.placements:
                if candidate.source_id == record.get("source_id"):
                    item = candidate
                    break
        if item is not None:
            keys.add(_item_key(bundle, item))
    return keys


def _item_record(progress, bundle: CourseBundle, item: Placement):
    items = progress.get("items")
    key = _item_key(bundle, item)
    if type(items) is dict:
        if _record_matches(items.get(key), item):
            return items[key]
        as_link = items.get(str(item.public_link_id))
        if _record_matches(as_link, item):
            return as_link
        as_int = items.get(item.public_link_id)
        if _record_matches(as_int, item):
            return as_int
    elif type(items) is list:
        for record in items:
            if type(record) is not dict:
                continue
            if _record_matches(record, item) and (record.get("public_link_id") == item.public_link_id or record.get("source_id") == item.source_id):
                return record
    return {}


def _record_matches(record, item):
    return (type(record) is dict and ("source_id" not in record or (
        type(record["source_id"]) is type(item.source_id) and record["source_id"] == item.source_id
    )))


def _final_block(progress):
    block = progress.get("final")
    return block if type(block) is dict else {}


def _evaluation(progress):
    for key in ("evaluation", "verified_evaluation"):
        value = progress.get(key)
        if type(value) is dict:
            return value
    block = _final_block(progress)
    value = block.get("evaluation")
    return value if type(value) is dict else None


def _ignore_lease_fields(progress):
    # T5: never infer FINAL.phase from these.
    for key in ("active_counted", "lease_seconds", "lease_expires_at", "app_wait_seconds"):
        progress.get(key)
        _final_block(progress).get(key)


def _program_pass_from_evaluation(evaluation):
    if type(evaluation) is not dict:
        return None
    if "goal" not in evaluation or "program_completed" not in evaluation:
        if evaluation.get("goal_status") == "pending_policy" or (type(evaluation.get("goal")) is dict and evaluation["goal"].get("status") == "pending_policy"):
            return "pending_policy"
        return None
    goal = evaluation.get("goal")
    if type(goal) is not dict:
        _fail("CALCULATOR_CONTRACT_MISMATCH")
    status = goal.get("status")
    completed = evaluation.get("program_completed")
    score = evaluation.get("score")
    if status == "pending_policy":
        if completed is True:
            _fail("CALCULATOR_CONTRACT_MISMATCH")
        return "pending_policy"
    if status == "evaluated":
        if type(completed) is not bool:
            _fail("CALCULATOR_CONTRACT_MISMATCH")
        required = goal.get("required")
        observed = goal.get("observed")
        if type(required) is int and type(observed) is int and observed < required and completed is True:
            _fail("CALCULATOR_CONTRACT_MISMATCH")
        if type(score) is dict and score.get("decision") == "pass" and type(required) is int and type(observed) is int:
            if observed < required and completed is True:
                _fail("CALCULATOR_CONTRACT_MISMATCH")
        return True if completed else False
    if status is None and type(completed) is bool:
        return completed
    _fail("CALCULATOR_CONTRACT_MISMATCH")


def _availability_error(state, reason):
    state, reason = validate_availability(state, reason)
    if state == "ready":
        return None
    if state == "reconciliation_required":
        return "PROGRESS_RECONCILIATION_REQUIRED"
    if reason == "contract_pending":
        return "CONTRACT_PENDING"
    return "ARC_PROGRESS_UNAVAILABLE"


def _execution_error(status):
    if status == "ready":
        return None
    if status == "absent":
        return "EXECUTION_DEFINITION_MISSING"
    if status == "unsupported":
        return "EXECUTION_DEFINITION_UNSUPPORTED"
    if status == "contract_pending":
        return "CONTRACT_PENDING"
    _fail()


def _in_assignments(view: CourseView, command: StartCommand):
    for binding in view.inventory.assignments:
        if (binding.public_ids.course_id == command.course_id
                and binding.public_ids.enrollment_id == command.enrollment_id
                and binding.public_ids == view.public_ids
                and scope_key(binding.scope) == view.scope_key
                and scope_key(view.bundle.scope) == view.scope_key):
            return True
    return False


class CoursePolicy:
    """vcc-policy-v1 start gates, content coverage, and course vs item aggregation."""

    def __init__(self, settings: CourseSettings):
        if type(settings) is not CourseSettings:
            raise TypeError("CourseSettings is required.")
        self.settings = settings

    def can_start(self, view: CourseView, command: StartCommand, role: str) -> None:
        if type(view) is not CourseView or type(command) is not StartCommand:
            _fail()
        require_member(role, START_ROLES)
        # D5 (4) assignment/gate after auth/idempotency (repository).
        inventory_error = _availability_error(view.inventory.state, view.inventory.reason)
        if inventory_error is not None:
            raise CourseError(inventory_error)
        if not _in_assignments(view, command):
            raise CourseError("NOT_FOUND")
        if (view.public_ids.course_id != command.course_id
                or view.public_ids.enrollment_id != command.enrollment_id):
            raise CourseError("NOT_FOUND")
        gate_error = _availability_error(view.gate.state, view.gate.reason)
        if gate_error is not None:
            raise CourseError(gate_error)
        # D5 (5) definition hash.
        expected_hash = view.gate.definition_hash or view.bundle.definition_hash
        if command.definition_hash != view.bundle.definition_hash or command.definition_hash != expected_hash:
            raise CourseError("DEFINITION_CHANGED")
        # D5 (6) item/kind/execution.
        placement = _placement_for(view.bundle, command.placement_id)
        if placement is None:
            raise CourseError("NOT_FOUND")
        last = _last_assessment(view.bundle)
        is_final = last is not None and placement.public_link_id == last.public_link_id
        if is_final:
            if role != "final_assessment" or placement.kind != "assessment":
                _fail()
        elif role != "training":
            _fail()
        if placement.kind in {"training", "assessment"}:
            execution_error = _execution_error(placement.execution_status)
            if execution_error is not None:
                raise CourseError(execution_error)
        elif placement.kind not in {"video", "document"}:
            _fail()
        # D5 (7) training completed / final passed / role / prerequisites.
        progress = _progress(view.progress_json)
        _ignore_lease_fields(progress)
        completed = _completed_keys(view.bundle, progress)
        item_done = _item_key(view.bundle, placement) in completed or _item_record(progress, view.bundle, placement).get("completed") is True
        if placement.kind == "training" and not is_final and item_done:
            raise CourseError("ITEM_ALREADY_COMPLETED")
        if is_final:
            for prior in view.bundle.placements[:-1]:
                if _item_key(view.bundle, prior) not in completed:
                    raise CourseError("PREREQUISITES_NOT_COMPLETED")
            phase = _final_block(progress).get("phase") or "free"
            if phase == "active":
                raise CourseError("FINAL_ASSESSMENT_ACTIVE")
            if phase == "recovery_required":
                raise CourseError("FINAL_ASSESSMENT_RECOVERY_REQUIRED")
            if phase == "policy_pending":
                raise CourseError("COMPLETION_POLICY_PENDING")
            if phase == "passed":
                raise CourseError("ASSESSMENT_ALREADY_PASSED")
            if phase != "free":
                _fail()
            # Only the atomic finalizer/recovery may release a persisted role.
            # A previous failed evaluation does not unlock a new active attempt.
            evaluation = _evaluation(progress)
            judged = _program_pass_from_evaluation(evaluation) if evaluation is not None else None
            if judged == "pending_policy":
                raise CourseError("COMPLETION_POLICY_PENDING")
            if judged is True:
                raise CourseError("ASSESSMENT_ALREADY_PASSED")
        submit = progress.get("submit_arc")
        if type(submit) is dict:
            # D11/V11: disabled/excluded is not a start gate.
            submit.get("status")

    def evaluate_content(self, start_evidence_json: bytes, report: ContentReport) -> bytes:
        evidence = _progress(start_evidence_json)
        if type(report) is not ContentReport:
            _fail()
        start_id = require_uuid(evidence.get("start_id") or report.start_id)
        if start_id != report.start_id:
            _fail()
        raw_link = evidence.get("public_link_id")
        if raw_link is None:
            raw_link = evidence.get("courseItemLinkId")
        link_id = require_public_id(raw_link)
        kind = evidence.get("kind")
        start_version = evidence.get("content_version")
        if type(start_version) is not str or not start_version:
            _fail()
        course_status = evidence.get("course_status") if evidence.get("course_status") in COURSE_STATUSES else "IN_PROGRESS"
        already = evidence.get("completed") is True
        if report.content_version != start_version:
            raise CourseError("CONTENT_VERSION_MISMATCH")
        current_version = evidence.get("current_content_version")
        version_unmapped = (evidence.get("current_content_missing") is True
                            or evidence.get("current_content_identity_matches") is False
                            or type(current_version) is str and current_version != start_version)
        report_count = evidence.get("report_count")
        if type(report_count) is int and report_count >= self.settings.max_reports_per_start:
            raise CourseError("PROGRESS_CAPACITY_EXCEEDED")
        if report.event_type == "video_segments":
            if kind not in (None, "video"):
                _fail()
            receipt = self._evaluate_video(
                evidence, report, link_id, course_status, already, version_unmapped,
            )
        else:
            if kind not in (None, "document"):
                _fail()
            receipt = self._evaluate_document(
                evidence, report, link_id, course_status, already, version_unmapped,
            )
        if evidence.get("historical_only") is True:
            receipt = progress_receipt(
                start_id=report.start_id, report_id=report.report_id, course_item_link_id=link_id,
                is_completed=None, is_passed=None, course_status=None, application="historical_only",
            )
        return json_bytes(receipt)

    def _evaluate_video(self, evidence, report, link_id, course_status, already, version_unmapped):
        duration_ms = evidence.get("duration_ms")
        if type(duration_ms) is not int or duration_ms <= 0:
            _fail()
        if len(report.intervals_ms) > self.settings.max_intervals_per_report:
            raise CourseError("PAYLOAD_TOO_LARGE")
        for start, end in report.intervals_ms:
            if start < 0 or end > duration_ms or start >= end:
                _fail()
        previous = evidence.get("merged_intervals_ms") or []
        if type(previous) not in (list, tuple):
            _fail()
        merged = merge_intervals(list(previous) + [list(pair) for pair in report.intervals_ms])
        if len(merged) > self.settings.max_merged_intervals_per_start:
            raise CourseError("PROGRESS_CAPACITY_EXCEEDED")
        covered = video_fully_covered(merged, duration_ms)
        if version_unmapped:
            return progress_receipt(
                start_id=report.start_id, report_id=report.report_id, course_item_link_id=link_id,
                is_completed=True if already else False, is_passed=None, course_status=course_status,
                application="pending_reconciliation",
            )
        completed = True if already else covered
        return progress_receipt(
            start_id=report.start_id, report_id=report.report_id, course_item_link_id=link_id,
            is_completed=completed, is_passed=None, course_status=course_status,
            application="applied",
        )

    def _evaluate_document(self, evidence, report, link_id, course_status, already, version_unmapped):
        displayed = _displayed_ids(evidence, evidence.get("content_version"))
        pending = evidence.get("pending_confirmations") or []
        if type(pending) not in (list, tuple):
            _fail()
        if version_unmapped:
            return progress_receipt(
                start_id=report.start_id, report_id=report.report_id, course_item_link_id=link_id,
                is_completed=True if already else False, is_passed=None, course_status=course_status,
                application="pending_reconciliation",
            )
        if report.event_type == "document_displayed":
            confirmed = False
            for item in pending:
                if type(item) is dict and item.get("display_report_id") == report.report_id:
                    if item.get("content_version") in (None, evidence.get("content_version")):
                        confirmed = True
            completed = True if already or confirmed else False
            return progress_receipt(
                start_id=report.start_id, report_id=report.report_id, course_item_link_id=link_id,
                is_completed=completed, is_passed=None, course_status=course_status,
                application="applied",
            )
        display_id = report.display_report_id
        if display_id in displayed:
            return progress_receipt(
                start_id=report.start_id, report_id=report.report_id, course_item_link_id=link_id,
                is_completed=True, is_passed=None, course_status=course_status,
                application="applied",
            )
        return progress_receipt(
            start_id=report.start_id, report_id=report.report_id, course_item_link_id=link_id,
            is_completed=True if already else False, is_passed=None, course_status=course_status,
            application="pending_evidence",
        )

    @staticmethod
    def aggregate_progress(bundle: CourseBundle, progress_json: bytes) -> bytes:
        if type(bundle) is not CourseBundle:
            _fail()
        progress = _progress(progress_json)
        assessment_reconciliation = progress.get("assessment_reconciliation_required") is True
        completed = _completed_keys(bundle, progress)
        items = {}
        all_complete = True
        any_complete = False
        last = _last_assessment(bundle)
        last_key = _item_key(bundle, last) if last is not None else None
        last_passed = False
        for item in bundle.placements:
            key = _item_key(bundle, item)
            record = _item_record(progress, bundle, item)
            done = key in completed
            passed = record.get("passed") if "passed" in record else None
            if item == last and record.get("content_version") != item.content_version:
                # An enrollment-level pass still prevents retaking, but cannot
                # establish completion of a replacement assessment definition.
                done = False
                passed = None
            if item.kind in {"video", "document"}:
                passed = None
            elif done and item.kind == "assessment" and last_key == key:
                if passed is True:
                    last_passed = True
            items[key] = {
                "source_id": item.source_id,
                "public_link_id": item.public_link_id,
                "kind": item.kind,
                "content_version": record.get("content_version") or item.content_version,
                "completed": done,
                "passed": passed if item.kind in {"training", "assessment"} else None,
            }
            if done:
                any_complete = True
            else:
                all_complete = False
        final = dict(_final_block(progress))
        enrollment_passed = final.get("phase") == "passed"
        bound_final_matches = (
            last is not None
            and final.get("passed_placement_key") == last_key
            and final.get("passed_definition_hash") == bundle.definition_hash
            and final.get("passed_content_version") == last.content_version
        )
        evaluation = _evaluation(progress)
        judged = _program_pass_from_evaluation(evaluation) if evaluation is not None else None
        if enrollment_passed:
            # Completion evidence cannot retrospectively release D78's lock.
            final["program_completed"] = True
            last_passed = last_passed or bound_final_matches
        elif judged == "pending_policy":
            final["phase"] = "policy_pending"
            final["goal_status"] = "pending_policy"
            final["program_completed"] = False
            last_passed = False
        elif judged is True:
            final["phase"] = "passed"
            final["goal_status"] = "evaluated"
            final["program_completed"] = True
            last_passed = last_passed or bound_final_matches
        elif judged is False:
            final["program_completed"] = False
            final["goal_status"] = final.get("goal_status") or "evaluated"
            if final.get("phase") == "passed":
                final["phase"] = "free"
            last_passed = False
        else:
            if final.get("phase") == "passed" or progress.get("passed_final") is True:
                final["phase"] = "passed"
                final["program_completed"] = True
                last_passed = last_passed or bound_final_matches
        if last_key is not None:
            if items[last_key]["passed"] is not True and last_passed:
                items[last_key]["passed"] = True
            if items[last_key]["passed"] is True:
                last_passed = True
            # Do not copy last-assessment pass onto earlier items.
            for item in bundle.placements[:-1]:
                key = _item_key(bundle, item)
                if item.kind != "assessment" and items[key]["passed"] is True and item.kind in {"video", "document"}:
                    items[key]["passed"] = None
        if assessment_reconciliation and last_key is not None:
            items[last_key]["completed"] = False
            items[last_key]["passed"] = None
            last_passed = False
            all_complete = False
            any_complete = any(item["completed"] for item in items.values())
        finished = all_complete and last_passed
        if finished:
            status = "FINISHED"
        elif any_complete or (final.get("phase") not in (None, "free") and final.get("phase") != "passed"):
            status = "IN_PROGRESS"
        elif any_complete:
            status = "IN_PROGRESS"
        else:
            status = "NOT_STARTED"
        if any_complete and not finished:
            status = "IN_PROGRESS"
        if finished:
            status = "FINISHED"
        # CERTIFIED is never invented.
        if status == "CERTIFIED":
            status = "FINISHED"
        completed_list = [_item_key(bundle, item) for item in bundle.placements if items[_item_key(bundle, item)]["completed"]]
        payload = {
            "contract_version": CONTRACT_VERSION,
            "policy_version": POLICY_VERSION,
            "course_status": status,
            "course_complete": finished,
            "completed_placements": completed_list,
            "passed_final": last_passed,
            "assessment_reconciliation_required": assessment_reconciliation,
            "items": items,
            "final": {
                "phase": final.get("phase") or "free",
                "active_attempt_id": final.get("active_attempt_id"),
                "passed_attempt_id": final.get("passed_attempt_id"),
                "goal_status": final.get("goal_status"),
                "program_completed": True if last_passed and judged is not False else final.get("program_completed"),
                **{key: final[key] for key in (
                    "passed_placement_key", "passed_definition_hash", "passed_content_version",
                ) if key in final},
            },
        }
        return json_bytes(payload)

    def classify_submission(
        self, binding: CourseBinding, verified_result_json: bytes, *, is_dummy: bool, current_epoch: str,
    ) -> bytes:
        if type(binding) is not CourseBinding:
            _fail()
        if type(is_dummy) is not bool:
            _fail()
        if type(current_epoch) is not str or not current_epoch:
            _fail()
        result = parse_owned(owned_json_bytes(verified_result_json, allow_none=False))
        if type(result) is not dict:
            _fail("CALCULATOR_CONTRACT_MISMATCH")
        reasons = []
        if is_dummy:
            reasons.append("dummy")
        guideline = _guideline_of(result)
        if guideline is not None and not str(guideline).startswith("ARC"):
            reasons.append("non_arc_guideline")
        if binding.epoch != current_epoch:
            reasons.append("progress_reset_before_result")
        ordered = [reason for reason in EXCLUSION_REASON_ORDER if reason in reasons]
        if ordered:
            payload = {
                "status": "excluded",
                "ok": False,
                "error": None,
                "exclusionReasons": ordered,
            }
        else:
            payload = {
                "status": "disabled",
                "ok": False,
                "error": "arc_contract_pending",
                "exclusionReasons": [],
            }
        return json_bytes(payload)


def _displayed_ids(evidence, version):
    ids = set()
    displayed = evidence.get("displayed") or evidence.get("displayed_report_ids") or []
    if type(displayed) not in (list, tuple):
        _fail()
    for item in displayed:
        if type(item) is str:
            ids.add(item)
            continue
        if type(item) is dict:
            report_id = item.get("report_id") or item.get("reportId")
            item_version = item.get("content_version") or item.get("contentVersion")
            if type(report_id) is str and item_version in (None, version):
                ids.add(report_id)
    return ids


def _guideline_of(result):
    if type(result.get("condition")) is dict and "guideline" in result["condition"]:
        return result["condition"]["guideline"]
    calculation = result.get("calculation")
    if type(calculation) is dict and type(calculation.get("condition")) is dict:
        return calculation["condition"].get("guideline")
    evaluation = result.get("evaluation")
    if type(evaluation) is dict and type(evaluation.get("condition")) is dict:
        return evaluation["condition"].get("guideline")
    binding = result.get("binding")
    if type(binding) is dict and type(binding.get("condition")) is dict:
        return binding["condition"].get("guideline")
    return result.get("guideline")

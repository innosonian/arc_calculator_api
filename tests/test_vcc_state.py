"""V06/V07/V08/V10 course repository tests against an explicit fake store. No sleep."""

import pytest

from mock_journey.course_contracts import (
    POLICY_VERSION, AttemptTemplate, ContentReport, CourseBinding, StartCommand,
)
from mock_journey.course_errors import CourseError
from mock_journey.course_policy import learner_key, placement_key, scope_key
from mock_journey.storage_keys import (
    course_final_key, course_head_key, course_item_key, session_key, user_key,
)
from mock_journey.models import AuthContext
from mock_journey.typed import json_bytes, parse_json
from tests.vcc_hook_requests import COURSE_STATE_HOOK_REQUESTS as HOOK_REQUESTS
from tests.vcc_state_support import (  # noqa: F401 (re-export)
    ATTEMPT, BUNDLE_DOC, EPOCH, REPORT_A, REPORT_B, REQUEST, REQUEST_B, ROOT, SESSION, START, SequencedUuid,
    assignment_doc, binding_of, learner_from, make_bundle, placement_from, provision, repository, seed_auth,
)


class TestV06WaitingVersusReady:
    def test_failed_inventory_is_waiting_empty_success_is_ready(self):
        repo, store, blobs = repository()
        auth = seed_auth(store)
        learner = learner_from()
        ticket = repo.begin_inventory(auth, learner)
        waiting = repo.apply_inventory(auth, ticket, CourseError("ARC_PROGRESS_UNAVAILABLE"))
        assert waiting.state == "waiting"
        assert waiting.reason == "arc_progress_unavailable"
        assert waiting.assignments == ()
        ready_ticket = repo.begin_inventory(auth, learner)
        ready = repo.apply_inventory(auth, ready_ticket, ())
        assert ready.state == "ready"
        assert ready.reason is None
        assert ready.assignments == ()

    def test_stale_inventory_success_and_failure_are_noop(self):
        repo, store, blobs = repository()
        auth = seed_auth(store)
        learner = learner_from()
        first = repo.begin_inventory(auth, learner)
        second = repo.begin_inventory(auth, learner)
        current = repo.apply_inventory(auth, second, CourseError("ARC_PROGRESS_UNAVAILABLE"))
        stale_success = repo.apply_inventory(auth, first, ())
        assert stale_success.state == "waiting"
        assert stale_success.generation == current.generation
        stale_fail = repo.apply_inventory(auth, first, CourseError("ARC_PROGRESS_UNAVAILABLE"))
        assert stale_fail.state == "waiting"
        assert stale_fail.revision == current.revision

    def test_parent_generation_change_drops_stale_bundle(self):
        repo, store, blobs = repository()
        auth = seed_auth(store)
        bundle = make_bundle()
        ticket = repo.begin_inventory(auth, bundle.scope.learner)
        repo.apply_inventory(auth, ticket, (binding_of(bundle),))
        repo.ensure_epoch(auth, binding_of(bundle))
        refresh = repo.begin_refresh(auth, binding_of(bundle), ticket)
        newer = repo.begin_inventory(auth, bundle.scope.learner)
        repo.apply_inventory(auth, newer, (binding_of(bundle),))
        gate = repo.apply_refresh(auth, refresh, bundle)
        assert gate.state == "waiting"
        assert gate.definition_hash is None

    def test_waiting_rejects_new_start_but_keeps_existing_report_path(self):
        repo, store, blobs = repository(uuids=[START, "41000000-0000-4000-8000-000000000002"])
        auth = seed_auth(store)
        bundle = make_bundle()
        view = provision(repo, auth, bundle)
        started = repo.start(auth, StartCommand(REQUEST, 501, 101, 1001, bundle.definition_hash), kind="content", view=view, template=None)
        assert started.created is True
        waiting_ticket = repo.begin_inventory(auth, bundle.scope.learner)
        repo.apply_inventory(auth, waiting_ticket, CourseError("ARC_PROGRESS_UNAVAILABLE"))
        with pytest.raises(CourseError) as raised:
            repo.start(
                auth, StartCommand(REQUEST_B, 501, 101, 1001, bundle.definition_hash),
                kind="content", view=repo.load_start_view(auth, StartCommand(REQUEST_B, 501, 101, 1001, bundle.definition_hash)),
                template=None,
            )
        assert raised.value.code == "ARC_PROGRESS_UNAVAILABLE"
        receipt = repo.report(
            auth, course_id=101, enrollment_id=501, placement_id=1001,
            report=ContentReport(REPORT_B, started.start_id, "video-v1", "video_segments", [[0, 10000]]),
        )
        body = parse_json(receipt.response_json)
        assert body["application"] in {"applied", "unchanged"}
        assert body["isCompleted"] is False


class TestV07IdempotentReplay:
    def test_same_request_replays_after_create_and_waiting_gate(self):
        repo, store, blobs = repository(uuids=[START])
        auth = seed_auth(store)
        bundle = make_bundle()
        view = provision(repo, auth, bundle)
        command = StartCommand(REQUEST, 501, 101, 1001, bundle.definition_hash)
        first = repo.start(auth, command, kind="content", view=view, template=None)
        waiting = repo.begin_inventory(auth, bundle.scope.learner)
        repo.apply_inventory(auth, waiting, CourseError("ARC_PROGRESS_UNAVAILABLE"))
        replay = repo.find_created(auth, command, kind="content")
        assert replay is not None
        assert replay.created is False
        assert replay.start_id == first.start_id
        second = repo.start(auth, command, kind="content", view=view, template=None)
        assert second.created is False
        assert second.start_id == first.start_id
        other = StartCommand(REQUEST, 501, 101, 1002, bundle.definition_hash)
        with pytest.raises(CourseError) as raised:
            repo.find_created(auth, other, kind="content")
        assert raised.value.code == "IDEMPOTENCY_CONFLICT"


class TestV08EpochHeadAndFinal:
    def test_head_and_final_created_together_and_partial_is_503(self):
        repo, store, blobs = repository()
        auth = seed_auth(store)
        bundle = make_bundle()
        gate = repo.ensure_epoch(auth, binding_of(bundle))
        assert gate.state == "waiting"
        scope = scope_key(bundle.scope)
        head = store.get_item(course_head_key(scope, EPOCH))
        final = store.get_item(course_final_key(scope, EPOCH))
        assert head is not None and final is not None
        assert head["completed_placements"] == []
        assert final["phase"] == "free"
        assert store.get_item(course_item_key(scope, EPOCH, placement_key(scope, "src-place-1001"))) is None
        again = repo.ensure_epoch(auth, binding_of(bundle))
        assert again.revision == gate.revision
        del store.items[(course_final_key(scope, EPOCH)["PK"], course_final_key(scope, EPOCH)["SK"])]
        with pytest.raises(CourseError) as raised:
            repo.ensure_epoch(auth, binding_of(bundle))
        assert raised.value.status == 503

    def test_dummy_epoch_change_does_not_fanout_delete(self):
        repo, store, blobs = repository(uuids=[START])
        auth = seed_auth(store)
        bundle = make_bundle()
        view = provision(repo, auth, bundle)
        repo.start(auth, StartCommand(REQUEST, 501, 101, 1001, bundle.definition_hash), kind="content", view=view, template=None)
        scope = scope_key(bundle.scope)
        old_head = store.get_item(course_head_key(scope, EPOCH))
        user = store.get_item(user_key(auth.principal))
        user["epoch"] = "90000000-0000-4000-8000-000000000009"
        user["revision"] = 1
        store.seed(user)
        assert store.get_item(course_head_key(scope, EPOCH)) == old_head
        new_auth = AuthContext(auth.session_id, auth.principal, 1, auth.expires_at)
        # Session revision must match USER writes: keep session revision 0 vs user 1 would fail session check.
        session = store.get_item(session_key(SESSION))
        session["revision"] = 1
        store.seed(session)
        repo.ensure_epoch(new_auth, binding_of(bundle))
        assert store.get_item(course_head_key(scope, EPOCH)) is not None
        assert store.get_item(course_head_key(scope, "90000000-0000-4000-8000-000000000009")) is not None


class TestV10ConcurrentTraining:
    def test_two_incomplete_starts_then_complete_still_allows_a_new_start(self):
        uuids = [ATTEMPT, "51000000-0000-4000-8000-000000000002", "52000000-0000-4000-8000-000000000003"]
        repo, store, blobs = repository(uuids=uuids)
        auth = seed_auth(store)
        bundle = make_bundle()
        view = provision(repo, auth, bundle)
        progress = parse_json(view.progress_json)
        for link in (1001, 1002):
            key = placement_key(view.scope_key, next(item.source_id for item in bundle.placements if item.public_link_id == link))
            progress.setdefault("items", {})
            progress["items"][key] = {
                "source_id": next(item.source_id for item in bundle.placements if item.public_link_id == link),
                "public_link_id": link, "kind": "video" if link == 1001 else "document",
                "content_version": "video-v1" if link == 1001 else "document-v1",
                "completed": True, "passed": None,
            }
            progress.setdefault("completed_placements", []).append(key)
        head = store.get_item(course_head_key(view.scope_key, EPOCH))
        head["progress_json"] = json_bytes(progress).decode("utf-8")
        store.seed(head)
        view = repo.load_start_view(auth, StartCommand(REQUEST, 501, 101, 1003, bundle.definition_hash))
        template = AttemptTemplate(
            {"attempt_id": ATTEMPT, "program_id": "mock-cpr", "target": "adult",
             "definition_json": bundle.placements[2].execution_json.decode()},
            CourseBinding(
                view.scope_key, placement_key(view.scope_key, "src-place-1003"), "training",
                bundle.definition_hash, "training-v1", EPOCH, POLICY_VERSION,
            ),
        )
        first = repo.start(auth, StartCommand(REQUEST, 501, 101, 1003, bundle.definition_hash), kind="attempt", view=view, template=template)
        second_template = AttemptTemplate(
            {"attempt_id": "51000000-0000-4000-8000-000000000002", "program_id": "mock-cpr", "target": "adult",
             "definition_json": bundle.placements[2].execution_json.decode()},
            template.binding,
        )
        second = repo.start(
            auth, StartCommand(REQUEST_B, 501, 101, 1003, bundle.definition_hash),
            kind="attempt", view=view, template=second_template,
        )
        assert first.attempt_id != second.attempt_id
        item = store.get_item(course_item_key(view.scope_key, EPOCH, placement_key(view.scope_key, "src-place-1003")))
        item["completed"] = True
        item["revision"] = item["revision"] + 1
        store.seed(item)
        progress = parse_json(store.get_item(course_head_key(view.scope_key, EPOCH))["progress_json"])
        key = placement_key(view.scope_key, "src-place-1003")
        progress.setdefault("completed_placements", []).append(key)
        progress.setdefault("items", {})[key] = {
            "source_id": "src-place-1003", "public_link_id": 1003, "kind": "training",
            "content_version": "training-v1", "completed": True, "passed": True,
        }
        head = store.get_item(course_head_key(view.scope_key, EPOCH))
        head["progress_json"] = json_bytes(progress).decode("utf-8")
        store.seed(head)
        # D130: the completed item is started again; the ITEM row keeps its completion.
        later_view = repo.load_start_view(auth, StartCommand("12000000-0000-4000-8000-000000000001", 501, 101, 1003, bundle.definition_hash))
        third = repo.start(
            auth, StartCommand("12000000-0000-4000-8000-000000000001", 501, 101, 1003, bundle.definition_hash),
            kind="attempt", view=later_view,
            template=AttemptTemplate(
                {"attempt_id": "52000000-0000-4000-8000-000000000003", "program_id": "mock-cpr", "target": "adult",
                 "definition_json": bundle.placements[2].execution_json.decode()},
                template.binding,
            ),
        )
        assert third.created is True and third.attempt_id == "52000000-0000-4000-8000-000000000003"
        assert store.get_item(course_item_key(view.scope_key, EPOCH, key))["completed"] is True


class TestContentReportAndHistorical:
    def test_video_complete_and_historical_only_nulls(self):
        repo, store, blobs = repository(uuids=[START])
        auth = seed_auth(store)
        bundle = make_bundle()
        view = provision(repo, auth, bundle)
        started = repo.start(auth, StartCommand(REQUEST, 501, 101, 1001, bundle.definition_hash), kind="content", view=view, template=None)
        first = repo.report(
            auth, course_id=101, enrollment_id=501, placement_id=1001,
            report=ContentReport(REPORT_A, started.start_id, "video-v1", "video_segments", [[0, 10000]]),
        )
        assert parse_json(first.response_json)["isCompleted"] is False
        second = repo.report(
            auth, course_id=101, enrollment_id=501, placement_id=1001,
            report=ContentReport(REPORT_B, started.start_id, "video-v1", "video_segments", [[10000, 20000]]),
        )
        body = parse_json(second.response_json)
        assert body["isCompleted"] is True
        replay = repo.report(
            auth, course_id=101, enrollment_id=501, placement_id=1001,
            report=ContentReport(REPORT_A, started.start_id, "video-v1", "video_segments", [[0, 10000]]),
        )
        assert parse_json(replay.response_json)["isCompleted"] is False
        original_keys = [course_head_key(view.scope_key, EPOCH), course_final_key(view.scope_key, EPOCH),
                         course_item_key(view.scope_key, EPOCH, placement_key(view.scope_key, bundle.placements[0].source_id))]
        original_rows = [store.get_item(key) for key in original_keys]
        user = store.get_item(user_key(auth.principal))
        user["epoch"] = "90000000-0000-4000-8000-000000000009"
        user["revision"] = 1
        store.seed(user)
        session = store.get_item(session_key(SESSION))
        session["revision"] = 1
        store.seed(session)
        late_auth = AuthContext(SESSION, auth.principal, 1, auth.expires_at)
        historical = repo.report(
            late_auth, course_id=101, enrollment_id=501, placement_id=1001,
            report=ContentReport("31000000-0000-4000-8000-000000000001", started.start_id, "video-v1", "video_segments", [[0, 20000]]),
        )
        late = parse_json(historical.response_json)
        assert late["application"] == "historical_only"
        assert late["isCompleted"] is None
        assert late["isPassed"] is None
        assert late["courseStatus"] is None
        current = store.get_item(course_head_key(view.scope_key, "90000000-0000-4000-8000-000000000009"))
        assert current is None
        assert [store.get_item(key) for key in original_keys] == original_rows


class TestHookSpecPresent:
    def test_hook_requests_cover_w5_signatures(self):
        text = HOOK_REQUESTS
        for token in (
            "CourseStore.get_item", "CourseStore.transact", "if_not_exists", "if_match",
            "begin_inventory", "apply_refresh", "COURSE_CREATE", "historical_only",
            "V06", "V07", "V08", "V10", "V11", "V22",
        ):
            assert token in text
        assert "state.py" in text

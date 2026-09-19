"""V22 fixture limit boundaries for course policy and repository. No sleep."""

from copy import deepcopy
from pathlib import Path
import sys

import pytest

from mock_journey.course_contracts import ContentReport, StartCommand
from mock_journey.course_errors import CourseError
from mock_journey.course_policy import CoursePolicy
from mock_journey.course_settings import fixture_course_settings
from mock_journey.course_state import (
    DynamoCourseRepository, InMemoryBlobStore, InMemoryCourseStore, _action_key, _start_key,
)
from mock_journey.typed import json_bytes, parse_json

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_vcc_state import EPOCH, REQUEST, START, make_bundle, provision, seed_auth

REPORT = "30000000-0000-4000-8000-000000000001"


def policy():
    return CoursePolicy(fixture_course_settings())


def video_evidence(merged=None, report_count=0, completed=False):
    return {
        "start_id": START, "public_link_id": 1001, "kind": "video",
        "content_version": "video-v1", "duration_ms": 20000,
        "merged_intervals_ms": merged or [], "report_count": report_count,
        "completed": completed, "course_status": "IN_PROGRESS",
    }


class TestV22PolicyBoundaries:
    def test_intervals_per_report_boundary_and_plus_one(self):
        settings = fixture_course_settings()
        evidence = video_evidence()
        ok = [[i, i + 1] for i in range(settings.max_intervals_per_report)]
        receipt = parse_json(policy().evaluate_content(
            json_bytes(evidence), ContentReport(REPORT, START, "video-v1", "video_segments", ok),
        ))
        assert receipt["application"] == "applied"
        too_many = ok + [[settings.max_intervals_per_report, settings.max_intervals_per_report + 1]]
        with pytest.raises(CourseError) as raised:
            policy().evaluate_content(
                json_bytes(evidence), ContentReport(REPORT, START, "video-v1", "video_segments", too_many),
            )
        assert raised.value.code == "PAYLOAD_TOO_LARGE"
        assert raised.value.status == 413

    def test_merged_intervals_boundary_preserves_evidence(self):
        settings = fixture_course_settings()
        merged = [[i * 2, i * 2 + 1] for i in range(settings.max_merged_intervals_per_start)]
        evidence = video_evidence(merged=merged, completed=False)
        extra = ContentReport(REPORT, START, "video-v1", "video_segments", [[2000, 2001]] if False else [[settings.max_merged_intervals_per_start * 2, settings.max_merged_intervals_per_start * 2 + 1]])
        with pytest.raises(CourseError) as raised:
            policy().evaluate_content(json_bytes(evidence), extra)
        assert raised.value.code == "PROGRESS_CAPACITY_EXCEEDED"
        assert raised.value.status == 413
        boundary = video_evidence(merged=merged[:-1])
        last = ContentReport(
            REPORT, START, "video-v1", "video_segments",
            [[(settings.max_merged_intervals_per_start - 1) * 2, (settings.max_merged_intervals_per_start - 1) * 2 + 1]],
        )
        receipt = parse_json(policy().evaluate_content(json_bytes(boundary), last))
        assert receipt["application"] == "applied"
        assert receipt["isCompleted"] is False

    def test_reports_per_start_boundary(self):
        settings = fixture_course_settings()
        at_limit = video_evidence(report_count=settings.max_reports_per_start)
        with pytest.raises(CourseError) as raised:
            policy().evaluate_content(
                json_bytes(at_limit),
                ContentReport(REPORT, START, "video-v1", "video_segments", [[0, 1]]),
            )
        assert raised.value.code == "PROGRESS_CAPACITY_EXCEEDED"
        under = video_evidence(report_count=settings.max_reports_per_start - 1)
        receipt = parse_json(policy().evaluate_content(
            json_bytes(under), ContentReport(REPORT, START, "video-v1", "video_segments", [[0, 1]]),
        ))
        assert receipt["application"] == "applied"


class TestV22RepositoryLimits:
    def test_conflict_retries_then_503_without_partial_write(self):
        store = InMemoryCourseStore()
        blobs = InMemoryBlobStore()
        repo = DynamoCourseRepository(
            store, fixture_course_settings(), CoursePolicy(fixture_course_settings()), blobs,
            clock=lambda: 1_000_000, uuid_factory=lambda: START,
        )
        auth = seed_auth(store)
        bundle = make_bundle()
        view = provision(repo, auth, bundle)
        before = deepcopy(store.items)
        calls_before = len(store.calls)
        store.always_conflict = True
        with pytest.raises(CourseError) as raised:
            repo.start(
                auth, StartCommand(REQUEST, 501, 101, 1001, bundle.definition_hash),
                kind="content", view=view, template=None,
            )
        assert raised.value.code == "TEMPORARILY_UNAVAILABLE"
        assert raised.value.status == 503
        assert len(store.calls) - calls_before == fixture_course_settings().max_conflict_retries
        store.always_conflict = False
        assert store.items == before

    def test_transact_actions_unique_and_within_20(self):
        store = InMemoryCourseStore()
        blobs = InMemoryBlobStore()
        repo = DynamoCourseRepository(
            store, fixture_course_settings(), CoursePolicy(fixture_course_settings()), blobs,
            clock=lambda: 1_000_000, uuid_factory=lambda: START,
        )
        auth = seed_auth(store)
        bundle = make_bundle()
        view = provision(repo, auth, bundle)
        repo.start(
            auth, StartCommand(REQUEST, 501, 101, 1001, bundle.definition_hash),
            kind="content", view=view, template=None,
        )
        start_actions = store.calls[-1]
        keys = [_action_key(action) for action in start_actions]
        assert len(keys) == len(set(keys))
        assert len(start_actions) <= fixture_course_settings().max_transaction_actions
        assert len(start_actions) <= 20
        repo.report(
            auth, course_id=101, enrollment_id=501, placement_id=1001,
            report=ContentReport(REPORT, START, "video-v1", "video_segments", [[0, 10000]]),
        )
        report_actions = store.calls[-1]
        report_keys = [_action_key(action) for action in report_actions]
        assert len(report_keys) == len(set(report_keys))
        assert len(report_actions) <= 20
        for command_actions in store.calls:
            assert len(command_actions) <= 20
            seen = [_action_key(action) for action in command_actions]
            assert len(seen) == len(set(seen))

    def test_same_digest_replay_after_capacity(self):
        store = InMemoryCourseStore()
        blobs = InMemoryBlobStore()
        repo = DynamoCourseRepository(
            store, fixture_course_settings(), CoursePolicy(fixture_course_settings()), blobs,
            clock=lambda: 1_000_000, uuid_factory=lambda: START,
        )
        auth = seed_auth(store)
        bundle = make_bundle()
        view = provision(repo, auth, bundle)
        started = repo.start(
            auth, StartCommand(REQUEST, 501, 101, 1001, bundle.definition_hash),
            kind="content", view=view, template=None,
        )
        first = repo.report(
            auth, course_id=101, enrollment_id=501, placement_id=1001,
            report=ContentReport(REPORT, started.start_id, "video-v1", "video_segments", [[0, 10000]]),
        )
        start_row = store.get_item(_start_key(view.scope_key, EPOCH, started.start_id))
        start_row["report_count"] = fixture_course_settings().max_reports_per_start
        store.seed(start_row)
        replay = repo.report(
            auth, course_id=101, enrollment_id=501, placement_id=1001,
            report=ContentReport(REPORT, started.start_id, "video-v1", "video_segments", [[0, 10000]]),
        )
        assert parse_json(replay.response_json) == parse_json(first.response_json)
        with pytest.raises(CourseError) as raised:
            repo.report(
                auth, course_id=101, enrollment_id=501, placement_id=1001,
                report=ContentReport("33000000-0000-4000-8000-000000000003", started.start_id, "video-v1", "video_segments", [[1, 2]]),
            )
        assert raised.value.code == "PROGRESS_CAPACITY_EXCEEDED"
        assert parse_json(first.response_json)["isCompleted"] is False

"""Shared course primitives keep every per-caller rule as an explicit argument.

Each primitive is compared with the copy it replaces (or will replace, for the
modules another change owns) on independent inputs; nothing here re-derives an
expected value from the code under test.
"""

import inspect
import random

import pytest

from mock_journey import course_http
from mock_journey.course_contracts import (
    APP_ROUTES, RECEIPT_FORBIDDEN_KEYS, RESUME_SECRET_KEYS, UUID_PATTERN, waiting_reason_for,
)
from mock_journey.course_errors import AUTH_ERROR_CODES, CourseError
from mock_journey.course_primitives import (
    contains_key, fail, require_exact_bool, require_exact_int, require_natural_int,
    require_positive_int, require_text, rfc3339_precise, rfc3339_seconds,
)
from mock_journey.course_response import availability_or_waiting
from mock_journey.course_settings import (
    MAX_CONFLICT_RETRIES_BOUND, CourseSettings, fixture_course_settings, require_course_settings,
)
from mock_journey.errors import JourneyError


CODES = ("INVALID_REQUEST", "UPSTREAM_CONTRACT_MISMATCH", "TEMPORARILY_UNAVAILABLE")


def code_of(call, *args, **kwargs):
    with pytest.raises(CourseError) as raised:
        call(*args, **kwargs)
    return raised.value.code


class TestValidationPrimitives:
    def test_code_is_a_required_keyword(self):
        for helper in (require_text, require_exact_bool, require_exact_int, require_natural_int,
                       require_positive_int, rfc3339_precise):
            parameter = inspect.signature(helper).parameters["code"]
            assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
            assert parameter.default is inspect.Parameter.empty
        assert inspect.signature(require_text).parameters["check_utf8"].default is inspect.Parameter.empty

    @pytest.mark.parametrize("code", CODES)
    def test_every_failure_uses_the_given_code(self, code):
        assert code_of(fail, code) == code
        for value in ("", None, 0, b"x"):
            assert code_of(require_text, value, code=code, check_utf8=True) == code
            assert code_of(require_text, value, code=code, check_utf8=False) == code
        assert code_of(require_text, "\ud800", code=code, check_utf8=True) == code
        assert code_of(require_exact_bool, 1, code=code) == code
        assert code_of(require_exact_int, True, code=code) == code
        assert code_of(require_natural_int, -1, code=code) == code
        assert code_of(require_positive_int, 0, code=code) == code

    def test_utf8_check_is_optional(self):
        assert require_text("\ud800", code="X_UNUSED", check_utf8=False) == "\ud800"
        with pytest.raises(CourseError) as raised:
            require_text("a\udfffb", code="INVALID_REQUEST", check_utf8=True)
        assert raised.value.__suppress_context__ is True

    def test_valid_values_are_returned_unchanged(self):
        text = "value"
        assert require_text(text, code="INVALID_REQUEST", check_utf8=True) is text
        assert require_exact_bool(False, code="INVALID_REQUEST") is False
        assert require_exact_int(-3, code="INVALID_REQUEST") == -3
        assert require_natural_int(0, code="INVALID_REQUEST") == 0
        assert require_positive_int(1, code="INVALID_REQUEST") == 1


def reference_walk(value, keys):
    """Independent iterative walk: the first forbidden key in pre-order, or None."""
    stack = [value]
    while stack:
        node = stack.pop()
        if type(node) is dict:
            for key in node:
                if key in keys:
                    return key
            stack.extend(reversed(list(node.values())))
        elif type(node) is list:
            stack.extend(reversed(node))
    return None


class TestContainsKey:
    @pytest.mark.parametrize("value,expected", [
        ({}, False), ([], False), ("secret", False), (["secret", ("secret",)], False),
        ({"secret": None}, True), ([{"a": [[{"secret": 1}]]}], True),
        ({"a": ("tuple", {"secret": 1})}, False),  # tuples are not JSON arrays; not walked
        ({"a": {"b": "secret"}}, False),
    ])
    def test_keys_only_in_dicts_and_lists(self, value, expected):
        assert contains_key(value, {"secret"}) is expected

    def test_matches_an_independent_walk_on_random_trees(self):
        rng = random.Random(20260928)
        keys = ["a", "b", "secret", "principal"]

        def tree(depth):
            if depth == 0 or rng.random() < 0.3:
                return rng.choice([None, 1, "secret", True])
            if rng.random() < 0.5:
                return [tree(depth - 1) for _ in range(rng.randint(0, 3))]
            return {rng.choice(keys): tree(depth - 1) for _ in range(rng.randint(0, 3))}

        for _ in range(2000):
            value = tree(5)
            for forbidden in ({"secret"}, {"principal", "b"}, frozenset()):
                assert contains_key(value, forbidden) is (reference_walk(value, forbidden) is not None)

    def test_deep_nesting_still_raises_recursion_error(self):
        value = []
        for _ in range(5000):
            value = [value]
        with pytest.raises(RecursionError):
            contains_key(value, {"secret"})

    def test_resume_secret_keys_extend_the_receipt_set_as_course_submission_does(self):
        assert RESUME_SECRET_KEYS == {"resume_credential", "resume_nonce", "resume_digest", "resume_key_version"}
        from mock_journey.course_submission import _forbid_secrets
        for key in sorted(RECEIPT_FORBIDDEN_KEYS | RESUME_SECRET_KEYS):
            assert contains_key({"x": [{key: 1}]}, RECEIPT_FORBIDDEN_KEYS | RESUME_SECRET_KEYS)
            assert code_of(_forbid_secrets, {"x": [{key: 1}]}) == "INVALID_REQUEST"
        for key in ("resumeNonce", "token", "session_id"):
            assert not contains_key({key: 1}, RECEIPT_FORBIDDEN_KEYS | RESUME_SECRET_KEYS)
            assert _forbid_secrets({key: 1}) is None


class TestRfc3339Primitives:
    SAMPLES = [0, 1, 59, 86_399, 1_800_000_000, 1_800_086_400, 253402300799, -1, -86_400,
               0.5, 1.9, 1_800_000_000.999999, True, False]

    def test_precise_matches_handwritten_values(self):
        assert rfc3339_precise(1_800_000_000, code="TEMPORARILY_UNAVAILABLE") == "2027-01-15T08:00:00Z"
        assert rfc3339_precise(0.125, code="TEMPORARILY_UNAVAILABLE") == "1970-01-01T00:00:00.125Z"
        assert rfc3339_precise(-0.5, code="TEMPORARILY_UNAVAILABLE") == "1969-12-31T23:59:59.5Z"
        for bad in (True, "0", None, float("nan")):
            assert code_of(rfc3339_precise, bad, code="STORED_INPUT_INVALID") == "STORED_INPUT_INVALID"

    def test_whole_seconds_matches_the_existing_copies(self):
        from mock_journey import course_records, course_wiring
        rng = random.Random(7)
        samples = self.SAMPLES + [rng.uniform(-10**9, 253402300799) for _ in range(500)]
        samples += [rng.randint(-10**9, 253402300799) for _ in range(500)]
        for value in samples:
            expected = course_wiring._rfc3339(value)
            assert rfc3339_seconds(value) == expected
            assert course_records._rfc3339(value) == expected

    def test_whole_seconds_known_values(self):
        assert rfc3339_seconds(1_800_000_300) == "2027-01-15T08:05:00Z"
        assert rfc3339_seconds(1.999) == "1970-01-01T00:00:01Z"
        assert rfc3339_seconds(-0.5) == "1970-01-01T00:00:00Z"
        with pytest.raises(ValueError):
            rfc3339_seconds(float("nan"))


class TestAvailabilityHelper:
    def _read(self, outcome):
        def read(auth):
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        return read

    @pytest.mark.parametrize("absorb", [False, True])
    def test_course_errors(self, absorb):
        assert availability_or_waiting(self._read(CourseError("CONTRACT_PENDING")), None,
                                       absorb_unexpected=absorb) == {"state": "waiting", "reason": "contract_pending"}
        for code in ("TEMPORARILY_UNAVAILABLE", "NOT_FOUND", "ARC_PROGRESS_UNAVAILABLE"):
            assert availability_or_waiting(self._read(CourseError(code)), None, absorb_unexpected=absorb) == {
                "state": "waiting", "reason": "arc_progress_unavailable"}
        for code in sorted(AUTH_ERROR_CODES):
            assert code_of(availability_or_waiting, self._read(CourseError(code)), None,
                           absorb_unexpected=absorb) == code

    @pytest.mark.parametrize("absorb", [False, True])
    def test_journey_errors_always_propagate(self, absorb):
        with pytest.raises(JourneyError):
            availability_or_waiting(self._read(JourneyError("SESSION_EXPIRED")), None, absorb_unexpected=absorb)

    def test_unexpected_errors_depend_on_the_caller(self):
        with pytest.raises(RuntimeError):
            availability_or_waiting(self._read(RuntimeError("x")), None, absorb_unexpected=False)
        assert availability_or_waiting(self._read(RuntimeError("x")), None, absorb_unexpected=True) == {
            "state": "waiting", "reason": "arc_progress_unavailable"}
        # A non-RefreshResult value is an upstream contract error, therefore waiting.
        assert availability_or_waiting(self._read(object()), None, absorb_unexpected=False) == {
            "state": "waiting", "reason": "arc_progress_unavailable"}

    def test_matches_course_wiring_session_reader_with_absorb(self):
        from types import SimpleNamespace
        from mock_journey.course_wiring import _session_reader
        journey = SimpleNamespace(check_session=lambda auth: None)
        auth = SimpleNamespace(session_id="s", expires_at=0)
        outcomes = [CourseError(code) for code in sorted(AUTH_ERROR_CODES)] + [
            CourseError("CONTRACT_PENDING"), CourseError("NOT_FOUND"), RuntimeError("x"), ValueError("y"),
            JourneyError("SESSION_EXPIRED"), object(),
        ]

        def run(call):
            try:
                return ("ok", call())
            except (CourseError, JourneyError) as error:
                return (type(error).__name__, error.code)

        for outcome in outcomes:
            service = SimpleNamespace(stored_refresh=self._read(outcome))
            wired = run(lambda: _session_reader(journey, service)(auth))
            if wired[0] == "ok":
                wired = ("ok", wired[1].learning_availability)
            helper = run(lambda: availability_or_waiting(service.stored_refresh, auth, absorb_unexpected=True))
            assert helper == wired

    def test_waiting_reason(self):
        assert waiting_reason_for(CourseError("CONTRACT_PENDING")) == "contract_pending"
        assert waiting_reason_for(CourseError("TEMPORARILY_UNAVAILABLE")) == "arc_progress_unavailable"
        assert AUTH_ERROR_CODES == {"LOGIN_FAILED", "SESSION_REQUIRED", "SESSION_EXPIRED", "SESSION_REVOKED"}


class TestSettingsAndRouteConstants:
    def test_require_course_settings_keeps_the_callers_error(self):
        settings = fixture_course_settings()
        assert require_course_settings(settings, lambda: AssertionError()) is settings
        with pytest.raises(TypeError, match=r"\AInvalid course settings\.\Z"):
            require_course_settings(object(), lambda: TypeError("Invalid course settings."))
        assert code_of(require_course_settings, None, lambda: CourseError("TEMPORARILY_UNAVAILABLE")) \
            == "TEMPORARILY_UNAVAILABLE"

        class Subclass(CourseSettings):
            pass

        subclass = Subclass(**{name: 1 for name in CourseSettings.__dataclass_fields__})
        with pytest.raises(TypeError):
            require_course_settings(subclass, lambda: TypeError("Invalid course settings."))

    def test_conflict_retry_bound(self):
        assert MAX_CONFLICT_RETRIES_BOUND == 8

    def test_route_rule_tables_name_real_routes(self):
        route_ids = {spec.route_id for spec in APP_ROUTES}
        assert set(course_http._REQUIRED_QUERY) <= route_ids
        assert set(course_http._QUERY_DEFAULTS) <= route_ids
        for route_id, keys in course_http._REQUIRED_QUERY.items():
            spec = next(spec for spec in APP_ROUTES if spec.route_id == route_id)
            assert set(keys) <= set(spec.query_allowed)
        for route_id, defaults in course_http._QUERY_DEFAULTS.items():
            spec = next(spec for spec in APP_ROUTES if spec.route_id == route_id)
            assert tuple(key for key, _ in defaults) == spec.query_allowed
        assert dict(course_http._QUERY_DEFAULTS["course_list"]) == {"page": 1, "pageSize": 100}
        assert course_http._PUBLIC_ID_DIGITS == 16
        assert course_http._PATH_ID_PATTERN == r"[1-9][0-9]{0,15}"
        assert UUID_PATTERN == r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
        body_kinds = {spec.body_kind for spec in APP_ROUTES}
        assert course_http._UNLIMITED_BODY_KINDS == {"none", "measurement"} <= body_kinds
        assert course_http._DUMMY_LOGIN == "test@test.com"

"""In-memory CourseStore and blob store fakes for unit tests (not production).

Moved verbatim from mock_journey/course_state.py (S7-04) so the Lambda bundle,
which excludes tests/, no longer carries them. Production code must not import
this module; tests import the fakes from here.
"""

from copy import deepcopy
import hashlib

from mock_journey.course_errors import CourseError


class InMemoryBlobStore:
    """Unit-test blob hook. W5 replaces this with JourneyStorage."""

    def __init__(self):
        self.objects = {}

    def put_bytes(self, body: bytes) -> str:
        if type(body) is not bytes:
            raise CourseError("TEMPORARILY_UNAVAILABLE")
        digest_value = hashlib.sha256(body).hexdigest()
        self.objects[digest_value] = body
        return digest_value

    def get_bytes(self, digest_value: str):
        return self.objects.get(digest_value)


class InMemoryCourseStore:
    """Atomic fake for CourseStore.action shapes. No sleep; conflicts are explicit."""

    def __init__(self):
        self.items = {}
        self.calls = []
        self.conflict_remaining = 0
        self.always_conflict = False

    def seed(self, item):
        self.items[(item["PK"], item["SK"])] = deepcopy(item)

    def get_item(self, key: dict):
        item = self.items.get((key["PK"], key["SK"]))
        return deepcopy(item) if item is not None else None

    def transact(self, actions: list) -> bool:
        self.calls.append(deepcopy(actions))
        if self.always_conflict:
            return False
        if self.conflict_remaining > 0:
            self.conflict_remaining -= 1
            return False
        snapshot = deepcopy(self.items)
        writes = []
        for action in actions:
            if action["op"] == "put" and "if_greater" in action:
                # Same shape rule as the DynamoDB adapter: if_greater is condition_check only.
                raise ValueError("if_greater is only valid on condition_check")
            key = (action["key"]["PK"], action["key"]["SK"])
            current = snapshot.get(key)
            if not _action_allowed(current, action):
                return False
            if action["op"] == "put":
                writes.append((key, deepcopy(action["item"])))
        for key, item in writes:
            snapshot[key] = item
        self.items = snapshot
        return True


def _action_allowed(current, action):
    greater = action.get("if_greater")
    if greater is not None and (current is None or not _fields_greater(current, greater)):
        return False
    match = action.get("if_match")
    missing_or = action.get("if_missing_or_match")
    if action.get("if_not_exists"):
        return current is None
    if missing_or is not None:
        if current is None:
            return True
        return _fields_match(current, missing_or)
    if match is not None:
        if current is None:
            return False
        return _fields_match(current, match)
    if action["op"] == "condition_check":
        return current is not None
    return current is not None or action["op"] == "put"


def _fields_match(item, expected):
    for key, value in expected.items():
        if item.get(key) != value:
            return False
    return True


def _fields_greater(item, expected):
    # DynamoDB number comparison: a missing or non-number attribute fails.
    for key, value in expected.items():
        current = item.get(key)
        if type(current) is not int or type(value) is not int or current <= value:
            return False
    return True

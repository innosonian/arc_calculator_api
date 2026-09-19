"""Course snapshots in the existing private, checksummed artifact namespace."""

from mock_journey.course_errors import CourseError
from mock_journey.errors import JourneyError


class CourseBlobStore:
    def __init__(self, storage):
        self.storage = storage

    def put_bytes(self, body):
        try:
            return self.storage.put_course_blob(body)
        except JourneyError:
            raise CourseError("TEMPORARILY_UNAVAILABLE") from None

    def get_bytes(self, checksum):
        try:
            return self.storage.get_course_blob(checksum)
        except JourneyError:
            raise CourseError("TEMPORARILY_UNAVAILABLE") from None

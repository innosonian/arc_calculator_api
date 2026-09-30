"""The assembled course application's mode marker, with no imports.

``course_wiring.CourseApplication.course_mode`` carries this value. Entry
points that only compare the marker (the Lambda handler, the local HTTP
server) can read it here without loading the course stack.
"""

COURSE_MODE = "course_v2"

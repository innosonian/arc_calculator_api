"""Exact VCC display schemas shared by provider and response boundaries.

These validate the app contract, not execution policy or an unverified ARC schema.
No coercion or missing-field defaults are allowed: Swift Decodable consumers must
receive the same scalar and nullable types for every verified snapshot.
"""

from math import isfinite

from mock_journey.course_contracts import (
    ENROLLMENT_FIELDS, FILE_DETAIL_FIELDS, ITEM_DETAIL_OUTER_FIELDS,
    TRAINING_PROGRAM_DETAIL_FIELDS, USAGE_VALUES, item_type_wire, parse_owned,
    require_public_id, require_uuid,
)
from mock_journey.course_errors import CourseError


_TRAINING_FIELDS = (
    "manikinType", "duration", "compressionLimit", "ventilationLimit", "cycleLimit",
    "compressionVentilationRatio", "aed", "cprGuideline", "twoRescuers",
)
_GUIDELINE_TEXT = ("title", "manikinType", "name")
_GUIDELINE_INTS = (
    "compressionDepthMax", "compressionDepthMin", "compressionRateMax", "compressionRateMin",
    "ventilationVolumeMax", "ventilationVolumeMin", "ventilationRateMax", "ventilationRateMin",
)
_GUIDELINE_NUMBERS = ("compressionDepthMaxInch", "compressionDepthMinInch")


def _mismatch():
    raise CourseError("UPSTREAM_CONTRACT_MISMATCH")


def _object(value, fields):
    if type(value) is not dict or set(value) != set(fields):
        _mismatch()
    return value


def _scalar(value, scalar_type, *, nullable=False):
    if value is None and nullable:
        return
    if type(value) is not scalar_type:
        _mismatch()


def _id(value):
    try:
        require_public_id(value)
    except CourseError:
        _mismatch()


def validate_file_detail(value):
    _object(value, FILE_DETAIL_FIELDS)
    _id(value["id"])
    _scalar(value["fileName"], str)
    _scalar(value["order"], int)
    for key in ("url", "contentUrl"):
        _scalar(value[key], str, nullable=True)
    return value


def _training_domain(value):
    _object(value, _TRAINING_FIELDS)
    _scalar(value["manikinType"], str, nullable=True)
    for key in ("duration", "compressionLimit", "ventilationLimit", "cycleLimit"):
        _scalar(value[key], int, nullable=True)
    ratio = value["compressionVentilationRatio"]
    if ratio is not None:
        _object(ratio, ("title", "cvrVentilation", "cvrCompression"))
        _scalar(ratio["title"], str, nullable=True)
        for key in ("cvrVentilation", "cvrCompression"):
            _scalar(ratio[key], int, nullable=True)
    aed = value["aed"]
    if aed is not None:
        _object(aed, ("id", "cprGuide", "shockMode", "scenarioNo", "volume", "language", "padsDetection", "arrivalSeconds"))
        _id(aed["id"])
        for key in ("cprGuide", "shockMode", "language"):
            _scalar(aed[key], str)
        for key in ("scenarioNo", "volume", "arrivalSeconds"):
            _scalar(aed[key], int)
        _scalar(aed["padsDetection"], bool)
    guideline = value["cprGuideline"]
    if guideline is not None:
        _object(guideline, _GUIDELINE_TEXT + _GUIDELINE_INTS + _GUIDELINE_NUMBERS)
        for key in _GUIDELINE_TEXT:
            _scalar(guideline[key], str, nullable=True)
        for key in _GUIDELINE_INTS:
            _scalar(guideline[key], int, nullable=True)
        for key in _GUIDELINE_NUMBERS:
            number = guideline[key]
            if number is not None and (type(number) not in (int, float) or not isfinite(number)):
                _mismatch()
    rescuers = value["twoRescuers"]
    if rescuers is not None:
        _object(rescuers, ("cycleChangeCount",))
        _scalar(rescuers["cycleChangeCount"], int)


def validate_training_detail(value):
    _object(value, TRAINING_PROGRAM_DETAIL_FIELDS)
    _id(value["id"])
    for key in ("title", "trainingType", "feedbackType", "trainingMode"):
        _scalar(value[key], str)
    _training_domain(value["training"])
    assessment = _object(value["assessment"], ("passThreshold",))
    threshold = _object(assessment["passThreshold"], ("cpr", "aed"))
    for key in ("cpr", "aed"):
        _scalar(threshold[key], int, nullable=True)
    if type(value["content"]) is not list:
        _mismatch()
    for content in value["content"]:
        validate_file_detail(content)
    return value


def validate_placement_detail(placement):
    value = parse_owned(placement.detail_json)
    _object(value, ITEM_DETAIL_OUTER_FIELDS)
    for key in ("id", "courseItemLinkId"):
        _id(value[key])
    if value["id"] != placement.public_item_id or value["courseItemLinkId"] != placement.public_link_id:
        _mismatch()
    _scalar(value["displayOrder"], int)
    if value["displayOrder"] != placement.position:
        _mismatch()
    for key in ("title", "itemType", "usage", "logicalId"):
        _scalar(value[key], str)
    if value["itemType"] != item_type_wire(placement.kind)["detail_item_type"]:
        _mismatch()
    if value["usage"] not in USAGE_VALUES:
        _mismatch()
    try:
        require_uuid(value["logicalId"])
    except CourseError:
        _mismatch()
    _scalar(value["description"], str, nullable=True)
    if value["detail"] is not None:
        if placement.kind in ("video", "document"):
            validate_file_detail(value["detail"])
        else:
            validate_training_detail(value["detail"])
    return value


def validate_placement_order(placements):
    if not placements or placements[-1].kind != "assessment":
        _mismatch()
    positions = [item.position for item in placements]
    if any(type(position) is not int for position in positions):
        _mismatch()
    if any(left >= right for left, right in zip(positions, positions[1:])):
        _mismatch()


def validate_enrollment(value, *, enrollment_id, course_id):
    _object(value, ENROLLMENT_FIELDS)
    _id(value["id"])
    _id(value["courseId"])
    if value["id"] != enrollment_id or value["courseId"] != course_id:
        _mismatch()
    for key in ("status", "courseTitle"):
        _scalar(value[key], str)
    for key in ("loginAt", "finishedAt", "centerName", "enrollStatusCode"):
        _scalar(value[key], str, nullable=True)
    _scalar(value["elapsedSeconds"], int, nullable=True)
    return value


def validate_course_metadata(course, public_ids):
    if type(course) is not dict or not {"courseId", "courseName", "certificationType", "enrollment_metadata"} <= set(course):
        _mismatch()
    _id(course["courseId"])
    if course["courseId"] != public_ids.course_id:
        _mismatch()
    _scalar(course["courseName"], str)
    _scalar(course["certificationType"], str, nullable=True)
    metadata = course["enrollment_metadata"]
    if type(metadata) is not dict:
        _mismatch()
    row = metadata.get(str(public_ids.enrollment_id))
    return validate_enrollment(row, enrollment_id=public_ids.enrollment_id, course_id=public_ids.course_id)

"""Local catalog targets for Take Course. ARC SKU spellings await the contract."""

from types import MappingProxyType

from mock_journey.dev_course import COURSE_IDS


SKU_CATALOG = MappingProxyType({
    f"{course_type.lower()}-{target}": course_id
    for (course_type, target), course_id in COURSE_IDS.items()
})

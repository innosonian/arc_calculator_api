"""Course progress payload builders shared by course policy and state tests.

Moved unchanged from tests/test_vcc_policy.py so other test modules no longer
import a test module; test_vcc_policy re-exports the same objects.
"""

from mock_journey.course_policy import empty_progress, placement_key, scope_key


def item_key(bundle, public_link_id):
    item = next(row for row in bundle.placements if row.public_link_id == public_link_id)
    return placement_key(scope_key(bundle.scope), item.source_id)


def completed_progress(bundle, *link_ids, final=None, evaluation=None):
    payload = empty_progress()
    keys = [item_key(bundle, link) for link in link_ids]
    payload["completed_placements"] = keys
    payload["items"] = {
        item_key(bundle, item.public_link_id): {
            "source_id": item.source_id,
            "public_link_id": item.public_link_id,
            "kind": item.kind,
            "content_version": item.content_version,
            "completed": item.public_link_id in link_ids,
            "passed": True if item.kind == "assessment" and item.public_link_id in link_ids else (False if item.kind == "training" and item.public_link_id in link_ids else None),
        }
        for item in bundle.placements
    }
    if final is not None:
        payload["final"].update(final)
    if evaluation is not None:
        payload["evaluation"] = evaluation
    return payload

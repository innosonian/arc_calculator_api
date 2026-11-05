"""CRE submission boundary, pending the ARC contract."""

from typing import TypedDict

from services.arc_contract import ContractPending


class SubmissionReceipt(TypedDict):
    accepted: bool
    reference: str | None
    error: str | None


def submit(record: dict) -> SubmissionReceipt:
    raise ContractPending()

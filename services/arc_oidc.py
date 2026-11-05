"""ARC identity verifier boundary, pending the issuer contract."""

from dataclasses import dataclass

from services.arc_contract import ContractPending


@dataclass(frozen=True)
class LearnerIdentity:
    issuer: str
    subject: str


def verify(id_token: str) -> LearnerIdentity:
    raise ContractPending()

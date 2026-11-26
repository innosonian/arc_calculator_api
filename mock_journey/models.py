"""Authenticated metadata carries no bearer or resume secret."""
from dataclasses import dataclass


@dataclass(frozen=True)
class AuthContext:
    session_id: str
    principal: str
    revision: int
    expires_at: int
    organizations: tuple[tuple[str, str], ...] = ()
    organization_id: str | None = None

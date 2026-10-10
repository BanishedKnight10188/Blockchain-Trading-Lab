"""Atomic session operations; persistence failures are provider-independent."""

from typing import Protocol, runtime_checkable

from agent_platform.domain.events import SessionJournalEvent
from agent_platform.domain.sessions import AgentSession


class SessionNotFound(LookupError):
    pass


class RevisionConflict(ValueError):
    pass


class ActiveSessionExists(ValueError):
    pass


class PersistenceUnavailable(RuntimeError):
    pass


@runtime_checkable
class SessionStorePort(Protocol):
    async def active(self) -> AgentSession | None: ...

    async def get(self, session_id: str) -> AgentSession: ...

    async def create(self, session: AgentSession, event: SessionJournalEvent) -> AgentSession: ...

    async def save(
        self,
        session: AgentSession,
        expected_revision: int,
        event: SessionJournalEvent,
    ) -> AgentSession: ...

    async def history(self, session_id: str) -> tuple[SessionJournalEvent, ...]: ...

"""Explicit user style selection with optimistic concurrency and audit facts."""

from collections.abc import Callable
from uuid import uuid4

from agent_platform.domain.events import SessionJournalEvent
from agent_platform.domain.session_market import SessionAnalysisTarget
from agent_platform.domain.sessions import AgentSession, SessionStatus, TradingStyle
from agent_platform.ports.clock import ClockPort
from agent_platform.ports.sessions import RevisionConflict, SessionStorePort


class SessionService:
    def __init__(
        self,
        store: SessionStorePort,
        clock: ClockPort,
        id_factory: Callable[[], str] | None = None,
    ):
        self.store = store
        self.clock = clock
        self.new_id = id_factory or (lambda: uuid4().hex)

    async def create(
        self, style: TradingStyle, analysis_target: SessionAnalysisTarget | None = None
    ) -> AgentSession:
        if not isinstance(style, TradingStyle):
            raise TypeError("an explicit validated trading style is required")
        now = self.clock.utcnow()
        session = AgentSession(
            session_id=self.new_id(),
            style=style,
            analysis_target=analysis_target or SessionAnalysisTarget(),
            created_at=now,
            updated_at=now,
        )
        event = SessionJournalEvent(
            event_id=self.new_id(),
            kind="created",
            session=session,
            occurred_at=now,
        )
        return await self.store.create(session, event)

    async def change_style(
        self,
        session_id: str,
        style: TradingStyle,
        expected_revision: int,
    ) -> AgentSession:
        if type(expected_revision) is not int or expected_revision < 1:
            raise ValueError("expected revision must be a positive integer")
        current = await self.store.get(session_id)
        if current.revision != expected_revision:
            raise RevisionConflict("session changed; reload before saving")
        changed = current.change_style(style, self.clock.utcnow())
        if changed == current:
            return current
        event = SessionJournalEvent(
            event_id=self.new_id(),
            kind="style_changed",
            session=changed,
            occurred_at=changed.updated_at,
        )
        return await self.store.save(changed, expected_revision, event)

    async def transition(
        self,
        session_id: str,
        status: SessionStatus | str,
        expected_revision: int,
    ) -> AgentSession:
        if type(expected_revision) is not int or expected_revision < 1:
            raise ValueError("expected revision must be a positive integer")
        current = await self.store.get(session_id)
        if current.revision != expected_revision:
            raise RevisionConflict("session changed; reload before saving")
        changed = current.transition(status, self.clock.utcnow())
        if changed == current:
            return current
        event = SessionJournalEvent(
            event_id=self.new_id(),
            kind="state_changed",
            session=changed,
            occurred_at=changed.updated_at,
        )
        return await self.store.save(changed, expected_revision, event)

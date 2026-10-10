"""Notifications carry owned safe text and state, never executable provider output."""

from typing import Protocol, runtime_checkable

from agent_platform.domain.events import Notice


@runtime_checkable
class NotificationPort(Protocol):
    async def publish(self, notice: Notice) -> None: ...

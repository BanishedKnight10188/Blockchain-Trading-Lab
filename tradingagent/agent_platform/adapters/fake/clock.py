"""Event-driven deterministic time for offline runs."""

from datetime import datetime

from agent_platform.domain.common import utc_datetime


class FakeClock:
    def __init__(self, start: datetime):
        self._start = self._now = utc_datetime(start)

    def utcnow(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return (self._now - self._start).total_seconds()

    def advance_to(self, timestamp: datetime) -> None:
        newer = utc_datetime(timestamp)
        if newer < self._now:
            raise ValueError("offline clock cannot move backwards")
        self._now = newer

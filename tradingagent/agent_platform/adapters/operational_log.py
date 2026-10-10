"""Bounded UTF-8 JSONL rotation, never given external messages or exceptions."""

import asyncio
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from agent_platform.adapters.owned_io import owned_thread
from agent_platform.domain.operations import OperationalEvent
from agent_platform.ports.sessions import PersistenceUnavailable


class _SafeHandler(RotatingFileHandler):
    def handleError(self, record):
        raise PersistenceUnavailable("operational log unavailable") from None


class SafeOperationalLog:
    def __init__(self, path: Path, *, max_bytes: int = 1048576, backup_count: int = 3):
        if type(max_bytes) is not int or not 1024 <= max_bytes <= 1048576:
            raise ValueError("log size must be a 1024–1048576 integer")
        if type(backup_count) is not int or not 1 <= backup_count <= 3:
            raise ValueError("log backups must be a 1–3 integer")
        self.path = Path(path).resolve()
        self.max_bytes, self.backup_count = max_bytes, backup_count
        self._lock = asyncio.Lock()

    async def write(self, event: OperationalEvent):
        checked = OperationalEvent.model_validate_json(event.model_dump_json())
        body = checked.model_dump_json()
        if len(body.encode("utf-8")) + 1 > self.max_bytes:
            raise ValueError("event exceeds the configured log bound")

        def write():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handler = _SafeHandler(
                self.path,
                maxBytes=self.max_bytes,
                backupCount=self.backup_count,
                encoding="utf-8",
            )
            try:
                handler.emit(
                    logging.LogRecord("btc.operations", logging.INFO, "", 0, body, (), None)
                )
                handler.flush()
            finally:
                handler.close()

        async with self._lock:
            try:
                await owned_thread(write)
            except OSError:
                raise PersistenceUnavailable("operational log unavailable") from None

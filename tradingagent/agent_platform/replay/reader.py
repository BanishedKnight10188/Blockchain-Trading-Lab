"""Streaming typed JSONL with bounded lines and sanitized input errors."""

from collections.abc import Iterator
from pathlib import Path

from agent_platform.domain.market import MarketEvent


class ReplayInputError(ValueError):
    pass


def read_events(path: str | Path) -> Iterator[MarketEvent]:
    with Path(path).open("rb") as stream:
        line_number = 0
        while line := stream.readline(65537):
            line_number += 1
            if len(line) > 65536:
                raise ReplayInputError(f"invalid replay input at line {line_number}")
            if not line.strip():
                continue
            try:
                yield MarketEvent.model_validate_json(line.decode("utf-8"))
            except ValueError:
                raise ReplayInputError(f"invalid replay input at line {line_number}") from None

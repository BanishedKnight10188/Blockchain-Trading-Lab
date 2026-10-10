"""First history assessment is explanatory evidence, never an order."""

import json
from typing import Literal, Self

from pydantic import Field, model_validator

from .costs import ModelUsage
from .model_calls import ProviderMetadata
from .models import DomainModel, Identifier, Revision, UtcDateTime
from .session_market import HistoricalMarketData, SessionAnalysisTarget
from .sessions import TradingStyle


class InitialAnalysisRequest(DomainModel):
    request_id: Identifier
    session_id: Identifier
    style: TradingStyle
    style_revision: Revision
    history: HistoricalMarketData
    deadline: UtcDateTime
    prompt_version: Literal["initial-history-v1"] = "initial-history-v1"

    @model_validator(mode="after")
    def bounded_context(self) -> Self:
        if (
            self.deadline <= self.history.captured_at
            or len(json.dumps(self.context_data(), ensure_ascii=False).encode()) > 100000
        ):
            raise ValueError("first analysis context exceeds time or size bounds")
        return self

    def context_data(self) -> dict:
        return {
            "request_id": self.request_id,
            "session_id": self.session_id,
            "target": self.history.target.model_dump(mode="json"),
            "style": self.style.context.model_dump(mode="json"),
            "style_revision": self.style_revision,
            "source": self.history.source,
            "history_hash": self.history.content_hash,
            "captured_at": self.history.captured_at.isoformat(),
            "requested_start": self.history.requested_start.isoformat(),
            "requested_end_exclusive": self.history.requested_end.isoformat(),
            "candle_columns": [
                "open_time_utc",
                "open",
                "high",
                "low",
                "close",
                "base_volume",
                "quote_volume_usdt",
            ],
            "candles": [
                [
                    c.opened_at.isoformat(),
                    str(c.open),
                    str(c.high),
                    str(c.low),
                    str(c.close),
                    str(c.volume),
                    str(c.quote_volume) if c.quote_volume is not None else None,
                ]
                for c in self.history.candles
            ],
            "account_positions": "not_provided",
            "execution_enabled": False,
        }


class InitialAnalysisResult(DomainModel):
    request_id: Identifier
    history_hash: str = Field(pattern=r"^[0-9a-f]{64}$", strict=True)
    source: Literal["fake", "model"]
    model_id: Literal["anthropic/claude-haiku-5.5", "deepseek/deepseek-v4.1-flash"] = (
        "anthropic/claude-haiku-5.5"
    )
    trend: Literal["bullish", "bearish", "sideways", "uncertain"]
    summary: str = Field(min_length=1, max_length=4000)
    evidence: tuple[str, ...] = Field(min_length=1, max_length=16)
    risks: tuple[str, ...] = Field(min_length=1, max_length=16)
    watch_conditions: tuple[str, ...] = Field(min_length=1, max_length=16)
    usage: ModelUsage | None = None
    provider_metadata: ProviderMetadata | None = None

    @model_validator(mode="after")
    def bounded_strings(self) -> Self:
        if self.usage is not None and self.usage.request_id != self.request_id:
            raise ValueError("analysis usage belongs to another request")
        if any(
            not s.strip() or len(s) > 512
            for s in (*self.evidence, *self.risks, *self.watch_conditions)
        ):
            raise ValueError("initial analysis text exceeds bounds")
        return self


class InitialAnalysisRecord(DomainModel):
    session_id: Identifier
    request_id: Identifier
    style: TradingStyle
    style_revision: Revision
    target: SessionAnalysisTarget
    started_at: UtcDateTime
    completed_at: UtcDateTime | None = None
    status: Literal[
        "pending",
        "complete",
        "model_unconfigured",
        "history_unavailable",
        "model_failed",
        "discarded",
        "interrupted",
    ] = "pending"
    history: HistoricalMarketData | None = None
    result: InitialAnalysisResult | None = None

    @model_validator(mode="after")
    def coherent_record(self) -> Self:
        if self.history is not None and self.history.target != self.target:
            raise ValueError("history belongs to another target")
        if self.result is not None and (
            self.history is None
            or self.result.request_id != self.request_id
            or self.result.history_hash != self.history.content_hash
        ):
            raise ValueError("answer belongs to another history request")
        if self.status == "complete" and self.result is None:
            raise ValueError("complete analysis requires an answer")
        if self.status != "pending" and self.completed_at is None:
            raise ValueError("finished analysis requires completion time")
        if self.completed_at is not None and self.completed_at < self.started_at:
            raise ValueError("analysis chronology is reversed")
        return self

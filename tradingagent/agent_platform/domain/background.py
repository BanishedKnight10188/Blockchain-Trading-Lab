"""Four cached background layers, with immutable source and session evidence."""

import json
from datetime import timedelta
from hashlib import sha256
from typing import Literal

from pydantic import Field, StrictBool, model_validator

from .costs import ModelUsage
from .model_calls import ProviderMetadata
from .models import DomainModel, Identifier, Revision, StyleStrength, UtcDateTime
from .multiscale import BACKGROUND_WINDOWS, CandleWindow
from .routing import ModelPrice
from .session_market import FuturesSymbol


class BackgroundSettings(DomainModel):
    enabled: StrictBool = False
    model_id: str = Field(
        default="anthropic/claude-haiku-5.5", pattern=r"^[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+$"
    )
    providers: tuple[str, ...] = Field(default=(), max_length=8)
    price: ModelPrice | None = None
    refresh_seconds: int = Field(default=900, strict=True, ge=60, le=86400)

    @model_validator(mode="after")
    def configured(self):
        import re

        if any(re.fullmatch(r"[a-z0-9._-]{1,64}", p) is None for p in self.providers):
            raise ValueError("invalid background provider")
        if self.enabled and (not self.providers or self.price is None):
            raise ValueError("background model needs explicit verified price and providers")
        return self


class BackgroundRequest(DomainModel):
    request_id: Identifier
    session_id: Identifier
    symbol: FuturesSymbol
    style_revision: Revision
    style_strength: StyleStrength
    created_at: UtcDateTime
    deadline: UtcDateTime
    windows: tuple[CandleWindow, ...] = Field(min_length=4, max_length=4)
    prompt_version: Literal["four-layer-background-v1"] = "four-layer-background-v1"

    @model_validator(mode="after")
    def coherent(self):
        if not self.created_at < self.deadline <= self.created_at + timedelta(seconds=60):
            raise ValueError("invalid background deadline")
        if tuple((w.interval, w.requested_count) for w in self.windows) != BACKGROUND_WINDOWS:
            raise ValueError("four exact background windows required")
        if len({w.source for w in self.windows}) != 1 or any(
            w.symbol != self.symbol
            or not w.complete
            or not w.fresh
            or w.captured_at > self.created_at
            for w in self.windows
        ):
            raise ValueError("background history incomplete or mismatched")
        if len(json.dumps(self.context_data(), ensure_ascii=False).encode()) > 100000:
            raise ValueError("background input exceeds bounds")
        return self

    @property
    def source_hash(self):
        return sha256("".join(w.content_hash for w in self.windows).encode()).hexdigest()

    def context_data(self):
        return {
            "request_id": self.request_id,
            "session_id": self.session_id,
            "symbol": self.symbol,
            "style_strength": self.style_strength,
            "style_revision": self.style_revision,
            "created_at": self.created_at.isoformat(),
            "source_hash": self.source_hash,
            "columns": ["open_time_utc", "open", "high", "low", "close", "base_volume"],
            "windows": [
                w.evidence() | {"candles": w.compact(), "features": w.features()}
                for w in self.windows
            ],
        }


class BackgroundLayer(DomainModel):
    period: Literal["90d", "30d", "7d", "1d"]
    trend: Literal["bullish", "bearish", "sideways", "uncertain"]
    summary: str = Field(strict=True, min_length=1, max_length=240)
    risks: tuple[str, ...] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def bounded(self):
        if not self.summary.strip() or any(not r.strip() or len(r) > 120 for r in self.risks):
            raise ValueError("background text exceeds bounds")
        return self


class BackgroundAnswer(DomainModel):
    layers: tuple[BackgroundLayer, ...] = Field(min_length=4, max_length=4)

    @model_validator(mode="after")
    def coherent(self):
        if tuple(layer.period for layer in self.layers) != ("90d", "30d", "7d", "1d"):
            raise ValueError("background layer order mismatch")
        if len(self.model_dump_json().encode()) > 5000:
            raise ValueError("background output exceeds bounds")
        return self


class BackgroundResult(DomainModel):
    request: BackgroundRequest
    answer: BackgroundAnswer
    source: Literal["fake", "model"]
    model_id: str = Field(strict=True, min_length=1, max_length=120)
    generated_at: UtcDateTime
    expires_at: UtcDateTime
    usage: ModelUsage | None = None
    provider_metadata: ProviderMetadata | None = None

    @model_validator(mode="after")
    def coherent(self):
        if not self.request.created_at <= self.generated_at < self.request.deadline or not (
            self.generated_at < self.expires_at <= self.generated_at + timedelta(days=1)
        ):
            raise ValueError("invalid background chronology")
        if self.source == "model" and (
            self.usage is None
            or self.provider_metadata is None
            or self.usage.request_id != self.request.request_id
            or self.usage.route_id != "background:" + self.request.request_id
            or self.usage.model_version != self.model_id
            or self.usage.billing_status != "confirmed"
            or self.request.windows[0].source != "binance_futures_public"
        ):
            raise ValueError("real background requires confirmed model and market evidence")
        return self

    def context_data(self):
        return {
            "request_id": self.request.request_id,
            "source_hash": self.request.source_hash,
            "summary_hash": sha256(self.answer.model_dump_json().encode()).hexdigest(),
            "source": self.source,
            "model_id": self.model_id,
            "generated_at": self.generated_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "history": [w.evidence() for w in self.request.windows],
            "layers": [
                layer.model_dump(mode="json", exclude={"schema_version"})
                for layer in self.answer.layers
            ],
        }


class MultiScaleSnapshot(DomainModel):
    background: BackgroundResult
    short: CandleWindow
    fast: CandleWindow
    captured_at: UtcDateTime

    @model_validator(mode="after")
    def coherent(self):
        r = self.background.request
        if (
            self.short.interval,
            self.short.requested_count,
            self.fast.interval,
            self.fast.requested_count,
        ) != ("3m", 20, "1s", 60):
            raise ValueError("20 three-minute and 60 second bars required")
        if any(
            w.symbol != r.symbol
            or w.source != r.windows[0].source
            or not w.complete
            or not w.fresh
            or w.captured_at > self.captured_at
            for w in (self.short, self.fast)
        ):
            raise ValueError("short context incomplete or mismatched")
        if not self.background.generated_at <= self.captured_at < self.background.expires_at:
            raise ValueError("background expired")
        return self

    def ready_for(self, session_id, style_revision, symbol, at):
        r = self.background.request
        return (
            (r.session_id, r.style_revision, r.symbol) == (session_id, style_revision, symbol)
            and self.captured_at <= at < self.background.expires_at
            and at - self.captured_at < timedelta(seconds=2)
            and self.fast.model_copy(update={"captured_at": at}).fresh
            and self.short.model_copy(update={"captured_at": at}).fresh
        )

    def context_data(self):
        return {
            "version": "jev-six-layer-v1",
            "captured_at": self.captured_at.isoformat(),
            "columns": ["open_time_utc", "open", "high", "low", "close", "base_volume"],
            "background": self.background.context_data(),
            "short_3m": self.short.evidence() | {"candles": self.short.compact()},
            "fast_1s": self.fast.evidence() | {"candles": self.fast.compact()},
        }

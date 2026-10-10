"""Explicit demo quotes and scripted decisions; never represented as real JEV."""

from datetime import timedelta
from decimal import Decimal

from agent_platform.domain.costs import ModelUsage
from agent_platform.domain.decision_models import (
    DecisionAnswer,
    DecisionModelResponse,
    DecisionProbability,
)
from agent_platform.domain.market import MarketSnapshot
from agent_platform.domain.model_calls import ProviderMetadata


class DemoPaperMarket:
    def __init__(self, clock):
        self.clock = clock
        self.price = Decimal("60000")
        self.age_seconds = 0

    async def sample(self):
        now = self.clock.utcnow()
        observed = now - timedelta(seconds=self.age_seconds)
        return MarketSnapshot(
            symbol="BTCUSDT",
            as_of=max(now, observed),
            status="ready",
            latest_received_at=observed,
            latest_quote_at=observed,
            book_as_of=observed,
            book={
                "symbol": "BTCUSDT",
                "bid": self.price,
                "ask": self.price + 1,
                "bid_quantity": "1",
                "ask_quantity": "1",
            },
        )


class MockPaperDecisionModel:
    def __init__(self, clock, *, choices=("BUY", "SELL", "WAIT")):
        if not choices or any(c not in ("BUY", "SELL", "WAIT") for c in choices):
            raise ValueError("invalid scripted paper decisions")
        self.clock, self.choices = clock, choices
        self.calls = 0
        self.confidence = "0.9"

    async def decide(self, request):
        selected = self.choices[self.calls % len(self.choices)]
        self.calls += 1
        return DecisionModelResponse(
            request_id=request.request_id,
            question_set_version=request.question_set_version,
            answers=(
                DecisionAnswer(
                    question_id="action",
                    kind="choice",
                    choice=selected,
                    confidence=self.confidence,
                    probabilities=tuple(
                        DecisionProbability(
                            key=key, probability="0.8" if key == selected else "0.1"
                        )
                        for key in ("BUY", "SELL", "WAIT")
                    ),
                ),
            ),
            usage=ModelUsage(
                request_id=request.request_id,
                route_id=request.route.route_id,
                model_version="typesafe/jev-1.13",
                input_tokens=0,
                output_tokens=0,
                estimated_cost_usd="0",
                actual_cost_usd="0",
                billing_status="confirmed",
                recorded_at=self.clock.utcnow(),
            ),
            provider_metadata=ProviderMetadata(
                request_id=request.request_id, model_id="typesafe/jev-1.13", provider="Offline Mock"
            ),
        )

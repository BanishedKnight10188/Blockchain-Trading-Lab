"""Opt-in fixtures for wiring checks; never presented as live market or real JEV."""

from datetime import timedelta
from decimal import Decimal

from agent_platform.adapters.fake.paper_trading import MockPaperDecisionModel
from agent_platform.adapters.fake.session_analysis import FakeHistoricalMarket
from agent_platform.domain.decision_models import DecisionProbability
from agent_platform.domain.futures_market import (
    FuturesContractRules,
    FuturesFundingWindow,
    FuturesMarketSnapshot,
)
from agent_platform.domain.futures_values import FuturesQuote
from agent_platform.domain.session_market import PerpetualContract


class OfflineFuturesMarket(FakeHistoricalMarket):
    def __init__(self, clock):
        super().__init__(clock)
        self.price = Decimal("2000")
        self.events = ()
        self.next_due = clock.utcnow() + timedelta(hours=8)

    async def snapshot(self, symbol):
        now = self.clock.utcnow()
        while self.next_due <= now:
            self.next_due += timedelta(hours=8)
        return FuturesMarketSnapshot(
            quote=FuturesQuote(
                symbol=symbol,
                source="offline_replay",
                bid=self.price,
                ask=self.price,
                mark=self.price,
                book_at=now,
                mark_at=now,
                received_at=now,
            ),
            index_price=self.price,
            displayed_funding_rate="0",
            next_funding_at=self.next_due,
            bid_quantity="100",
            ask_quantity="100",
        )

    async def rules(self, symbol):
        return FuturesContractRules(
            contract=PerpetualContract(symbol=symbol, base_asset=symbol.removesuffix("USDT")),
            source="offline_replay",
            captured_at=self.clock.utcnow(),
            price_tick="0.01",
            min_price="0.01",
            max_price="1000000000",
            lot_step="0.001",
            lot_min_qty="0.001",
            lot_max_qty="1000000",
            market_step="0.001",
            market_min_qty="0.001",
            market_max_qty="1000000",
            min_notional="5",
            percent_down="0.9",
            percent_up="1.1",
            market_take_bound="0.1",
        )

    async def settlements(self, symbol, *, after, through=None):
        through = through or self.clock.utcnow()
        return FuturesFundingWindow(
            symbol=symbol,
            source="offline_replay",
            requested_after=after,
            requested_through=through,
            captured_at=self.clock.utcnow(),
            events=tuple(
                e for e in self.events if e.symbol == symbol and after < e.settled_at <= through
            ),
        )


class OfflineFuturesDecisionModel(MockPaperDecisionModel):
    def __init__(self, clock, *, choices=("WAIT",)):
        if not choices or any(
            c not in ("OPEN_LONG", "OPEN_SHORT", "REDUCE", "WAIT") for c in choices
        ):
            raise ValueError("invalid scripted futures decisions")
        self.clock, self.choices, self.calls, self.confidence = clock, choices, 0, "0.9"

    async def decide(self, request):
        # Reuse the owned zero-fee metadata, replacing the fixed Spot distribution.
        selected = self.choices[self.calls % len(self.choices)]
        old = self.choices
        self.choices = ("WAIT",)
        try:
            response = await super().decide(request)
        finally:
            self.choices = old
        answer = response.answers[0].model_dump()
        answer |= {
            "question_id": request.questions[0].question_id,
            "choice": selected,
            "probabilities": tuple(
                DecisionProbability(key=c.key, probability="1" if c.key == selected else "0")
                for c in request.questions[0].criteria
            ),
        }
        return type(response).model_validate(response.model_dump() | {"answers": (answer,)})

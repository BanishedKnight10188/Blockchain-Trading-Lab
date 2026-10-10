"""A finite offline stream; current snapshots expose only events already delivered."""

from collections.abc import AsyncIterator, Iterable

from agent_platform.domain.market import BookTicker, Candle, MarketEvent, MarketSnapshot, TradeTick


class FakeMarket:
    def __init__(self, events: Iterable[MarketEvent] = ()):
        self._events = events
        self._latest: dict[str, MarketSnapshot] = {}

    def observe(self, event: MarketEvent) -> MarketSnapshot:
        checked = MarketEvent.model_validate_json(event.model_dump_json())
        timestamp = max(checked.occurred_at, checked.received_at)
        previous = self._latest.get(checked.symbol)
        if previous is not None and timestamp < previous.as_of:
            raise ValueError("offline observations must be chronological")
        trade = previous.latest_trade if previous else None
        book = previous.book if previous else None
        candles = previous.candles if previous else ()
        quote_received_at = previous.latest_received_at if previous else checked.received_at
        quote_at = previous.latest_quote_at if previous else None
        payload = checked.payload
        if isinstance(payload, TradeTick):
            trade = payload
            book = None
            quote_received_at = checked.received_at
            quote_at = checked.occurred_at
        elif isinstance(payload, BookTicker):
            book = payload
            trade = None
            quote_received_at = checked.received_at
            quote_at = checked.occurred_at
        elif isinstance(payload, Candle) and payload.is_closed:
            candles = (*candles, payload)[-120:]
        status = "ready" if trade is not None or book is not None else "warming"
        if status == "ready" and (timestamp - quote_at).total_seconds() > 5:
            status = "stale"
        result = MarketSnapshot(
            symbol=checked.symbol,
            as_of=timestamp,
            status=status,
            latest_received_at=quote_received_at,
            latest_quote_at=quote_at,
            book_as_of=quote_at if book is not None else None,
            latest_trade=trade,
            book=book,
            candles=candles,
        )
        self._latest[checked.symbol] = result
        return result

    def stream(self, symbols: tuple[str, ...]) -> AsyncIterator[MarketEvent]:
        async def deliver():
            for event in self._events:
                if event.symbol in symbols:
                    self.observe(event)
                    yield event

        return deliver()

    async def latest(self, symbol: str) -> MarketSnapshot:
        if symbol not in self._latest:
            raise ValueError("offline market has no delivered quote for this symbol")
        return self._latest[symbol]

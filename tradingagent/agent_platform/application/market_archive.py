"""Public-only projection, separate from immutable decision/review evidence."""

from datetime import timedelta

from agent_platform.domain.market import MarketDataStatus
from agent_platform.domain.market_archive import MarketArchiveRecord, QuoteSample
from agent_platform.domain.overview import OverviewFrame


def archive_records(frame: OverviewFrame) -> tuple[MarketArchiveRecord, ...]:
    frame = OverviewFrame.model_validate_json(frame.model_dump_json())
    if frame.market is None or frame.mode == "disabled" or frame.market_source == "none":
        return ()
    market = frame.market
    quote_at = market.latest_quote_at if market.latest_trade is not None else None
    book_at = market.book_as_of if market.book is not None else None
    status = market.status
    if status != MarketDataStatus.GAP and any(
        at is not None and frame.captured_at - at > timedelta(seconds=5)
        for at in (quote_at, book_at)
    ):
        status = MarketDataStatus.STALE
    scope = dict(mode=frame.mode, source=frame.market_source, collected_at=frame.captured_at)
    sample = MarketArchiveRecord(
        **scope,
        kind="raw",
        event_at=frame.captured_at,
        payload=QuoteSample(
            price=market.latest_trade.price if quote_at is not None else None,
            quote_at=quote_at,
            book_at=book_at,
            bid=market.book.bid if book_at is not None else None,
            ask=market.book.ask if book_at is not None else None,
            status=status,
        ),
    )
    # The current buffer holds a bounded window; archives never recreate missing minutes.
    minutes = tuple(
        MarketArchiveRecord(**scope, kind="minute", event_at=candle.closed_at, payload=candle)
        for candle in market.candles[-120:]
        if (candle.closed_at - candle.opened_at).total_seconds() == 60
    )
    return (sample, *minutes)

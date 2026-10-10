"""Strict current Spot trade/bookTicker/1m kline mappings, no network access."""

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from agent_platform.domain.common import required_identifier, utc_datetime
from agent_platform.domain.market import BookTicker, Candle, MarketEvent, TradeTick


class NormalizationError(ValueError):
    pass


def _integer(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("expected nonnegative integer")
    return value


def _timestamp(value: Any) -> datetime:
    return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=_integer(value))


def _amount(value: Any) -> str:
    if (
        not isinstance(value, str)
        or len(value) > 128
        or re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value) is None
    ):
        raise ValueError("wire amounts must be bounded fixed-point decimal strings")
    return value


def _symbol(value: Any) -> str:
    if required_identifier(value) != "BTCUSDT":
        raise ValueError("unsupported spot symbol")
    return value


def _candle(data: dict, closed: bool) -> Candle:
    result = Candle(
        symbol=_symbol(data["s"]),
        opened_at=_timestamp(data["t"]),
        closed_at=_timestamp(data["T"]) + timedelta(milliseconds=1),
        open=_amount(data["o"]),
        high=_amount(data["h"]),
        low=_amount(data["l"]),
        close=_amount(data["c"]),
        volume=_amount(data["v"]),
        quote_volume=_amount(data["q"]),
        is_closed=closed,
    )
    if result.closed_at - result.opened_at != timedelta(minutes=1):
        raise ValueError("unexpected minute kline duration")
    return result


def normalize_stream(raw: dict, received_at: datetime) -> MarketEvent:
    try:
        received = utc_datetime(received_at)
        data = raw["data"] if "stream" in raw else raw
        symbol = _symbol(data["s"])
        if "stream" in raw and raw["stream"].split("@", 1)[0] != symbol.lower():
            raise ValueError("combined stream has another symbol")
        quality = "exchange"
        sequence = None
        if data.get("e") == "trade":
            payload = TradeTick(
                symbol=symbol,
                trade_id=str(_integer(data["t"])),
                price=_amount(data["p"]),
                quantity=_amount(data["q"]),
            )
            _timestamp(data["E"])
            occurred = _timestamp(data["T"])
            identity = f"binance:trade:{symbol}:{payload.trade_id}"
            sequence = _integer(data["t"])
            stream_name = symbol.lower() + "@trade"
        elif data.get("e") == "kline":
            detail = data["k"]
            if detail["i"] != "1m" or detail["s"] != symbol or type(detail["x"]) is not bool:
                raise ValueError("unsupported or mismatched kline")
            payload = _candle(detail, detail["x"])
            occurred = _timestamp(data["E"])
            if payload.is_closed and payload.closed_at - timedelta(milliseconds=1) > max(
                occurred, received
            ):
                raise ValueError("closed kline is ahead of its observation time")
            identity = f"binance:kline:{symbol}:{detail['t']}:{data['E']}:{detail['x']}"
            stream_name = symbol.lower() + "@kline_1m"
        elif "u" in data and "e" not in data:
            payload = BookTicker(
                symbol=symbol,
                bid=_amount(data["b"]),
                ask=_amount(data["a"]),
                bid_quantity=_amount(data["B"]),
                ask_quantity=_amount(data["A"]),
            )
            occurred, quality = received, "received"
            identity = f"binance:book:{symbol}:{_integer(data['u'])}"
            sequence = _integer(data["u"])
            stream_name = symbol.lower() + "@bookTicker"
        else:
            raise ValueError("unsupported public market event")
        if "stream" in raw and raw["stream"] != stream_name:
            raise ValueError("combined stream type does not match its payload")
        return MarketEvent(
            event_id=identity,
            symbol=symbol,
            source="binance_direct",
            occurred_at=occurred,
            received_at=received,
            time_quality=quality,
            stream_sequence=sequence,
            payload=payload,
        )
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
        raise NormalizationError("invalid Binance Spot market payload") from None


def normalize_kline_row(
    row: list, symbol: str, received_at: datetime, *, completed_before: datetime | None = None
) -> MarketEvent:
    try:
        received, symbol = utc_datetime(received_at), _symbol(symbol)
        cutoff = (
            received if completed_before is None else min(received, utc_datetime(completed_before))
        )
        if not isinstance(row, list) or len(row) != 12:
            raise ValueError("unsupported kline row")
        closed_at = _timestamp(row[6]) + timedelta(milliseconds=1)
        payload = _candle(
            {
                "s": symbol,
                "t": row[0],
                "T": row[6],
                "o": row[1],
                "h": row[2],
                "l": row[3],
                "c": row[4],
                "v": row[5],
                "q": row[7],
            },
            closed_at <= cutoff,
        )
        return MarketEvent(
            event_id=f"binance:rest-kline:{symbol}:{row[0]}:{received.isoformat()}:{payload.is_closed}",
            symbol=symbol,
            source="binance_direct",
            occurred_at=received,
            received_at=received,
            time_quality="received",
            payload=payload,
        )
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
        raise NormalizationError("invalid Binance Spot kline row") from None

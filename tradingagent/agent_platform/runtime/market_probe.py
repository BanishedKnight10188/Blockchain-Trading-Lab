"""Bounded diagnostic capture; no session, strategy, account or model requests."""

import asyncio

from agent_platform.application.features import FeatureService
from agent_platform.ports.market import MarketDataPort


async def capture_market(port: MarketDataPort, *, seconds: int) -> dict:
    if type(seconds) is not int or not 1 <= seconds <= 60:
        raise ValueError("market probe duration must be 1..60 seconds")
    count, captured = 0, None
    stream = port.stream(("BTCUSDT",))
    try:
        async with asyncio.timeout(seconds):
            async for _ in stream:
                count += 1
                captured = await port.latest("BTCUSDT")
    except TimeoutError:
        pass
    finally:
        await stream.aclose()
    features = FeatureService().compute(captured) if captured is not None else None
    return {
        "schema_version": 1,
        "mode": "public_market_probe",
        "status": "captured" if captured is not None else "no_data",
        "seconds": seconds,
        "events_received": count,
        "snapshot": captured.model_dump(mode="json") if captured else None,
        "features": features.model_dump(mode="json") if features else None,
        "account_calls": 0,
        "real_orders": 0,
        "model_calls": 0,
    }

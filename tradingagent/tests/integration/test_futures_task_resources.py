"""A futures task must not consume an unrelated BTC Spot stream."""

import pytest

from agent_platform.adapters.fake.clock import FakeClock
from agent_platform.bootstrap import build_application_services
from agent_platform.config import RuntimeConfig
from tests.domain.test_futures_paper import NOW


@pytest.mark.asyncio
async def test_futures_task_does_not_create_spot_stream(tmp_path, monkeypatch):
    from agent_platform.adapters.binance_direct import market_stream

    def unexpected_spot_stream(*args, **kwargs):
        pytest.fail("a futures task started an unrelated BTC Spot stream")

    monkeypatch.setattr(market_stream, "BinanceMarketStream", unexpected_spot_stream)
    config = RuntimeConfig(paper=True, paper_mock=True, live_public=True).model_copy(
        update={"futures_task_only": True}
    )
    async with build_application_services(
        tmp_path / "futures.sqlite3", config, clock=FakeClock(NOW)
    ) as services:
        assert services.futures_trading.ready

"""Archive opt-in follows public mode and never creates history in disabled mode."""

import asyncio

import pytest

from agent_platform.adapters.fake.market import FakeMarket
from agent_platform.bootstrap import build_application_services
from agent_platform.config import RuntimeConfig
from tests.runtime.test_read_only import observation


@pytest.mark.asyncio
async def test_disabled_has_safe_runtime_health_and_no_auxiliary_database(tmp_path):
    path = tmp_path / "disabled.sqlite3"
    async with build_application_services(path) as services:
        data = await services.system.current()
        assert data["runtime"]["status"] == "running"
        assert data["runtime"]["worker_count"] == 4
        assert data["archive"]["enabled"] is False
        assert data["archive"]["observations_written"] == 0
        assert not path.with_suffix(".market.sqlite3").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_public_archive_is_separate_and_explicit_disable_is_respected(
    tmp_path, monkeypatch, enabled
):
    import agent_platform.adapters.binance_direct.futures_public as futures
    import agent_platform.adapters.binance_direct.market_stream as streams
    import agent_platform.adapters.binance_direct.public_rest as rest

    closed = []
    futures_closed = []

    class FakeRest:
        def __init__(self, clock):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            closed.append(True)

    monkeypatch.setattr(rest, "PublicRestClient", FakeRest)

    class FakeFutures:
        def __init__(self, clock, *, proxy_url=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            futures_closed.append(True)

        async def catalog(self):
            raise AssertionError("no futures session was selected")

    monkeypatch.setattr(futures, "FuturesPublicClient", FakeFutures)
    monkeypatch.setattr(
        streams,
        "BinanceMarketStream",
        lambda *args: FakeMarket((observation(source="binance_direct"),)),
    )
    path = tmp_path / "public.sqlite3"
    async with build_application_services(
        path, RuntimeConfig(live_public=True, market_archive=enabled)
    ) as services:
        assert services.system.archive is not None if enabled else services.system.archive is None
        if enabled:
            async with asyncio.timeout(3):
                while services.system.archive.observations_written == 0:  # noqa: ASYNC110
                    await asyncio.sleep(0.01)
        status = await services.system.current()
        assert status["archive"]["enabled"] is enabled
        assert status["runtime"]["worker_count"] == (6 if enabled else 5)
        assert services.initial_analysis is not None
        assert "public.sqlite3" not in str(status)
    assert closed == [True]
    assert futures_closed == [True]
    assert path.with_suffix(".market.sqlite3").exists() is enabled
    assert (await services.runtime.health()).status == "stopped"

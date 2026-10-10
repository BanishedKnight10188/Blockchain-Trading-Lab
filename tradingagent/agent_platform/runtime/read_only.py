"""Process-owned read-only workers, independent of browser and model requests."""

import asyncio

from agent_platform.application.account_sync import AccountSyncService
from agent_platform.application.features import FeatureService
from agent_platform.domain.common import live_account_ref
from agent_platform.domain.overview import OverviewFrame
from agent_platform.ports.clock import ClockPort
from agent_platform.ports.market import MarketDataPort
from agent_platform.ports.sessions import PersistenceUnavailable
from agent_platform.runtime.latest import LatestOverview


class ReadOnlyRuntime:
    def __init__(
        self,
        cache: LatestOverview,
        clock: ClockPort,
        *,
        market: MarketDataPort | None = None,
        account_sync: AccountSyncService | None = None,
        account_ref: str | None = None,
    ):
        if (account_sync is None) != (account_ref is None):
            raise ValueError("account worker requires an explicit account scope")
        self.cache, self.clock, self.market = cache, clock, market
        self.account_sync = account_sync
        self.account_ref = live_account_ref(account_ref) if account_ref is not None else None
        self.features = FeatureService()
        self._tasks: list[asyncio.Task] = []
        self._scope = None
        self._sync = None
        self._market_error = self._account_error = None
        self._invalid_market = False
        self._account_wake = asyncio.Event()
        self._lifecycle = asyncio.Lock()
        self._requires_resync = True

    @property
    def running(self) -> bool:
        return bool(self._tasks)

    @property
    def worker_count(self) -> int:
        return len(self._tasks)

    @property
    def last_failure(self):
        return (
            self._account_error or getattr(self._sync, "failure_reason", None) or self._market_error
        )

    @property
    def reconcile_pending_count(self) -> int:
        return int(self._account_wake.is_set())

    def request_sync(self) -> None:
        if self.account_sync is None:
            raise RuntimeError("reconciliation requires configured read-only polling")
        self._account_wake.set()

    async def start(self) -> None:
        async with self._lifecycle:
            await self._start()

    async def _start(self) -> None:
        if self.running:
            raise RuntimeError("read-only runtime already started")
        initial = await self.cache.latest()
        if (self.market is None) != (initial.market_source == "none") or (
            self.account_sync is None
        ) != (initial.account_source == "none"):
            raise ValueError("runtime providers do not match declared sources")
        self._scope = initial.mode, initial.market_source, initial.account_source
        self._sync = None
        self._requires_resync = True
        self._market_error = self._account_error = None
        self._invalid_market = False
        await self.cache.publish(
            OverviewFrame(
                mode=initial.mode,
                market_source=initial.market_source,
                account_source=initial.account_source,
                captured_at=self.clock.utcnow(),
            )
        )
        self._account_wake.clear()
        if self.market is not None:
            self._tasks.append(asyncio.create_task(self._consume(), name="btc-market-reader"))
        if self.account_sync is not None:
            self._tasks.append(asyncio.create_task(self._accounts(), name="btc-account-sync"))
        self._tasks.append(asyncio.create_task(self._sample(), name="btc-overview-sampler"))
        await asyncio.sleep(0)

    async def stop(self) -> None:
        async with self._lifecycle:
            await self._stop()

    async def _stop(self) -> None:
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _consume(self) -> None:
        iterator = None
        try:
            iterator = self.market.stream(("BTCUSDT",))
            async for event in iterator:
                if event.source != self._scope[1] or event.symbol != "BTCUSDT":
                    self._market_error, self._invalid_market = "invalid_data", True
                    return
            if self._scope[0] == "live_read_only":
                self._market_error = "transport"
        except Exception:
            self._market_error = "transport"
        finally:
            close = getattr(iterator, "aclose", None)
            if close is not None:
                await close()

    async def _accounts(self) -> None:
        while True:
            self._account_wake.clear()
            try:
                candidate = await self.account_sync.sync(self.account_ref, "BTCUSDT")
                if not candidate.cached or not self._requires_resync:
                    self._sync = candidate
                    self._requires_resync = False
                self._account_error = None
                delay = max(
                    1 if candidate.cached else 15,
                    (
                        candidate.next_attempt_at
                        - (self.clock.utcnow() if candidate.cached else candidate.attempted_at)
                    ).total_seconds(),
                )
            except PersistenceUnavailable:
                self._account_error = "persistence"
                return  # No repeated write attempts into failed audit storage.
            except Exception:
                self._account_error = "invalid_data"
                return
            # AccountSyncService independently preserves its monotonic 15s / RetryAfter gate.
            # A private hint can wake this loop, never authorize an earlier network request.
            try:
                await asyncio.wait_for(self._account_wake.wait(), timeout=delay)
            except TimeoutError:
                pass

    async def sample_once(self) -> None:
        if self._scope is None:
            initial = await self.cache.latest()
            self._scope = initial.mode, initial.market_source, initial.account_source
        snapshot = features = None
        error = self._market_error
        if self.market is not None and not self._invalid_market:
            try:
                snapshot = await self.market.latest("BTCUSDT")
                features = self.features.compute(snapshot)
            except ValueError:
                snapshot = features = None
                error = "invalid_data" if self._market_error else "not_connected"
            except Exception:
                snapshot = features = None
                error = "transport"
        mode, market_source, account_source = self._scope
        now = self.clock.utcnow()
        # Validate each provider separately so one bad clock/scope cannot erase the other.
        try:
            OverviewFrame(
                mode=mode,
                market_source=market_source,
                account_source=account_source,
                captured_at=now,
                market=snapshot,
                features=features,
            )
        except ValueError:
            snapshot = features = None
            error = "invalid_data"
        sync, account_error = self._sync, self._account_error
        try:
            OverviewFrame(
                mode=mode,
                market_source=market_source,
                account_source=account_source,
                captured_at=now,
                sync=sync,
            )
        except ValueError:
            sync, account_error = None, "invalid_data"
        frame = OverviewFrame(
            mode=mode,
            market_source=market_source,
            account_source=account_source,
            captured_at=now,
            market=snapshot,
            features=features,
            sync=sync,
            market_error=error,
            account_error=account_error,
        )
        await self.cache.publish(frame)

    async def _sample(self) -> None:
        while True:
            await self.sample_once()
            await asyncio.sleep(1)

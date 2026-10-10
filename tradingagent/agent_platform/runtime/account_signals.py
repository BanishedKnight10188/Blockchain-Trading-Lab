"""Optional private stream hints: bounded, isolated, never authoritative account facts."""

import asyncio
from collections.abc import Awaitable, Callable

from agent_platform.domain.common import live_account_ref
from agent_platform.domain.sync import AccountSignal
from agent_platform.ports.account import AccountReadUnavailable, AccountSignalPort
from agent_platform.ports.clock import ClockPort


class AccountSignalRuntime:
    def __init__(
        self,
        source: AccountSignalPort,
        request_sync: Callable[[], None],
        clock: ClockPort,
        *,
        account_ref: str,
        wait: Callable[[float], Awaitable[None]] | None = None,
    ):
        self.source, self.request_sync, self.clock = source, request_sync, clock
        self.account_ref = live_account_ref(account_ref)
        self._wait = wait or asyncio.sleep
        self._pending = asyncio.Event()
        self._tasks = []
        self._lifecycle = asyncio.Lock()
        self._last_event = None
        self.received_count = self.reconnect_count = 0
        self.last_failure = None

    @property
    def running(self):
        return any(not task.done() for task in self._tasks)

    @property
    def pending_count(self):
        return int(self._pending.is_set())

    async def start(self):
        async with self._lifecycle:
            if self._tasks:
                raise RuntimeError("account hint worker already started")
            self._pending.clear()
            self._last_event = None
            self._tasks = [
                asyncio.create_task(self._consume(), name="btc-account-hints"),
                asyncio.create_task(self._dispatch(), name="btc-account-reconcile-hint"),
            ]
            await asyncio.sleep(0)

    async def stop(self):
        async with self._lifecycle:
            tasks, self._tasks = self._tasks, []
            for task in tasks:
                if not task.done() and not task.cancelling():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._pending.clear()

    async def _consume(self):
        backoff = 1
        while True:
            iterator = None
            delay = backoff
            self.last_failure = None
            try:
                iterator = self.source.updates()
                async for candidate in iterator:
                    signal = AccountSignal.model_validate_json(candidate.model_dump_json())
                    if (
                        signal.account_ref != self.account_ref
                        or signal.received_at > self.clock.utcnow()
                    ):
                        self.last_failure = "invalid_data"
                        return
                    backoff = 1
                    if signal.event_id == self._last_event:
                        continue
                    self._last_event = signal.event_id
                    self.received_count += 1
                    self._pending.set()
                self.last_failure = "transport"
            except AccountReadUnavailable as error:
                self.last_failure = error.reason.value
                if error.reason in ("authentication", "credentials", "invalid_data"):
                    return
                delay = max(delay, error.retry_after_seconds)
            except Exception:
                self.last_failure = "transport"
            finally:
                close = getattr(iterator, "aclose", None)
                if close is not None:
                    await close()
            self.reconnect_count += 1
            await self._wait(delay)
            backoff = min(60, backoff * 2)

    async def _dispatch(self):
        next_dispatch = 0.0
        while True:
            await self._pending.wait()
            delay = next_dispatch - self.clock.monotonic()
            if delay > 0:
                await self._wait(delay)
            self._pending.clear()
            try:
                self.request_sync()
            except Exception:
                self.last_failure = "reconcile_unavailable"
                return
            next_dispatch = self.clock.monotonic() + 15

"""Read-only owned account data, with no execution or funds-transfer methods."""

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from agent_platform.domain.account import AccountSnapshot, ObservedOrder, TradeBatch, TradeCursor
from agent_platform.domain.sync import AccountSignal, SyncFailureReason


class AccountReadUnavailable(RuntimeError):
    def __init__(self, reason: SyncFailureReason | str, *, retry_after_seconds: int = 0):
        self.reason = SyncFailureReason(reason)
        if type(retry_after_seconds) is not int or not 0 <= retry_after_seconds <= 999999999:
            raise ValueError("invalid account retry delay")
        self.retry_after_seconds = retry_after_seconds
        super().__init__("read-only account unavailable: " + self.reason.value)


@runtime_checkable
class AccountPort(Protocol):
    async def snapshot(self, account_ref: str) -> AccountSnapshot: ...

    async def orders(self, account_ref: str, symbol: str) -> tuple[ObservedOrder, ...]: ...

    async def trades(self, account_ref: str, symbol: str, cursor: TradeCursor) -> TradeBatch: ...


class AccountSignalPort(Protocol):
    def updates(self) -> AsyncIterator[AccountSignal]: ...

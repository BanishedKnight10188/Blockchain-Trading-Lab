"""Configured one-account Spot AccountPort; no execution method exists."""

from contextlib import contextmanager

from agent_platform.adapters.binance_direct.account_mapping import (
    AccountMappingError,
    map_account,
    map_order,
)
from agent_platform.adapters.binance_direct.read_client import (
    AuthenticationRejected,
    CredentialsUnavailable,
    ReadOnlyClient,
    ReadOnlyError,
    ReadRateLimited,
)
from agent_platform.adapters.binance_direct.trade_history import TradeHistory, TradeHistoryError
from agent_platform.domain.account import AccountSnapshot, ObservedOrder, TradeBatch, TradeCursor
from agent_platform.domain.common import live_account_ref
from agent_platform.ports.account import AccountReadUnavailable
from agent_platform.ports.clock import ClockPort


@contextmanager
def _port_errors():
    try:
        yield
    except CredentialsUnavailable:
        raise AccountReadUnavailable("credentials") from None
    except AuthenticationRejected:
        raise AccountReadUnavailable("authentication") from None
    except ReadRateLimited as error:
        raise AccountReadUnavailable(
            "rate_limit", retry_after_seconds=error.retry_after_seconds
        ) from None
    except (AccountMappingError, TradeHistoryError):
        raise AccountReadUnavailable("invalid_data") from None
    except ReadOnlyError:
        raise AccountReadUnavailable("transport") from None


class BinanceAccount:
    def __init__(self, client: ReadOnlyClient, clock: ClockPort, account_ref: str):
        self.client, self.clock, self.account_ref = client, clock, live_account_ref(account_ref)
        self.history = TradeHistory(client, clock)

    def _scope(self, account_ref: str, symbol: str = "BTCUSDT") -> None:
        if account_ref != self.account_ref or symbol != "BTCUSDT":
            raise ValueError("unsupported configured Binance account scope")

    async def snapshot(self, account_ref: str) -> AccountSnapshot:
        self._scope(account_ref)
        with _port_errors():
            raw = await self.client.request("GET", "/api/v3/account")
            return map_account(raw, self.account_ref, self.clock.utcnow())

    async def orders(self, account_ref: str, symbol: str) -> tuple[ObservedOrder, ...]:
        self._scope(account_ref, symbol)
        with _port_errors():
            raw = await self.client.request("GET", "/api/v3/openOrders", {"symbol": symbol})
            if type(raw) is not list or len(raw) > 2000:
                raise AccountMappingError("invalid open order collection")
            orders = tuple(map_order(row, self.account_ref) for row in raw)
            if len({order.order_id for order in orders}) != len(orders) or any(
                order.status.terminal for order in orders
            ):
                raise AccountMappingError("invalid open order set")
            return orders

    async def trades(self, account_ref: str, symbol: str, cursor: TradeCursor) -> TradeBatch:
        self._scope(account_ref, symbol)
        with _port_errors():
            return await self.history.read(self.account_ref, symbol, cursor)

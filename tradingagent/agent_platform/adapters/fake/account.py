"""Read-only fixed account facts for offline contract testing and replay."""

from agent_platform.domain.account import (
    AccountSnapshot,
    ObservedOrder,
    ObservedTrade,
    TradeBatch,
    TradeCursor,
)


class FakeAccount:
    def __init__(
        self,
        account: AccountSnapshot,
        *,
        orders: tuple[ObservedOrder, ...] = (),
        trades: tuple[ObservedTrade, ...] = (),
    ):
        self.account = account
        self.observed_orders = tuple(orders)
        self.observed_trades = tuple(trades)
        if any(
            item.account_ref != account.account_ref or item.market_type != account.market_type
            for item in (*self.observed_orders, *self.observed_trades)
        ):
            raise ValueError("offline fixture contains another account scope")

    def _scope(self, account_ref: str) -> None:
        if account_ref != self.account.account_ref:
            raise ValueError("offline provider account scope mismatch")

    async def snapshot(self, account_ref: str) -> AccountSnapshot:
        self._scope(account_ref)
        return self.account

    async def orders(self, account_ref: str, symbol: str) -> tuple[ObservedOrder, ...]:
        self._scope(account_ref)
        return tuple(order for order in self.observed_orders if order.symbol == symbol)

    async def trades(self, account_ref: str, symbol: str, cursor: TradeCursor) -> TradeBatch:
        self._scope(account_ref)
        scoped = tuple(trade for trade in self.observed_trades if trade.symbol == symbol)
        start = 0
        if cursor.last_trade_id is not None:
            positions = [
                index
                for index, trade in enumerate(scoped)
                if trade.trade_id == cursor.last_trade_id
            ]
            if len(positions) != 1:
                raise ValueError("offline cursor does not match fixture history")
            start = positions[0] + 1
        remaining = scoped[start:]
        next_cursor = (
            TradeCursor(
                last_trade_id=remaining[-1].trade_id,
                last_executed_at=remaining[-1].executed_at,
            )
            if remaining
            else cursor
        )
        return TradeBatch(
            account_ref=account_ref,
            symbol=symbol,
            market_type=self.account.market_type,
            trades=remaining,
            next_cursor=next_cursor,
            history_complete=True,
        )

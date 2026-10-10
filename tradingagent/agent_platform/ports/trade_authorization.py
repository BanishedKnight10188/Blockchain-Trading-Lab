from datetime import datetime
from typing import Protocol

from agent_platform.domain.futures_market import FuturesMarketSnapshot
from agent_platform.domain.trading_execution import TradeCommand, TradingAccountSnapshot


class TradeAuthorizationPort(Protocol):
    async def authorize(
        self,
        command: TradeCommand,
        snapshot: FuturesMarketSnapshot,
        account: TradingAccountSnapshot,
        at: datetime,
    ) -> None: ...

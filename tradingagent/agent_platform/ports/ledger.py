"""A bounded read of the local journal, with scope supplied by the server."""

from typing import Protocol

from agent_platform.domain.ledger_views import LedgerData


class LedgerReadPort(Protocol):
    async def ledger(
        self, account_ref: str, *, after_sequence: int = 0, limit: int = 50
    ) -> LedgerData: ...

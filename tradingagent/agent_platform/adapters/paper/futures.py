"""Neutral execution/account adapter around the existing isolated virtual ledger."""

from datetime import timedelta
from uuid import uuid4

from agent_platform.domain.common import utc_datetime
from agent_platform.domain.futures_paper import (
    FuturesPaperOrder,
    FuturesPaperRules,
    FuturesPaperSettings,
)
from agent_platform.domain.futures_paper_engine import value
from agent_platform.domain.futures_values import FuturesQuote
from agent_platform.domain.trading_execution import (
    ExecutionReceipt,
    ExecutionScope,
    TradeCommand,
    TradingAccountSnapshot,
)
from agent_platform.ports.futures_paper import FuturesPaperStorePort
from agent_platform.ports.persistence import EventIdentityConflict
from agent_platform.ports.trading_execution import ExecutionRejected


class PaperFuturesBackend:
    public_metadata = {
        "simulation_rules": {"maintenance_margin_rate": "0.005", "liquidation_fee_bps": "50"}
    }

    def __init__(self, store: FuturesPaperStorePort, *, market_source: str):
        if market_source not in ("offline_replay", "binance_futures_public"):
            raise ValueError("paper backend requires an explicit supported market source")
        self.store, self.market_source = store, market_source

    async def configure(self, scope, limits, rules, at, *, style_revision):
        self._scope(scope)
        return await self.store.create(
            scope.session_id,
            FuturesPaperSettings.model_validate(limits.model_dump()),
            FuturesPaperRules(
                qty_step=rules.market_step,
                min_qty=rules.market_min_qty,
                max_qty=rules.market_max_qty,
                tick_size=rules.price_tick,
                min_notional=rules.min_notional,
                maintenance_margin_rate="0.005",
                liquidation_fee_bps="50",
            ),
            at,
            expected_style_revision=style_revision,
        )

    async def start(self, scope, revision, at, *, style_revision, trader_revision):
        self._scope(scope)
        return await self.store.start(
            scope.account_ref,
            revision,
            at,
            expected_style_revision=style_revision,
            expected_trader_revision=trader_revision,
        )

    async def pause(self, scope, revision, at):
        self._scope(scope)
        return await self.store.pause(scope.account_ref, revision, at)

    async def recover(self, at):
        await self.store.recover(at)

    async def maintenance_accounts(self):
        return tuple(
            (
                ExecutionScope(
                    environment="paper",
                    account_ref=s.account_ref,
                    session_id=s.session_id,
                    symbol=s.symbol,
                ),
                s.created_at,
            )
            for s in await self.store.maintenance_accounts()
        )

    async def settle(self, scope, funding, at):
        self._scope(scope)
        state = await self.store.get(scope.account_ref)
        if funding.source != self.market_source:
            raise ExecutionRejected("funding source differs")
        if state.last_funding_at is not None and funding.settled_at <= state.last_funding_at:
            return
        await self.store.funding(
            scope.account_ref, state.revision, funding, at, command_id="funding:" + uuid4().hex
        )

    async def mark(self, scope, quote, at):
        self._scope(scope)
        if quote.source != self.market_source:
            raise ExecutionRejected("mark source differs")
        state = await self.store.get(scope.account_ref)
        await self.store.mark(
            scope.account_ref, state.revision, quote, at, command_id="mark:" + uuid4().hex
        )

    async def recent(self, scope):
        self._scope(scope)
        return await self.store.recent(scope.account_ref)

    @staticmethod
    def _scope(scope):
        scope = ExecutionScope.model_validate(scope)
        if scope.environment != "paper":
            raise ExecutionRejected("backend does not own this execution environment")
        return scope

    @staticmethod
    def _receipt(command, operation, at):
        if operation.kind == "liquidation":
            return ExecutionReceipt(
                command=command,
                status="rejected",
                reason="account_liquidated",
                backend_order_id="paper-operation:" + command.command_id,
                backend_at=operation.occurred_at,
                observed_at=at,
            )
        return ExecutionReceipt(
            command=command,
            status="filled",
            filled_quantity=operation.quantity,
            average_price=operation.price,
            fee_usdt=operation.fee_usdt,
            backend_order_id="paper-order:" + command.command_id,
            backend_at=operation.occurred_at,
            observed_at=at,
        )

    async def lookup(self, command: TradeCommand, at) -> ExecutionReceipt | None:
        command = TradeCommand.model_validate(command)
        self._scope(command.scope)
        at = utc_datetime(at)
        record = await self.store.lookup_trade(command)
        if record is None:
            return None
        if record.quote.source != self.market_source or record.operation.occurred_at > at:
            raise ExecutionRejected("execution provenance or chronology is unavailable")
        return self._receipt(command, record.operation, at)

    async def submit(self, command: TradeCommand, quote: FuturesQuote, at) -> ExecutionReceipt:
        command = TradeCommand.model_validate(command)
        self._scope(command.scope)
        at = utc_datetime(at)
        prior = await self.lookup(command, at)
        if prior is not None:
            return prior
        try:
            quote = FuturesQuote.model_validate(quote)
            if (
                not command.created_at <= at <= command.expires_at
                or quote.symbol != command.scope.symbol
                or quote.source != self.market_source
                or quote.received_at > at
                or at - min(quote.received_at, quote.mark_at, quote.book_at) > timedelta(seconds=5)
            ):
                raise ExecutionRejected("command or quote is unavailable")
            account = await self.store.get(command.scope.account_ref)
            if (
                account.symbol != command.scope.symbol
                or account.session_id != command.scope.session_id
            ):
                raise ExecutionRejected("account does not match the selected contract")
            transition = await self.store.execute(
                account.account_ref,
                command.expected_account_revision,
                FuturesPaperOrder(
                    action=command.action,
                    quantity=command.quantity,
                    target_leverage=command.target_leverage,
                ),
                quote,
                at,
                command_id=command.command_id,
                expected_style_revision=command.style_revision,
                expected_trader_revision=command.trader_revision,
                execution_command=command,
            )
        except EventIdentityConflict:
            raise
        except (ValueError, LookupError):
            raise ExecutionRejected("paper execution rejected by current facts") from None
        return self._receipt(command, transition.operation, at)

    async def account(self, scope: ExecutionScope, at) -> TradingAccountSnapshot:
        scope = self._scope(scope)
        at = utc_datetime(at)
        state = await self.store.get(scope.account_ref)
        if (
            state.symbol != scope.symbol
            or state.session_id != scope.session_id
            or state.updated_at > at
        ):
            raise ExecutionRejected("account scope or chronology differs")
        quote, equity, pnl = state.last_quote, None, None
        if quote is not None:
            if quote.source != self.market_source or quote.received_at > at:
                raise ExecutionRejected("account market provenance differs")
            if at - min(quote.received_at, quote.mark_at) <= timedelta(seconds=5):
                valuation = value(state, quote, at)
                equity, pnl = valuation.equity_usdt, valuation.unrealized_pnl_usdt
        return TradingAccountSnapshot(
            scope=scope,
            revision=state.revision,
            status=state.status,
            free_usdt=state.free_usdt,
            margin_usdt=state.margin_usdt,
            quantity=state.quantity,
            side=state.side,
            entry_notional=state.entry_notional,
            realized_pnl_usdt=state.realized_pnl_usdt,
            funding_usdt=state.funding_usdt,
            fees_usdt=state.fees_usdt,
            equity_usdt=equity,
            unrealized_pnl_usdt=pnl,
            quote=quote,
            captured_at=at,
            leverage=state.settings.leverage,
        )

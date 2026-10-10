"""Independent deterministic protection based on actual receipt and account facts."""

import asyncio
from datetime import timedelta

from agent_platform.domain.agent_trade_evidence import GuardianTradeEvidence
from agent_platform.domain.position_protection import ProtectionStatus, protection_triggered
from agent_platform.domain.trading_execution import TERMINAL_STATUSES, TradeCommand
from agent_platform.ports.futures_market import FuturesMarketUnavailable
from agent_platform.ports.sessions import PersistenceUnavailable


class PositionGuardian:
    def __init__(self, *, protections, runs, execution, accounts, market, clock):
        self.protections, self.runs, self.execution, self.accounts, self.market, self.clock = (
            protections,
            runs,
            execution,
            accounts,
            market,
            clock,
        )

    async def step(self, scope):
        values = await self.protections.recover(scope)
        if not values:
            return ProtectionStatus(status="not_configured")
        updated = []
        funding_error = None
        try:
            snapshot = await self.market.snapshot(scope.symbol)
            await self.accounts.mark(scope, snapshot.quote, self.clock.utcnow())
            # Bound optional maintenance so executable stops remain reachable.
            if hasattr(self.market, "settlements"):
                try:
                    async with asyncio.timeout(0.5):
                        state = await self.accounts.get(scope.account_ref)
                        after = max(
                            state.last_funding_at or state.created_at,
                            state.position_updated_at or state.created_at,
                        )
                        window = await self.market.settlements(
                            scope.symbol, after=after, through=self.clock.utcnow()
                        )
                        for funding in window.events:
                            await self.accounts.settle(scope, funding, self.clock.utcnow())
                except (
                    ValueError,
                    OSError,
                    TimeoutError,
                    PersistenceUnavailable,
                    FuturesMarketUnavailable,
                ) as error:
                    funding_error = str(error)[:256] or "funding_unavailable"
            for p in values:
                try:
                    record = await self.execution.journal.get("agent:" + p.intent.intent_id)
                except LookupError:
                    record = None
                if record is None:
                    # A crash before reservation proves no backend dispatch took place.
                    # The submitter may still be between prepare and journal reservation.
                    # Preserve the protection rather than racing an imminent dispatch.
                    updated.append(p)
                    continue
                record = await self.execution.reconcile(record.command.command_id)
                opening_terminal = record.receipt.status in TERMINAL_STATUSES
                if record.receipt.filled_quantity > p.filled_quantity:
                    p = await self.protections.save(
                        p.model_copy(
                            update={
                                "filled_quantity": record.receipt.filled_quantity,
                                "status": "ready",
                            }
                        ),
                        p.revision,
                    )
                account = await self.accounts.account(scope, self.clock.utcnow())
                if account.quantity == 0 or (
                    record.receipt.status in ("rejected", "canceled")
                    and not record.receipt.filled_quantity
                ):
                    p = await self.protections.save(
                        p.model_copy(
                            update={
                                "status": "closed" if opening_terminal else "ready",
                            }
                        ),
                        p.revision,
                    )
                    updated.append(p)
                    continue
                if p.closing_command_id:
                    try:
                        closed = await self.execution.reconcile(p.closing_command_id)
                    except LookupError:
                        # Crash before reservation proves no dispatch.
                        # A fresh bounded protection command is permitted.
                        p = await self.protections.save(
                            p.model_copy(update={"status": "ready", "closing_command_id": None}),
                            p.revision,
                        )
                        updated.append(p)
                        continue
                    account = await self.accounts.account(scope, self.clock.utcnow())
                    if account.quantity == 0:
                        p = await self.protections.save(
                            p.model_copy(
                                update={
                                    "status": "closed" if opening_terminal else "ready",
                                }
                            ),
                            p.revision,
                        )
                    elif closed.receipt.status in ("rejected", "canceled", "filled"):
                        p = await self.protections.save(
                            p.model_copy(update={"status": "ready", "closing_command_id": None}),
                            p.revision,
                        )
                    updated.append(p)
                    continue
                lane = await self.runs.lane(p.intent.evidence.lane_id)
                triggered = protection_triggered(p, lane, account, snapshot.quote)
                if triggered and p.filled_quantity:
                    e = p.intent.evidence
                    intent_id = f"{p.protection_id}:{p.revision}"
                    proof = GuardianTradeEvidence(
                        **{
                            k: getattr(e, k)
                            for k in (
                                "lane_id",
                                "scope",
                                "lane_revision",
                                "style_revision",
                                "agent_revision",
                                "policy_revision",
                            )
                        },
                        intent_id=intent_id,
                        protection_id=p.protection_id,
                        quantity=min(p.filled_quantity, account.quantity),
                        account_revision=account.revision,
                        original_quote=snapshot.quote,
                    )
                    command = TradeCommand(
                        command_id="guardian:" + intent_id,
                        scope=scope,
                        action="reduce",
                        quantity=proof.quantity,
                        created_at=self.clock.utcnow(),
                        expires_at=self.clock.utcnow() + timedelta(seconds=30),
                        expected_account_revision=account.revision,
                        style_revision=proof.style_revision,
                        trader_revision=proof.agent_revision,
                        decision_evidence=proof,
                    )
                    p = await self.protections.save(
                        p.model_copy(
                            update={"status": "closing", "closing_command_id": command.command_id}
                        ),
                        p.revision,
                    )
                    await self.execution.submit(command)
                    account = await self.accounts.account(scope, self.clock.utcnow())
                    if account.quantity == 0:
                        p = await self.protections.save(
                            p.model_copy(
                                update={
                                    "status": "closed" if opening_terminal else "ready",
                                }
                            ),
                            p.revision,
                        )
                updated.append(p)
            return ProtectionStatus(
                status="degraded" if funding_error else "ready",
                protections=tuple(updated),
                reason=funding_error,
            )
        except (
            ValueError,
            OSError,
            TimeoutError,
            PersistenceUnavailable,
            FuturesMarketUnavailable,
        ) as error:
            for p in values:
                current = await self.protections.get(p.protection_id)
                if current.status != "closed":
                    await self.protections.save(
                        current.model_copy(
                            update={"status": "degraded", "reason": str(error)[:256]}
                        ),
                        current.revision,
                    )
            return ProtectionStatus(status="degraded", reason=str(error)[:256])

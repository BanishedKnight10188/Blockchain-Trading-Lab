"""Preview and stable submit through the common execution journal."""

from datetime import timedelta
from hashlib import sha256

from agent_platform.domain.agent_trade_evidence import EventAgentTradeEvidence
from agent_platform.domain.trade_intents import TradeIntent, TradePreview
from agent_platform.domain.trading_execution import TradeCommand
from agent_platform.domain.watches import fact_hash


class AgentIntentService:
    def __init__(self, *, runs, protections, execution, accounts, market, clock, authorizer):
        (
            self.runs,
            self.protections,
            self.execution,
            self.accounts,
            self.market,
            self.clock,
            self.authorizer,
        ) = (runs, protections, execution, accounts, market, clock, authorizer)

    def _command(self, intent, run):
        e = intent.evidence
        return TradeCommand(
            command_id="agent:" + intent.intent_id,
            scope=intent.scope,
            action=intent.action,
            quantity=intent.quantity,
            target_leverage=e.target_leverage,
            created_at=self.clock.utcnow(),
            expires_at=min(self.clock.utcnow() + timedelta(seconds=30), run.deadline),
            expected_account_revision=e.account_revision,
            style_revision=e.style_revision,
            trader_revision=e.agent_revision,
            decision_evidence=e,
        )

    async def preview(self, intent, lane):
        # Preview has no order or protective side effect. Submit authorizes again with fresh facts.
        from agent_platform.application.shared_futures_risk import SharedFuturesRisk
        from agent_platform.domain.trade_authorization import FuturesRiskSettings

        try:
            if await self.runs.lane(lane.lane_id) != lane or not lane.enabled:
                raise ValueError("lane_changed")
            snapshot = await self.market.snapshot(lane.symbol)
            account = await self.accounts.account_at_quote(
                lane.scope, snapshot.quote, self.clock.utcnow()
            )
            SharedFuturesRisk.evaluate(
                FuturesRiskSettings.from_lane(lane),
                intent.evidence.original_quote,
                snapshot.quote,
                account,
                intent.action,
                intent.quantity,
                intent.evidence.target_leverage,
            )
            return TradePreview(intent=intent, allowed=True)
        except ValueError as error:
            return TradePreview(intent=intent, allowed=False, reason=str(error)[:256])

    async def submit(self, intent, lane):
        await self.protections.prepare(intent)
        # Crash after prepare is recovered from the execution journal, never resubmitted.
        run = await self.runs.get(intent.evidence.run_id)
        try:
            existing = await self.execution.journal.get("agent:" + intent.intent_id)
        except LookupError:
            existing = None
        if existing:
            return await self.execution.reconcile(existing.command.command_id)
        record = await self.execution.submit(self._command(intent, run))
        if intent.action != "reduce":
            p = await self.protections.get(intent.intent_id)
            status = (
                "ready"
                if record.receipt.filled_quantity > 0
                else "closed"
                if record.receipt.status in ("rejected", "canceled")
                else "prepared"
            )
            await self.protections.save(
                p.model_copy(
                    update={"status": status, "filled_quantity": record.receipt.filled_quantity}
                ),
                p.revision,
            )
        return record

    async def execute_tool(self, call, run, lane):
        if run.mode != "REVIEW" or run.event is None or run.context is None:
            raise ValueError("review_evidence_required")
        persisted = await self.runs.get(run.run_id)
        if not any(t.call == call for t in persisted.tools):
            raise ValueError("tool_not_archived")
        if (
            call.arguments.get("target_leverage") is not None
            and call.arguments["target_leverage"] != lane.limits.leverage
        ):
            raise ValueError("fixed_lane_leverage")
        response = next(
            t.response for t in persisted.turns if t.response and call in t.response.tool_calls
        )
        key = sha256(f"{run.run_id}:{call.tool_call_id}".encode()).hexdigest()
        try:
            intent = await self.protections.get_intent(key)
        except ValueError:
            snapshot = await self.market.snapshot(lane.symbol)
            if call.name == "preview_trade":
                account = await self.accounts.account_at_quote(
                    lane.scope, snapshot.quote, self.clock.utcnow()
                )
            else:
                await self.accounts.mark(lane.scope, snapshot.quote, self.clock.utcnow())
                account = await self.accounts.account(lane.scope, self.clock.utcnow())
            e = EventAgentTradeEvidence(
                intent_id=key,
                lane_id=lane.lane_id,
                scope=lane.scope,
                action=call.arguments["action"],
                quantity=call.arguments["quantity"],
                target_leverage=call.arguments.get("target_leverage"),
                account_revision=account.revision,
                lane_revision=lane.revision,
                style_revision=lane.style_revision,
                agent_revision=lane.agent_revision,
                policy_revision=lane.policy_revision,
                original_quote=snapshot.quote,
                run_id=run.run_id,
                event_id=run.event.event_id,
                watch_id=run.event.watch_id,
                definition_revision=run.event.definition_revision,
                rule_hash=run.event.rule_hash,
                context_hash=run.context.context_hash,
                tool_call_id=call.tool_call_id,
                model_response_hash=fact_hash(response),
                tool_arguments_hash=fact_hash(call.arguments),
            )
            intent = TradeIntent(
                intent_id=key,
                scope=lane.scope,
                action=e.action,
                quantity=e.quantity,
                evidence=e,
                protective_stop_mark=call.arguments.get("protective_stop_mark"),
            )
        if call.name == "preview_trade":
            return {"preview": (await self.preview(intent, lane)).model_dump(mode="json")}
        if call.name != "submit_trade_intent":
            raise ValueError("trade_tool_unknown")
        return {"execution": (await self.submit(intent, lane)).model_dump(mode="json")}

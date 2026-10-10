"""Mandatory ownership, hypothesis and common discipline checks before submit."""

import json
from datetime import timedelta

from agent_platform.application.shared_futures_risk import SharedFuturesRisk
from agent_platform.domain.agent_trade_evidence import (
    EventAgentTradeEvidence,
    GuardianTradeEvidence,
)
from agent_platform.domain.futures_values import FuturesQuote
from agent_platform.domain.position_protection import protection_triggered
from agent_platform.domain.trade_authorization import FuturesRiskSettings
from agent_platform.domain.watch_rules import evaluate_conditions
from agent_platform.domain.watches import fact_hash
from agent_platform.ports.trading_execution import ExecutionRejected


class LegacyTradeAuthorizer:
    """Only the explicitly assembled JEV run; no access to event lane wallets."""

    def __init__(self, store):
        self.store = store

    async def authorize(self, command, snapshot, account, at):
        if isinstance(command.decision_evidence, (EventAgentTradeEvidence, GuardianTradeEvidence)):
            raise ExecutionRejected("legacy_provenance_required")
        run = await self.store.run(command.scope.account_ref)
        if run is None or run.scope != command.scope:
            raise ExecutionRejected("legacy_run_unavailable")
        original = snapshot.quote
        if command.decision_evidence is not None:
            state = json.loads(command.decision_evidence.request.state_json)
            original = FuturesQuote.model_validate_json(
                json.dumps(state["market_snapshot"]["quote"])
            )
        SharedFuturesRisk.evaluate(
            run,
            original,
            snapshot.quote,
            account,
            command.action,
            command.quantity,
            command.target_leverage,
        )


class EventTradeAuthorizer:
    def __init__(self, *, runs, watches, events, data, protections):
        self.runs, self.watches, self.events, self.data, self.protections = (
            runs,
            watches,
            events,
            data,
            protections,
        )

    async def authorize(self, command, snapshot, account, at):
        evidence = command.decision_evidence
        if not isinstance(evidence, (EventAgentTradeEvidence, GuardianTradeEvidence)):
            raise ExecutionRejected("event_provenance_required")
        lane = await self.runs.lane(evidence.lane_id)
        if lane.scope != command.scope or not lane.risk_configured:
            raise ExecutionRejected("lane_scope_or_risk")
        if isinstance(evidence, GuardianTradeEvidence):
            protection = await self.protections.get(evidence.protection_id)
            if (
                protection.intent.scope != command.scope
                or command.action != "reduce"
                or protection.status not in ("ready", "degraded", "closing")
                or command.quantity > protection.filled_quantity
                or command.quantity > account.quantity
            ):
                raise ExecutionRejected("protection_scope")
            if not protection_triggered(protection, lane, account, snapshot.quote):
                raise ExecutionRejected("protective_condition_absent")
            return
        run = await self.runs.get(evidence.run_id)
        if (
            not lane.enabled
            or run.lane != lane
            or run.status != "RUNNING"
            or at >= run.deadline
            or run.mode != "REVIEW"
            or run.event is None
            or run.event.event_id != evidence.event_id
            or run.context is None
            or run.context.context_hash != evidence.context_hash
        ):
            raise ExecutionRejected("run_retired")
        intent = await self.protections.get_intent(evidence.intent_id)
        if intent.evidence != evidence:
            raise ExecutionRejected("intent_evidence_mismatch")
        call = next(
            (t.call for t in run.tools if t.call.tool_call_id == evidence.tool_call_id), None
        )
        if call is None or call.name != "submit_trade_intent":
            raise ExecutionRejected("tool_not_archived")
        response = next(
            (t.response for t in run.turns if t.response and call in t.response.tool_calls), None
        )
        if (
            response is None
            or fact_hash(response) != evidence.model_response_hash
            or (fact_hash(call.arguments) != evidence.tool_arguments_hash)
        ):
            raise ExecutionRejected("model_or_tool_evidence_mismatch")
        from decimal import Decimal

        if (
            call.arguments.get("target_leverage") != evidence.target_leverage
            or (
                Decimal(call.arguments["protective_stop_mark"])
                if call.arguments.get("protective_stop_mark") is not None
                else None
            )
            != intent.protective_stop_mark
        ):
            raise ExecutionRejected("tool_intent_mismatch")
        if (
            evidence.target_leverage is not None
            and evidence.target_leverage != lane.limits.leverage
        ):
            raise ExecutionRejected("fixed_lane_leverage")
        if call.arguments.get("action") != command.action or str(
            call.arguments.get("quantity")
        ) != str(intent.quantity):
            # Decimal representations may vary; validate structurally through the intent service.
            from decimal import Decimal

            if (
                call.arguments.get("action") != command.action
                or Decimal(call.arguments.get("quantity", "0")) != intent.quantity
            ):
                raise ExecutionRejected("tool_intent_mismatch")
        if (
            evidence.lane_revision,
            evidence.style_revision,
            evidence.agent_revision,
            evidence.policy_revision,
        ) != (lane.revision, lane.style_revision, lane.agent_revision, lane.policy_revision):
            raise ExecutionRejected("lane_revision_changed")
        event = await self.events.get(evidence.event_id)
        watch = await self.watches.get(event.watch_id)
        if (
            not await self.watches.partition_healthy(event.watch_id)
            or watch.state != "TRIGGERED"
            or watch.definition.version != evidence.definition_revision
            or watch.definition.rule_hash != evidence.rule_hash
            or at >= event.expires_at
        ):
            raise ExecutionRejected("hypothesis_retired")
        current = await self.data.latest(lane.symbol, watch.definition.interval)
        if (
            current.effective_quality != "ready"
            or current.occurred_at < event.occurred_at
            or current.occurred_at > at
            or at - current.occurred_at > timedelta(minutes=5 if current.interval == "5m" else 1)
        ):
            raise ExecutionRejected("watch_facts_unavailable")
        if watch.definition.invalidation is not None:
            invalid = evaluate_conditions(watch.definition.invalidation, current)
            if invalid.result != "false":
                raise ExecutionRejected("hypothesis_invalidated_or_unavailable")
        SharedFuturesRisk.evaluate(
            FuturesRiskSettings.from_lane(lane),
            evidence.original_quote,
            snapshot.quote,
            account,
            command.action,
            command.quantity,
            command.target_leverage,
        )

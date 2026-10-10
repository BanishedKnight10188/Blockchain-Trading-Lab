"""Explicit offline lane configuration; never inherited from a JEV wallet."""

from datetime import timedelta
from importlib import import_module

from tests.fixtures.watch_cases import NOW, definition, frame


def lane(**changes):
    cls = import_module("agent_platform.domain.event_agent").EventAgentLane
    data = dict(
        lane_id="lane-1",
        session_id="session-agent",
        enabled=True,
        scope={
            "environment": "paper",
            "account_ref": "paper:futures:session-agent",
            "session_id": "session-agent",
            "symbol": "BTCUSDT",
        },
        policy={
            "order_notional_usdt": "100",
            "max_price_drift_bps": "100",
            "min_confidence": "0",
            "strategy_instructions": "Offline test only",
        },
        limits={
            "initial_usdt": "1000",
            "leverage": 2,
            "max_position_notional": "500",
            "max_run_loss_usdt": "100",
            "fee_bps": "4",
            "slippage_bps": "1",
        },
        qty_step="0.01",
        min_qty="0.01",
        max_qty="10",
        min_notional="5",
    )
    return cls.model_validate(data | changes)


def event():
    from agent_platform.domain.agent_events import WatchEvent, watch_event_id
    from agent_platform.domain.watch_rules import evaluate_watch
    from agent_platform.domain.watches import WatchRecord

    d = definition(session_id="session-agent")
    f = frame()
    return WatchEvent(
        event_id=watch_event_id(d, f),
        lane_id=d.lane_id,
        session_id=d.session_id,
        watch_id=d.watch_id,
        definition_revision=1,
        occurred_at=NOW,
        expires_at=NOW + timedelta(seconds=120),
        rule_hash=d.rule_hash,
        frame=f,
        evaluation=evaluate_watch(WatchRecord(definition=d), f, NOW),
    )


def request(**changes):
    cls = import_module("agent_platform.domain.event_agent").AgentTurnRequest
    data = dict(
        request_id="agent-request-1",
        run_id="agent-run-1",
        lane_id="lane-1",
        captured_at=NOW,
        deadline=NOW + timedelta(seconds=30),
        route={
            "route_id": "event-agent",
            "kind": "standard",
            "purpose": "advisory",
            "reason": "offline_protocol_fixture",
            "model_version": "test/model",
            "price_version": "test-price",
            "paid": True,
        },
        messages=[{"role": "user", "content": "Inspect the closed candle"}],
        tools=[
            {
                "name": "get_market_snapshot",
                "description": "Latest market",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            }
        ],
    )
    return cls.model_validate(data | changes)


def response(request_id="agent-request-1", calls=(), **changes):
    cls = import_module("agent_platform.domain.event_agent").AgentTurnResponse
    data = dict(
        request_id=request_id,
        tool_calls=calls,
        final=None if calls else {"action": "WAIT", "reason": "No entry"},
        usage={
            "request_id": request_id,
            "route_id": "event-agent",
            "model_version": "test/model",
            "input_tokens": 10,
            "output_tokens": 5,
            "estimated_cost_usd": "0.000001",
            "actual_cost_usd": "0",
            "billing_status": "confirmed",
            "recorded_at": NOW,
        },
    )
    return cls.model_validate(data | changes)


def grant(**changes):
    cls = import_module("agent_platform.domain.event_agent").AgentBudgetGrant
    data = dict(
        grant_id="new-agent-grant",
        lane_id="lane-1",
        model_id="test/model",
        price_version="test-price",
        lane_total_usd="0.01",
        parent_total_usd="0.02",
        max_single_cost_usd="0.005",
        hourly_call_limit=10,
        valid_from=NOW,
        expires_at=NOW + timedelta(hours=1),
    )
    return cls.model_validate(data | changes)

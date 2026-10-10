"""Production historical analysis uses local transport and fee fixtures."""

import importlib
import json
from datetime import timedelta
from decimal import Decimal

import pytest

from agent_platform.adapters.fake.clock import FakeClock
from agent_platform.adapters.fake.session_analysis import FakeHistoricalMarket
from agent_platform.adapters.sqlite import open_store
from agent_platform.domain.routing import ModelPrice
from agent_platform.domain.session_analysis import InitialAnalysisRequest
from agent_platform.domain.session_market import SessionAnalysisTarget
from agent_platform.domain.sessions import TradingStyle
from agent_platform.ports.model import ModelCallFailed
from agent_platform.ports.persistence import DispatchAlreadyReserved
from tests.test_session_market import NOW


class LocalClient:
    def __init__(self, failure=None):
        self.calls = []
        self.failure = failure

    async def post(self, path, payload, deadline):
        self.calls.append((path, payload))
        assert path == "/api/v1/chat/completions"
        assert payload["model"] == "anthropic/claude-haiku-5.5"
        context = json.loads(payload["messages"][1]["content"])
        assert context["target"]["symbol"] == "ETHUSDT" and len(context["candles"]) == 168
        assert "BTC" not in json.dumps(context)
        if self.failure == "transport":
            raise importlib.import_module(
                "agent_platform.adapters.openrouter.transport"
            ).OpenRouterError("private error")
        answer = {
            "trend": "uncertain",
            "summary": "历史测试回答",
            "evidence": ["已收盘历史"],
            "risks": ["无账户信息"],
            "watch_conditions": ["观察后续变化"],
        }
        usage = {"prompt_tokens": 100, "completion_tokens": 50, "cost": "0.00001"}
        if self.failure == "invalid_answer":
            answer["unexpected_order"] = "BUY"
        if self.failure == "missing_cost":
            usage.pop("cost")
        return {
            "id": "local-request",
            "model": "anthropic/claude-haiku-5.5",
            "provider": "Anthropic",
            "usage": usage,
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": json.dumps(answer)},
                }
            ],
        }


async def context(tmp_path, *, enabled=True, failure=None, high_price=False):
    clock = FakeClock(NOW)
    history = await FakeHistoricalMarket(clock).history(
        SessionAnalysisTarget(market="usdt_perpetual", symbol="ETHUSDT")
    )
    history = history.model_copy(update={"source": "binance_futures_public"})
    request = InitialAnalysisRequest(
        request_id="initial:test",
        session_id="test",
        style=TradingStyle(strength=80),
        style_revision=1,
        history=history,
        deadline=NOW + timedelta(seconds=15),
    )
    budgets = await open_store(tmp_path / "flash.sqlite3")
    client = LocalClient(failure)
    model = importlib.import_module(
        "agent_platform.adapters.openrouter.initial_analysis"
    ).OpenRouterInitialFlash(
        client=client,
        budgets=budgets,
        clock=clock,
        enabled=enabled,
        providers=("anthropic",),
        daily_limit_usd="1",
        single_call_usd="0.02",
        price=ModelPrice(
            version="test-price",
            input_usd_per_million="100" if high_price else "0.01",
            output_usd_per_million="0.01",
            verified_at=NOW - timedelta(seconds=1),
            valid_until=NOW + timedelta(minutes=10),
        ),
    )
    return model, request, budgets, client


@pytest.mark.asyncio
async def test_initial_flash_payload_fee_and_bound_response(tmp_path):
    model, request, budgets, client = await context(tmp_path)
    result = await model.analyze(request)
    assert result.source == "model" and result.history_hash == request.history.content_hash
    assert result.usage.actual_cost_usd == Decimal("0.00001")
    assert len(client.calls) == 1
    with pytest.raises(DispatchAlreadyReserved):
        await model.analyze(request)
    assert len(client.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["transport", "invalid_answer", "missing_cost"])
async def test_invalid_or_unknown_fee_does_not_return_current_analysis(tmp_path, failure):
    model, request, _, client = await context(tmp_path, failure=failure)
    with pytest.raises(ModelCallFailed):
        await model.analyze(request)
    assert len(client.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["disabled", "price", "expired"])
async def test_disabled_cost_and_deadline_gates_run_before_client(tmp_path, kind):
    model, request, _, client = await context(
        tmp_path, enabled=kind != "disabled", high_price=kind == "price"
    )
    if kind == "expired":
        model.clock.advance_to(NOW + timedelta(seconds=15))
    with pytest.raises(ModelCallFailed):
        await model.analyze(request)
    assert not client.calls

import json
from datetime import timedelta
from decimal import Decimal

import pytest

from agent_platform.adapters.fake.clock import FakeClock
from agent_platform.adapters.openrouter.background import OpenRouterBackground
from agent_platform.adapters.openrouter.transport import OpenRouterError
from agent_platform.adapters.sqlite import open_store
from agent_platform.domain.background import BackgroundSettings
from agent_platform.domain.routing import ModelPrice
from agent_platform.ports.model import ModelCallFailed
from tests.domain.test_background import answer, request
from tests.domain.test_multiscale import NOW


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_cost", [False, True, "transport"])
async def test_background_shares_cumulative_fee_and_preserves_unknown(tmp_path, missing_cost):
    budgets = await open_store(tmp_path / "fee.sqlite3")
    clock = FakeClock(NOW)
    req = request()
    req = req.model_copy(
        update={
            "windows": tuple(
                w.model_copy(update={"source": "binance_futures_public"}) for w in req.windows
            )
        }
    )
    calls = []

    class Client:
        async def post(self, path, payload, deadline, *, max_wait_seconds):
            assert max_wait_seconds == 60
            assert payload["reasoning"] == {"enabled": False}
            calls.append(payload)
            if missing_cost == "transport":
                raise OpenRouterError("provider_http_503")
            data = json.loads(payload["messages"][1]["content"])
            assert [len(w["candles"]) for w in data["windows"]] == [90, 180, 168, 288]
            usage = {"prompt_tokens": 100, "completion_tokens": 50}
            if not missing_cost:
                usage["cost"] = "0.00001"
            return {
                "id": "bg-provider",
                "model": "test/background",
                "provider": "test",
                "usage": usage,
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": answer().model_dump_json(exclude={"schema_version"}),
                        },
                    }
                ],
            }

    settings = BackgroundSettings(
        enabled=True,
        model_id="test/background",
        providers=("test",),
        price=ModelPrice(
            version="verified-test",
            verified_at=NOW - timedelta(seconds=1),
            input_usd_per_million="0.01",
            output_usd_per_million="0.01",
        ),
    )
    model = OpenRouterBackground(
        client=Client(),
        budgets=budgets,
        clock=clock,
        settings=settings,
        total_usd=Decimal("0.1"),
        single_usd=Decimal("0.02"),
        active=lambda: True,
    )
    if missing_cost:
        with pytest.raises(ModelCallFailed) as failed:
            await model.analyze(req)
        if missing_cost == "transport":
            assert failed.value.reason == "provider_http_503"
    else:
        assert (await model.analyze(req)).usage.actual_cost_usd == Decimal("0.00001")
    balance = await budgets.budget_balance(NOW, daily_limit_usd="0.1", cumulative=True)
    assert len(calls) == 1
    assert balance.reserved_usd > 0 if missing_cost else balance.spent_usd == Decimal("0.00001")

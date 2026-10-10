import pytest

from agent_platform.domain.trading_execution import TradeCommand
from tests.integration.test_futures_trading_core import assembled as assembled
from tests.integration.test_futures_trading_core import configured


@pytest.mark.asyncio
async def test_old_jev_evidence_roundtrip(assembled):
    svc, clock, ctx, worker = assembled
    await configured(svc)
    svc.model.choices = ("OPEN_LONG",)
    await svc.step()
    operations = await svc.backend.recent((await svc.store.run("paper:futures:s1")).scope)
    trades = [r for r in operations if r.execution_command]
    assert trades
    command = trades[-1].execution_command
    assert TradeCommand.model_validate_json(command.model_dump_json()) == command
    assert command.command_id.startswith("jev:")

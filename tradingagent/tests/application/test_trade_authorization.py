import pytest

from tests.adapters.test_futures_execution_backend import cmd
from tests.application.test_trading_execution import setup


@pytest.mark.asyncio
async def test_backend_never_called_without_authorization(tmp_path):
    ctx, a, backend, clock, market, journal, service = await setup(tmp_path, deferred=True)
    service.authorizer = None
    result = await service.submit(cmd(a))
    assert result.receipt.status == "rejected"
    assert backend.submissions == []

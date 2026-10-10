import pytest

from tests.fixtures.event_agent_cases import lane
from tests.fixtures.watch_cases import NOW


def test_event_agent_cannot_forge_jev_or_null_evidence():
    from agent_platform.domain.trading_execution import TradeCommand

    value = lane()
    from datetime import timedelta

    with pytest.raises(ValueError):
        TradeCommand(
            command_id="agent:forged",
            scope=value.scope,
            action="open_long",
            quantity="1",
            created_at=NOW,
            expires_at=NOW + timedelta(seconds=30),
            expected_account_revision=1,
            style_revision=1,
            trader_revision=1,
        )

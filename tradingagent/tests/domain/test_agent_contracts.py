"""Agent limits and message/result identities are enforced at ingress."""

from datetime import timedelta

import pytest

from tests.fixtures.event_agent_cases import grant, lane, request, response
from tests.fixtures.watch_cases import NOW


def test_tool_call_protocol_and_usage_binding():
    answer = response(
        calls=({"tool_call_id": "call-1", "name": "get_market_snapshot", "arguments": {}},)
    )
    followup = request(
        messages=[
            {"role": "user", "content": "Inspect"},
            {"role": "assistant", "tool_calls": answer.tool_calls},
            {"role": "tool", "tool_call_id": "call-1", "content": "{}"},
        ]
    )
    assert followup.messages[-1].tool_call_id == answer.tool_calls[0].tool_call_id
    with pytest.raises(ValueError):
        response(calls=answer.tool_calls, final={"action": "WAIT", "reason": "mixed"})
    with pytest.raises(ValueError):
        response(request_id="wrong", usage=response().usage)


@pytest.mark.parametrize(
    "change",
    [
        {"max_output_tokens": 2049},
        {"deadline": NOW + timedelta(seconds=31)},
        {"messages": [{"role": "user", "content": "x" * 32769}]},
        {"messages": [{"role": "tool", "tool_call_id": "orphan", "content": "{}"}]},
    ],
)
def test_turn_bounds_reject_overflow_or_orphan(change):
    with pytest.raises(ValueError):
        request(**change)


def test_lane_scope_and_grant_are_explicit():
    assert lane().scope.account_ref == "paper:futures:session-agent"
    with pytest.raises(ValueError):
        lane(
            scope={
                "environment": "paper",
                "account_ref": "paper:futures:session-agent",
                "session_id": "session-agent",
                "symbol": "ETHUSDT",
            },
            symbol="BTCUSDT",
        )
    with pytest.raises(ValueError):
        grant(expires_at=NOW)

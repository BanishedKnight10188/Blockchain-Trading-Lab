"""The first confirmed trial cap constrains every later request in that budget day."""

from datetime import timedelta
from decimal import Decimal

import pytest

from agent_platform.bootstrap_paper import PaperModelConfig
from agent_platform.domain.costs import BudgetRequest
from agent_platform.ports.paper import PaperGuardConflict
from agent_platform.ports.persistence import BudgetExceeded
from tests.adapters.test_paper_store import context as _context
from tests.domain.test_paper_trading import NOW
from tests.test_paper_config import config_data

context = _context


def renewal():
    issued = NOW + timedelta(hours=1, seconds=1)
    values = config_data() | {
        "grant_id": "confirmed-renewal-1",
        "trial_total_usd": "0.1",
        "issued_at": issued,
        "expires_at": issued + timedelta(minutes=30),
    }
    return PaperModelConfig(**values)


@pytest.mark.asyncio
async def test_explicit_renewal_preserves_root_policy_and_existing_fee_exposure(context):
    first = PaperModelConfig(**config_data())
    original = await context[2].ensure_trial(first, NOW)
    await context[3].reserve(
        BudgetRequest(
            request_id="old-unknown",
            route_id="old",
            purpose="advisory",
            price_version="old",
            estimated_cost_usd="0.09",
            daily_limit_usd="1",
            requested_at=NOW,
        )
    )
    policy = renewal()
    record = await context[2].ensure_trial(policy, policy.issued_at)
    assert record.aggregate_id != original.aggregate_id
    assert (await context[3].load(first.budget_key)).state == original
    assert await context[2].ensure_trial(policy, policy.issued_at) == record
    balance = await context[3].budget_balance(policy.issued_at, daily_limit_usd="0.1")
    assert balance.reserved_usd == Decimal("0.09")
    with pytest.raises(BudgetExceeded):
        await context[3].reserve(
            BudgetRequest(
                request_id="new-over-limit",
                route_id="new",
                purpose="advisory",
                price_version="new",
                estimated_cost_usd="0.02",
                daily_limit_usd="1",
                requested_at=policy.issued_at,
            )
        )


@pytest.mark.asyncio
async def test_renewal_cannot_raise_first_cap_or_rewrite_an_existing_grant(context):
    await context[2].ensure_trial(PaperModelConfig(**config_data()), NOW)
    policy = renewal()
    too_large = PaperModelConfig.model_validate_json(
        policy.model_copy(update={"trial_total_usd": Decimal("2")}).model_dump_json()
    )
    with pytest.raises(PaperGuardConflict):
        await context[2].ensure_trial(too_large, policy.issued_at)
    await context[2].ensure_trial(policy, policy.issued_at)
    changed = PaperModelConfig.model_validate_json(
        policy.model_copy(
            update={"expires_at": policy.expires_at + timedelta(minutes=1)}
        ).model_dump_json()
    )
    with pytest.raises(PaperGuardConflict):
        await context[2].ensure_trial(changed, policy.issued_at)


@pytest.mark.asyncio
async def test_renewal_requires_existing_expired_first_policy(context):
    policy = renewal()
    with pytest.raises(PaperGuardConflict):
        await context[2].ensure_trial(policy, policy.issued_at)


@pytest.mark.asyncio
async def test_first_trial_limit_cannot_be_raised_on_restart(context):
    policy = PaperModelConfig(**config_data())
    saved = await context[2].ensure_trial(policy, NOW)
    assert await context[2].ensure_trial(policy, NOW) == saved
    larger = PaperModelConfig(**(config_data() | {"trial_total_usd": "10"}))
    with pytest.raises(PaperGuardConflict):
        await context[2].ensure_trial(larger, NOW)


@pytest.mark.asyncio
async def test_other_module_cannot_reserve_above_durable_first_cap(context):
    await context[2].ensure_trial(PaperModelConfig(**config_data()), NOW)
    await context[3].reserve(
        BudgetRequest(
            request_id="first-full-budget",
            route_id="first",
            purpose="advisory",
            price_version="known",
            estimated_cost_usd="1",
            daily_limit_usd="1",
            requested_at=NOW,
        )
    )
    with pytest.raises(BudgetExceeded):
        await context[3].reserve(
            BudgetRequest(
                request_id="larger-second-budget",
                route_id="second",
                purpose="advisory",
                price_version="known",
                estimated_cost_usd="0.01",
                daily_limit_usd="10",
                requested_at=NOW,
            )
        )

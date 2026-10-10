from datetime import timedelta
from decimal import Decimal

import pytest

from agent_platform.adapters.sqlite import open_store
from agent_platform.adapters.sqlite.paper import SqlitePaperStore
from agent_platform.domain.costs import BudgetRequest, ModelUsage
from agent_platform.domain.paper_trials import PaperTrialPolicy
from agent_platform.ports.paper import PaperGuardConflict
from agent_platform.ports.persistence import BudgetExceeded
from tests.domain.test_paper_trading import NOW
from tests.test_jev_continuous import continuous_data


def amendment(parent, at=NOW + timedelta(days=1), **extra):
    return PaperTrialPolicy(
        **(
            continuous_data(at=at, grant_id="usd1")
            | {
                "trial_total_usd": "1",
                "supersedes_budget_key": parent.budget_key,
                "budget_change_confirmed": True,
            }
            | extra
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("cross_day", [False, True])
async def test_confirmed_append_changes_cap_but_preserves_every_old_fee(tmp_path, cross_day):
    path = tmp_path / "fees.sqlite3"
    core, trials = await open_store(path), SqlitePaperStore(path)
    parent = PaperTrialPolicy(**(continuous_data() | {"trial_total_usd": "0.1"}))
    await trials.ensure_trial(parent, NOW)
    await core.reserve(
        BudgetRequest(
            request_id="old-held",
            route_id="jev",
            purpose="advisory",
            price_version="verified",
            estimated_cost_usd="0.09",
            daily_limit_usd="0.1",
            requested_at=NOW,
        )
    )
    with core._connect() as db:
        old = db.execute("SELECT * FROM budget_requests").fetchall()[0]
        old = tuple(old)
    at = NOW + timedelta(days=1) if cross_day else NOW + timedelta(seconds=1)
    change = amendment(parent, at=at)
    await trials.ensure_trial(change, at)
    await trials.ensure_trial(change, at)  # Idempotent, never a new allowance.
    assert (await trials.load(parent.budget_key)).state.policy == parent
    status = await core.budget_balance(at, daily_limit_usd="1", cumulative=True)
    assert status.daily_limit_usd == 1 and status.reserved_usd == Decimal("0.09")
    await core.reserve(
        BudgetRequest(
            request_id="after-confirmation",
            route_id="jev",
            purpose="advisory",
            price_version="verified",
            estimated_cost_usd="0.02",
            daily_limit_usd="1",
            requested_at=at,
        )
    )
    with core._connect() as db:
        assert (
            tuple(
                db.execute("SELECT * FROM budget_requests WHERE request_id='old-held'").fetchone()
            )
            == old
        )
    assert (
        await core.budget_balance(at, daily_limit_usd="1", cumulative=True)
    ).reserved_usd == Decimal("0.11")


@pytest.mark.asyncio
async def test_missing_parent_and_fork_do_not_authorize_a_higher_budget(tmp_path):
    core = await open_store(tmp_path / "fees.sqlite3")
    trials = SqlitePaperStore(core.path)
    parent = PaperTrialPolicy(**(continuous_data() | {"trial_total_usd": "0.1"}))
    with pytest.raises(PaperGuardConflict):
        await trials.ensure_trial(amendment(parent), NOW + timedelta(days=1))
    await trials.ensure_trial(parent, NOW)
    change = amendment(parent)
    await trials.ensure_trial(change, change.issued_at)
    with pytest.raises(PaperGuardConflict):
        await trials.ensure_trial(amendment(parent, grant_id="fork"), change.issued_at)


@pytest.mark.parametrize(
    "fields",
    [
        {"budget_change_confirmed": False},
        {"supersedes_budget_key": None},
    ],
)
def test_budget_revision_requires_parent_and_explicit_confirmation(fields):
    parent = PaperTrialPolicy(**(continuous_data() | {"trial_total_usd": "0.1"}))
    with pytest.raises(ValueError):
        amendment(parent, **fields)


@pytest.mark.asyncio
@pytest.mark.parametrize("cross_day", [False, True])
async def test_old_unknown_settlement_uses_current_cap_without_rewriting_request(
    tmp_path, cross_day
):
    core = await open_store(tmp_path / "fees.sqlite3")
    trials = SqlitePaperStore(core.path)
    parent = PaperTrialPolicy(**(continuous_data() | {"trial_total_usd": "0.1"}))
    await trials.ensure_trial(parent, NOW)
    old = await core.reserve(
        BudgetRequest(
            request_id="old",
            route_id="jev",
            purpose="advisory",
            price_version="verified",
            estimated_cost_usd="0.02",
            daily_limit_usd="0.1",
            requested_at=NOW,
        )
    )
    await core.settle(
        old.reservation_id,
        ModelUsage(
            request_id="old",
            route_id="jev",
            model_version="jev",
            input_tokens=0,
            output_tokens=0,
            estimated_cost_usd="0.02",
            billing_status="unknown",
            recorded_at=NOW,
        ),
    )
    at = NOW + timedelta(days=1) if cross_day else NOW + timedelta(seconds=1)
    await trials.ensure_trial(amendment(parent, at=at), at)
    await core.reserve(
        BudgetRequest(
            request_id="new",
            route_id="jev",
            purpose="advisory",
            price_version="verified",
            estimated_cost_usd="0.1",
            daily_limit_usd="1",
            requested_at=at,
        )
    )
    usage = ModelUsage(
        request_id="old",
        route_id="jev",
        model_version="jev",
        input_tokens=1,
        output_tokens=0,
        estimated_cost_usd="0.02",
        actual_cost_usd="0.02",
        billing_status="confirmed",
        recorded_at=at,
    )
    settled = await core.settle(old.reservation_id, usage)
    assert settled.daily_limit_usd == Decimal("1") and not settled.billing_frozen
    assert await core.settle(old.reservation_id, usage) == settled
    with core._connect() as db:
        from agent_platform.domain.costs import BudgetReservation

        saved = BudgetReservation.model_validate_json(
            db.execute("SELECT body FROM budget_requests WHERE request_id='old'").fetchone()[0]
        )
    assert saved.request == old.request


@pytest.mark.asyncio
async def test_new_request_cannot_expand_its_own_lower_cumulative_limit(tmp_path):
    core = await open_store(tmp_path / "fees.sqlite3")
    trials = SqlitePaperStore(core.path)
    parent = PaperTrialPolicy(**(continuous_data() | {"trial_total_usd": "0.1"}))
    await trials.ensure_trial(parent, NOW)
    await core.reserve(
        BudgetRequest(
            request_id="old",
            route_id="jev",
            purpose="advisory",
            price_version="verified",
            estimated_cost_usd="0.09",
            daily_limit_usd="0.1",
            requested_at=NOW,
        )
    )
    at = NOW + timedelta(days=1)
    await trials.ensure_trial(amendment(parent, at=at), at)
    with pytest.raises(BudgetExceeded):
        await core.reserve(
            BudgetRequest(
                request_id="lower-limit",
                route_id="jev",
                purpose="advisory",
                price_version="verified",
                estimated_cost_usd="0.02",
                daily_limit_usd="0.1",
                requested_at=at,
            )
        )

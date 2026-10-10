"""No timeout and no midnight reset of cumulative JEV spend."""

from datetime import timedelta
from decimal import Decimal

import pytest

from agent_platform.adapters.fake.clock import FakeClock
from agent_platform.adapters.sqlite import open_store
from agent_platform.adapters.sqlite.paper import SqlitePaperStore
from agent_platform.bootstrap_paper import PaperModelConfig, TrialDecisionModel
from agent_platform.domain.costs import BudgetRequest, ModelUsage
from agent_platform.ports.persistence import BudgetExceeded
from tests.application.test_decision_models import setup, valid_response
from tests.domain.test_paper_trading import NOW
from tests.test_paper_config import config_data


def continuous_data(*, at=NOW, grant_id=None):
    old = config_data()
    return old | {
        "issued_at": at,
        "expires_at": None,
        "grant_id": grant_id,
        "price": old["price"] | {"verified_at": at, "valid_until": None},
    }


def test_continuous_policy_and_price_have_no_expiry():
    policy = PaperModelConfig(**continuous_data())
    policy.validate_active(NOW + timedelta(days=365))
    assert policy.expires_at is None and policy.price.valid_until is None
    with pytest.raises(ValueError):
        policy.validate_active(NOW - timedelta(seconds=1))


@pytest.mark.asyncio
async def test_continuation_keeps_root_and_cross_day_exposure(tmp_path):
    path = tmp_path / "fees.sqlite3"
    core = await open_store(path)
    trials = SqlitePaperStore(path)
    root = PaperModelConfig(**config_data())
    await trials.ensure_trial(root, NOW)
    prior = await trials.load(root.budget_key)
    await core.reserve(
        BudgetRequest(
            request_id="old-confirmed",
            route_id="jev",
            purpose="advisory",
            price_version="old",
            estimated_cost_usd="0.04",
            daily_limit_usd="1",
            requested_at=NOW,
        )
    )
    reservation = await core.reserve(
        BudgetRequest(
            request_id="old-unknown",
            route_id="jev",
            purpose="advisory",
            price_version="old",
            estimated_cost_usd="0.05",
            daily_limit_usd="1",
            requested_at=NOW,
        )
    )
    first = await core.load(root.budget_key)
    assert first == prior
    # Unknown hold remains held; also confirm the other request's actual bill.
    with core._connect() as db:
        from agent_platform.domain.costs import BudgetReservation

        known = BudgetReservation.model_validate_json(
            db.execute(
                "SELECT body FROM budget_requests WHERE request_id='old-confirmed'"
            ).fetchone()[0]
        )
    await core.settle(
        known.reservation_id,
        ModelUsage(
            request_id="old-confirmed",
            route_id="jev",
            model_version="typesafe/jev-1.13",
            input_tokens=1,
            output_tokens=0,
            estimated_cost_usd="0.04",
            actual_cost_usd="0.04",
            billing_status="confirmed",
            recorded_at=NOW,
        ),
    )
    at = NOW + timedelta(hours=2)
    policy = PaperModelConfig(
        **continuous_data(at=at, grant_id="continuous") | {"trial_total_usd": "0.1"}
    )
    await trials.ensure_trial(policy, at)
    assert await trials.load(root.budget_key) == prior
    next_day = NOW + timedelta(days=1)
    status = await core.budget_balance(next_day, daily_limit_usd="10", cumulative=True)
    assert status.daily_limit_usd == Decimal("0.1")
    assert status.spent_usd == Decimal("0.04") and status.reserved_usd == Decimal("0.05")
    with pytest.raises(BudgetExceeded):
        await core.reserve(
            BudgetRequest(
                request_id="next-day-too-much",
                route_id="other",
                purpose="advisory",
                price_version="old",
                estimated_cost_usd="0.02",
                daily_limit_usd="10",
                requested_at=next_day,
            )
        )
    with core._connect() as db:
        held = BudgetReservation.model_validate_json(
            db.execute(
                "SELECT body FROM budget_requests WHERE reservation_id=?",
                (reservation.reservation_id,),
            ).fetchone()[0]
        )
        assert held.held_cost_usd == Decimal("0.05")
    # Cross-day concurrent reservations share one cumulative write lock.
    import asyncio

    def small_request(request_id):
        return BudgetRequest(
            request_id=request_id,
            route_id="other",
            purpose="advisory",
            price_version="old",
            estimated_cost_usd="0.006",
            daily_limit_usd="10",
            requested_at=next_day,
        )

    attempts = await asyncio.gather(
        core.reserve(small_request("small-a")),
        core.reserve(small_request("small-b")),
        return_exceptions=True,
    )
    assert sum(isinstance(value, BudgetExceeded) for value in attempts) == 1
    final = await core.budget_balance(next_day, daily_limit_usd="10", cumulative=True)
    assert final.reserved_usd == Decimal("0.056") and final.spent_usd == Decimal("0.04")


@pytest.mark.asyncio
async def test_steady_price_quote_and_status_work_after_midnight(tmp_path):
    from agent_platform.domain.decision_models import DecisionModelRequest
    from agent_platform.domain.routing import ModelPrice

    class Port:
        async def decide(self, request, quote):
            return valid_response(request, quote, at=request.captured_at)

    worker, store, old_request = await setup(tmp_path, port=Port())
    policy = PaperModelConfig(**continuous_data())
    worker.price = ModelPrice(**policy.price.model_dump() | {"version": worker.price.version})
    clock = FakeClock(old_request.captured_at + timedelta(days=2))
    worker.clock = clock
    request = DecisionModelRequest.model_validate(
        old_request.model_dump()
        | {
            "request_id": "later",
            "captured_at": clock.utcnow(),
            "deadline": clock.utcnow() + timedelta(seconds=10),
        }
    )
    model = TrialDecisionModel(worker, policy, clock)
    reply = await model.decide(request)
    assert reply.usage.actual_cost_usd == Decimal("0.0000084")
    view = await model.budget_status()
    assert view["active"] and view["expires_at"] is None and view["budget_scope"] == "cumulative"

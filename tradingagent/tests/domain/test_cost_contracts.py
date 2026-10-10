"""Usage is honest about unknown billing; budgets cannot silently erase exposure."""

import importlib
from datetime import UTC, date, datetime

import pytest

NOW = datetime(2026, 10, 5, tzinfo=UTC)


@pytest.fixture
def costs():
    return importlib.import_module("agent_platform.domain.costs")


def request_data():
    return {
        "request_id": "request-1",
        "route_id": "economy-1",
        "purpose": "advisory",
        "price_version": "user-price-1",
        "estimated_cost_usd": "0.6",
        "daily_limit_usd": "1",
        "hourly_call_limit": 60,
        "requested_at": NOW,
    }


def test_zero_budget_is_valid_but_never_implies_paid_authorization(costs):
    request = costs.BudgetRequest(**{**request_data(), "daily_limit_usd": "0"})
    assert not request.within_single_request_limit
    assert request.budget_day == date(2026, 10, 5)
    with pytest.raises(ValueError):
        costs.BudgetRequest(**{**request_data(), "hourly_call_limit": True})
    with pytest.raises(ValueError):
        costs.BudgetRequest(**{**request_data(), "estimated_cost_usd": 0.6})


def test_budget_day_uses_shanghai_not_the_utc_calendar(costs):
    request = costs.BudgetRequest(
        **{**request_data(), "requested_at": datetime(2026, 10, 4, 17, tzinfo=UTC)}
    )
    assert request.budget_day == date(2026, 10, 5)


def test_unknown_usage_does_not_claim_an_actual_bill(costs):
    base = {
        "request_id": "request-1",
        "route_id": "economy-1",
        "model_version": "fake-v1",
        "input_tokens": 10,
        "output_tokens": 0,
        "estimated_cost_usd": "0.6",
        "recorded_at": NOW,
    }
    usage = costs.ModelUsage(**base, billing_status="unknown")
    assert usage.actual_cost_usd is None
    with pytest.raises(ValueError):
        costs.ModelUsage(**base, billing_status="confirmed")
    with pytest.raises(ValueError):
        costs.ModelUsage(**base, billing_status="unknown", actual_cost_usd="0")


def test_unresolved_reservation_keeps_its_exposure(costs):
    reservation = costs.BudgetReservation(
        reservation_id="reservation-1", request=request_data(), status="unknown", updated_at=NOW
    )
    assert reservation.held_cost_usd == reservation.request.estimated_cost_usd
    with pytest.raises(ValueError):
        costs.BudgetReservation(**{**reservation.model_dump(), "status": "settled"})
    settled = costs.BudgetReservation(
        **{**reservation.model_dump(), "status": "settled", "actual_cost_usd": "0.4"}
    )
    assert settled.held_cost_usd == 0


def test_recording_an_overrun_requires_frozen_paid_calls(costs):
    base = {
        "budget_day": date(2026, 10, 5),
        "daily_limit_usd": "1",
        "spent_usd": "1.2",
        "reserved_usd": "0",
        "hourly_call_count": 1,
    }
    with pytest.raises(ValueError):
        costs.BudgetBalance(**base)
    assert costs.BudgetBalance(**base, billing_frozen=True).spent_usd > 1

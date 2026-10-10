"""Synthetic price versions; no supplier prices or paid requests are used."""

import importlib
from datetime import timedelta
from decimal import Decimal, localcontext

import pytest

from agent_platform.adapters.fake.clock import FakeClock
from agent_platform.domain.decisions import DecisionSnapshot
from tests.domain.test_decisions import NOW, snapshot_data


def modules():
    return (
        importlib.import_module("agent_platform.domain.routing"),
        importlib.import_module("agent_platform.application.routing"),
    )


def price():
    return dict(
        version="fixture-price-v1",
        input_usd_per_million="0.3",
        output_usd_per_million="1.2",
        verified_at=NOW - timedelta(hours=1),
        valid_until=NOW + timedelta(days=1),
    )


def tier(kind="economy", **changes):
    value = dict(
        kind=kind,
        model_version="fixture-model-v1",
        price=price(),
        max_output_tokens=100,
        max_single_cost_usd="1",
        prompt_overhead_tokens=100,
        enabled=True,
    )
    value.update(changes)
    return value


def router(*routes, daily="1"):
    domain, application = modules()
    return application.ModelRouter(
        domain.RoutingPolicy(routes=routes, daily_limit_usd=daily), FakeClock(NOW)
    )


def snapshot(**changes):
    value = snapshot_data()
    value.update(changes)
    return DecisionSnapshot(**value)


def test_default_policy_uses_rules_and_never_creates_a_paid_route():
    domain, application = modules()
    route = application.ModelRouter(domain.RoutingPolicy(), FakeClock(NOW)).select(
        snapshot(), "advisory"
    )
    assert route.kind == "rule" and not route.paid


@pytest.mark.parametrize(
    "change",
    [
        {"daily": "0"},
        {"route": {"price": None}},
        {"route": {"enabled": False}},
        {"route": {"price": dict(price(), valid_until=NOW)}},
        {"route": {"price": dict(price(), verified_at=NOW + timedelta(seconds=1))}},
    ],
)
def test_disabled_unknown_or_unverified_price_never_routes_paid(change):
    candidate = tier(**change.get("route", {}))
    route = router(candidate, daily=change.get("daily", "1")).select(snapshot(), "advisory")
    assert route.kind == "rule" and not route.paid


def test_economy_standard_review_are_explicit_configured_choices():
    value = router(tier("economy"), tier("standard"), tier("review"))
    assert value.select(snapshot(), "advisory").kind == "economy"
    data = snapshot_data()
    data["trigger"]["kind"] = "account_change"
    data["position"]["quantity"] = "1"
    assert value.select(DecisionSnapshot(**data), "advisory").kind == "standard"
    assert value.select(snapshot(), "review").kind == "review"


def test_cost_upper_bound_uses_all_bytes_overhead_and_maximum_output_tokens():
    value = router(tier())
    route = value.select(snapshot(), "advisory")
    quote = value.quote(route, b"x" * 200)
    assert quote.input_token_upper_bound == 300 and quote.max_output_tokens == 100
    assert quote.estimated_cost_usd == Decimal("0.00021")
    assert quote.price_version == "fixture-price-v1"
    with localcontext() as ambient:
        ambient.prec = 2
        ambient.Emax = 0
        assert value.quote(route, b"x" * 200).estimated_cost_usd == Decimal("0.00021")


def test_single_request_ceiling_rejects_quote_before_any_model_call():
    value = router(tier(max_single_cost_usd="0.000001"))
    with pytest.raises(ValueError):
        value.quote(value.select(snapshot(), "advisory"), b"x" * 200)


def test_router_does_not_fall_back_to_an_unconfigured_tier_or_upgrade_from_output():
    value = router(tier("economy"))
    data = snapshot_data()
    data["trigger"]["kind"] = "account_change"
    data["position"]["quantity"] = "1"
    assert value.select(DecisionSnapshot(**data), "advisory").kind == "rule"
    assert value.select(snapshot(), "review").kind == "rule"


@pytest.mark.parametrize(
    "change",
    [
        {"max_output_tokens": True},
        {"max_output_tokens": 8193},
        {"max_single_cost_usd": "-1"},
        {"prompt_overhead_tokens": -1},
        {"price": dict(price(), input_usd_per_million="1e200")},
    ],
)
def test_config_rejects_unsafe_limits_and_price_arithmetic(change):
    domain, _ = modules()
    with pytest.raises(ValueError):
        domain.RoutingPolicy(routes=(tier(**change),), daily_limit_usd="1")


def test_duplicate_tiers_and_unknown_prompt_size_are_not_silently_accepted():
    domain, _ = modules()
    with pytest.raises(ValueError):
        domain.RoutingPolicy(routes=(tier(), tier()), daily_limit_usd="1")
    value = router(tier())
    route = value.select(snapshot(), "advisory")
    with pytest.raises(ValueError):
        value.quote(route, b"x" * 65537)

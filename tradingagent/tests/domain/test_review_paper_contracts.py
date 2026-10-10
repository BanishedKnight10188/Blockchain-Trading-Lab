"""Review revisions preserve history; simulated facts cannot use a live namespace."""

import importlib
from datetime import UTC, datetime, timedelta

import pytest

NOW = datetime(2026, 10, 5, tzinfo=UTC)


def review_data():
    return {
        "review_id": "review-1",
        "trade_group_id": "group-1",
        "revision": 1,
        "kind": "initial",
        "data_cutoff": NOW,
        "generated_at": NOW,
        "evidence_ids": ("trade-1",),
        "explanation": "成本资料不全",
        "cost_status": "unknown",
    }


def test_unknown_cost_cannot_generate_an_invented_pnl():
    reviews = importlib.import_module("agent_platform.domain.reviews")
    original = reviews.ReviewRevision(**review_data())
    assert original.realized_pnl_usd is None
    assert not original.model_participated
    with pytest.raises(ValueError):
        reviews.ReviewRevision(**{**review_data(), "realized_pnl_usd": "0"})


def test_followup_preserves_parent_and_rejects_future_information():
    reviews = importlib.import_module("agent_platform.domain.reviews")
    original = reviews.ReviewRevision(**review_data())
    next_data = {
        **review_data(),
        "review_id": "review-2",
        "revision": 2,
        "kind": "followup",
        "data_cutoff": NOW + timedelta(hours=1),
        "generated_at": NOW + timedelta(hours=1),
    }
    with pytest.raises(ValueError):
        reviews.ReviewRevision(**next_data)
    followup = reviews.ReviewRevision(**next_data, parent_review_id=original.review_id)
    assert followup.is_retrospective
    assert original.revision == 1
    assert original.data_cutoff == NOW
    with pytest.raises(ValueError):
        reviews.ReviewRevision(**{**next_data, "parent_review_id": "review-1", "generated_at": NOW})


def test_review_job_identity_survives_serialization():
    reviews = importlib.import_module("agent_platform.domain.reviews")
    job = reviews.ReviewJob(
        job_id="job-1",
        trade_group_id="group-1",
        kind="followup",
        created_at=NOW,
        scheduled_for=NOW + timedelta(hours=1),
    )
    restored = reviews.ReviewJob.model_validate_json(job.model_dump_json())
    assert restored.identity == job.identity
    with pytest.raises(ValueError):
        reviews.ReviewJob(**{**job.model_dump(), "status": "done"})


def test_paper_intent_requires_explicit_mode_and_separate_account():
    paper = importlib.import_module("agent_platform.domain.paper")
    base = {
        "intent_id": "intent-1",
        "account_ref": "paper:offline-spot",
        "symbol": "BTCUSDT",
        "side": "buy",
        "quantity": "0.001",
        "created_at": NOW,
    }
    with pytest.raises(ValueError):
        paper.PaperIntent(**base)
    intent = paper.PaperIntent(**base, mode="paper")
    assert intent.account_ref.startswith("paper:")
    for changes in ({"mode": "read_only"}, {"account_ref": "live-spot"}, {"quantity": 0.001}):
        with pytest.raises(ValueError):
            paper.PaperIntent(**{**intent.model_dump(), **changes})


def test_paper_fill_exposes_costs_and_simulator_version():
    paper = importlib.import_module("agent_platform.domain.paper")
    fill = paper.PaperFill(
        fill_id="fill-1",
        intent_id="intent-1",
        mode="paper",
        account_ref="paper:offline-spot",
        symbol="BTCUSDT",
        side="buy",
        quantity="0.001",
        price="60000",
        fee="0.06",
        fee_asset="USDT",
        slippage_bps="1",
        model_version="fixture-fill-v1",
        filled_at=NOW,
    )
    assert paper.PaperFill.model_validate_json(fill.model_dump_json()) == fill
    with pytest.raises(ValueError):
        paper.PaperFill(**{**fill.model_dump(), "account_ref": "live-spot"})


@pytest.mark.parametrize(
    "model", ["AccountSnapshot", "ObservedTrade", "ObservedOrder", "TradeBatch", "TradeAttribution"]
)
def test_paper_namespace_cannot_enter_actual_exchange_facts(model):
    account = importlib.import_module("agent_platform.domain.account")
    reviews = importlib.import_module("agent_platform.domain.reviews")
    values = {
        "AccountSnapshot": {"as_of": NOW},
        "ObservedTrade": {
            "symbol": "BTCUSDT",
            "trade_id": "1",
            "order_id": "1",
            "side": "buy",
            "price": "60000",
            "quantity": "0.001",
            "fee": "0",
            "fee_asset": "USDT",
            "executed_at": NOW,
        },
        "ObservedOrder": {
            "symbol": "BTCUSDT",
            "order_id": "1",
            "side": "buy",
            "status": "new",
            "price": "60000",
            "quantity": "0.001",
            "filled_quantity": "0",
            "updated_at": NOW,
        },
        "TradeBatch": {"symbol": "BTCUSDT"},
        "TradeAttribution": {"symbol": "BTCUSDT", "trade_id": "1"},
    }
    model_type = getattr(reviews if model == "TradeAttribution" else account, model)
    with pytest.raises(ValueError):
        model_type(**values[model], account_ref="paper:offline-spot")

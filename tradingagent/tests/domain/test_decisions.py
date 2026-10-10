"""Frozen evidence, explicit unavailable advice, and forward-only recommendation state."""

import importlib
from datetime import UTC, datetime, timedelta

import pytest

NOW = datetime(2026, 10, 5, tzinfo=UTC)


@pytest.fixture
def decisions():
    return importlib.import_module("agent_platform.domain.decisions")


def snapshot_data():
    return {
        "snapshot_id": "snapshot-1",
        "session_id": "session-1",
        "session_revision": 1,
        "style_revision": 1,
        "style": {"strength": 67},
        "captured_at": NOW,
        "market": {"symbol": "BTCUSDT", "as_of": NOW, "status": "warming"},
        "features": {"symbol": "BTCUSDT", "as_of": NOW, "snapshot_id": "features-1"},
        "account": {"account_ref": "local-spot", "as_of": NOW, "status": "unavailable"},
        "position": {"symbol": "BTCUSDT", "quantity": "0", "cost_status": "unknown"},
        "trigger": {
            "trigger_id": "trigger-1",
            "session_id": "session-1",
            "kind": "periodic",
            "requested_at": NOW,
            "expires_at": NOW + timedelta(seconds=60),
        },
        "evidence_ids": ("market-1", "features-1", "account-1"),
    }


def recommendation_data():
    return {
        "recommendation_id": "advice-1",
        "snapshot_id": "snapshot-1",
        "session_id": "session-1",
        "style_revision": 1,
        "account_revision": 0,
        "assessment": {"action": "hold", "explanation": "明确的测试规则", "source": "fake"},
        "created_at": NOW,
        "updated_at": NOW,
        "expires_at": NOW + timedelta(seconds=60),
    }


def test_decision_snapshot_round_trip_keeps_missing_data_and_style(decisions):
    snapshot = decisions.DecisionSnapshot(**snapshot_data())
    restored = decisions.DecisionSnapshot.model_validate_json(snapshot.model_dump_json())
    assert restored == snapshot
    assert restored.style.strength == 67
    assert restored.features.ema_fast is None
    assert restored.position.average_cost is None


@pytest.mark.parametrize("field", ["market", "features", "account"])
def test_decision_snapshot_rejects_future_context(decisions, field):
    data = snapshot_data()
    data[field]["as_of"] = NOW + timedelta(seconds=1)
    with pytest.raises(ValueError):
        decisions.DecisionSnapshot(**data)


@pytest.mark.parametrize("field", ["market", "features", "position"])
def test_decision_snapshot_rejects_other_symbol(decisions, field):
    data = snapshot_data()
    data[field]["symbol"] = "ETHUSDT"
    with pytest.raises(ValueError):
        decisions.DecisionSnapshot(**data)


def test_decision_snapshot_rejects_another_session_trigger(decisions):
    data = snapshot_data()
    data["trigger"]["session_id"] = "session-2"
    with pytest.raises(ValueError):
        decisions.DecisionSnapshot(**data)


def test_unavailable_advice_requires_reason_and_cannot_claim_quantity(decisions):
    with pytest.raises(ValueError):
        decisions.AdvisoryAssessment(action="unavailable", explanation="缺数据", source="rule")
    result = decisions.AdvisoryAssessment(
        action="unavailable", explanation="缺数据", source="rule", unavailable_reasons=("warming",)
    )
    assert result.quantity is None
    with pytest.raises(ValueError):
        decisions.AdvisoryAssessment(**{**result.model_dump(), "quantity": "0.1"})


def test_buy_advice_requires_evidence_but_does_not_invent_position_size(decisions):
    with pytest.raises(ValueError):
        decisions.AdvisoryAssessment(action="buy", explanation="测试", source="fake")
    result = decisions.AdvisoryAssessment(
        action="buy", explanation="测试", source="fake", evidence_ids=("features-1",)
    )
    assert result.quantity is None
    with pytest.raises(ValueError):
        decisions.AdvisoryAssessment(**{**result.model_dump(), "quantity": 0.1})


def test_recommendation_moves_forward_and_preserves_original_author(decisions):
    original = decisions.Recommendation(**recommendation_data())
    published = original.transition("published", NOW + timedelta(seconds=1))
    accepted = published.transition("accepted", NOW + timedelta(seconds=2))
    assert original.status == "created"
    assert accepted.original_author == "agent"
    with pytest.raises(ValueError):
        accepted.transition("published", NOW + timedelta(seconds=3))
    with pytest.raises(ValueError):
        decisions.Recommendation(**{**recommendation_data(), "original_author": "human"})


def test_recommendation_cannot_publish_after_expiry_or_skip_publication(decisions):
    original = decisions.Recommendation(**recommendation_data())
    with pytest.raises(ValueError):
        original.transition("published", NOW + timedelta(seconds=60))
    with pytest.raises(ValueError):
        original.transition("accepted", NOW + timedelta(seconds=1))
    with pytest.raises(ValueError):
        original.transition("expired", NOW + timedelta(seconds=1))
    assert original.transition("expired", NOW + timedelta(seconds=60)).status == "expired"


def test_modified_feedback_is_separate_and_explicit(decisions):
    base = {"feedback_id": "feedback-1", "recorded_at": NOW, "explanation": "本人调整"}
    with pytest.raises(ValueError):
        decisions.DecisionFeedback(**base, kind="modified", recommendation_id="advice-1")
    feedback = decisions.DecisionFeedback(
        **base,
        kind="modified",
        recommendation_id="advice-1",
        modified_assessment={"action": "hold", "explanation": "本人选择等待", "source": "human"},
    )
    assert feedback.final_decision_maker == "human"
    with pytest.raises(ValueError):
        decisions.DecisionFeedback(**base, kind="independent", recommendation_id="advice-1")


def test_published_decision_result_requires_current_advice_and_allowed_risk(decisions):
    recommendation = decisions.Recommendation(**recommendation_data())
    base = {
        "request_id": "request-1",
        "snapshot_id": "snapshot-1",
        "status": "published",
        "risk": {"snapshot_id": "snapshot-1", "outcome": "allow", "evaluated_at": NOW},
    }
    with pytest.raises(ValueError):
        decisions.DecisionResult(**base, recommendation=recommendation)
    published = recommendation.transition("published", NOW)
    assert decisions.DecisionResult(**base, recommendation=published).recommendation == published
    with pytest.raises(ValueError):
        decisions.DecisionResult(
            **{
                **base,
                "risk": {
                    "snapshot_id": "snapshot-1",
                    "outcome": "block",
                    "evaluated_at": NOW,
                    "reasons": ("market_stale",),
                },
            },
            recommendation=published,
        )

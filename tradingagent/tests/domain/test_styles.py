"""Continuous style is explicit, exact, immutable and versioned."""

import importlib
import importlib.util
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext

import pytest


@pytest.fixture
def sessions():
    assert importlib.util.find_spec("agent_platform.domain.sessions") is not None
    return importlib.import_module("agent_platform.domain.sessions")


@pytest.mark.parametrize("strength", [0, 1, 33, 34, 50, 66, 67, 99, 100])
def test_every_integer_style_is_preserved(sessions, strength):
    style = sessions.TradingStyle(strength=strength)
    restored = sessions.TradingStyle.model_validate_json(style.model_dump_json())
    assert restored.strength == strength
    assert restored.context.aggression_weight == Decimal(strength) / Decimal(100)
    assert restored.context.conservatism_weight + restored.context.aggression_weight == 1
    assert restored.context.strength == strength


@pytest.mark.parametrize("strength", [-1, 101, 1.5, 50.0, True, False, "50", None])
def test_style_rejects_coercion_and_out_of_range(sessions, strength):
    with pytest.raises(ValueError):
        sessions.TradingStyle(strength=strength)


def test_style_has_no_silent_default_or_risk_override(sessions):
    with pytest.raises(ValueError):
        sessions.TradingStyle()
    with pytest.raises(ValueError):
        sessions.TradingStyle(strength=100, max_position="1")
    style = sessions.TradingStyle(strength=71)
    with pytest.raises(ValueError):
        style.strength = 2


@pytest.mark.parametrize("strength,label", [(0, "偏保守"), (50, "均衡"), (100, "偏激进")])
def test_display_labels_do_not_replace_strength(sessions, strength, label):
    style = sessions.TradingStyle(strength=strength)
    assert style.label == label
    assert style.model_dump(mode="json")["strength"] == strength


def test_old_style_snapshot_is_invalid_after_revision(sessions):
    now = datetime(2026, 10, 5, tzinfo=UTC)
    original = sessions.AgentSession(
        session_id="session-1",
        style={"strength": 27},
        created_at=now,
        updated_at=now,
    )
    changed = original.change_style(sessions.TradingStyle(strength=72), now)
    assert original.style.strength == 27
    assert changed.style.strength == 72
    assert changed.revision == 2
    assert changed.style_revision == 2
    assert not changed.matches_style_revision(original.style_revision)
    assert changed.matches_style_revision(changed.style_revision)


def test_same_style_does_not_create_a_new_version(sessions):
    now = datetime(2026, 10, 5, tzinfo=UTC)
    original = sessions.AgentSession(
        session_id="session-1",
        style={"strength": 50},
        created_at=now,
        updated_at=now,
    )
    assert original.change_style(sessions.TradingStyle(strength=50), now) == original


def test_style_weights_are_exact_under_low_ambient_precision(sessions):
    with localcontext() as context:
        context.prec = 1
        weights = sessions.TradingStyle(strength=67).context
        assert weights.aggression_weight == Decimal("0.67")
        assert weights.conservatism_weight == Decimal("0.33")


def test_style_context_rejects_rounded_weights_under_low_precision(sessions):
    with localcontext() as context:
        context.prec = 1
        with pytest.raises(ValueError):
            sessions.StyleContext(strength=67, aggression_weight="0.7", conservatism_weight="0.3")


def test_session_lifecycle_is_explicit_and_revisioned(sessions):
    now = datetime(2026, 10, 5, tzinfo=UTC)
    original = sessions.AgentSession(
        session_id="session-1", style={"strength": 67}, created_at=now, updated_at=now
    )
    running = original.transition("running", now)
    paused = running.transition("paused", now)
    resumed = paused.transition("running", now)
    closed = resumed.transition("closed", now)
    assert original.status == "configured"
    assert closed.revision == 5
    assert closed.style_revision == 1
    with pytest.raises(ValueError):
        closed.transition("running", now)
    with pytest.raises(ValueError):
        original.transition("paused", now)


def test_style_change_cannot_move_time_back_before_latest_session_change(sessions):
    now = datetime(2026, 10, 5, tzinfo=UTC)
    original = sessions.AgentSession(
        session_id="session-1", style={"strength": 67}, created_at=now, updated_at=now
    )
    changed = original.change_style(sessions.TradingStyle(strength=70), now + timedelta(seconds=10))
    with pytest.raises(ValueError):
        changed.change_style(sessions.TradingStyle(strength=71), now + timedelta(seconds=5))

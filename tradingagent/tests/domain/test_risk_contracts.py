"""Risk outcomes are explicit and limits never come from trading style."""

import importlib
from datetime import UTC, datetime

import pytest


@pytest.fixture
def risk():
    return importlib.import_module("agent_platform.domain.risk")


def test_unconfigured_limits_cannot_imply_a_quantity(risk):
    limits = risk.DisciplineLimits()
    assert limits.max_buy_quantity is None
    assert limits.max_position_quantity is None
    with pytest.raises(ValueError):
        risk.DisciplineLimits(strength=100)
    with pytest.raises(ValueError):
        risk.DisciplineLimits(max_buy_quantity="-1")


@pytest.mark.parametrize("outcome", ["block", "unavailable"])
def test_blocked_or_unavailable_risk_requires_a_reason(risk, outcome):
    data = {"snapshot_id": "snapshot-1", "evaluated_at": datetime(2026, 10, 5, tzinfo=UTC)}
    with pytest.raises(ValueError):
        risk.RiskAssessment(**data, outcome=outcome)
    assessment = risk.RiskAssessment(**data, outcome=outcome, reasons=("market_stale",))
    assert not assessment.permits_advice


def test_allow_has_no_blocking_reasons(risk):
    data = {"snapshot_id": "snapshot-1", "evaluated_at": datetime(2026, 10, 5, tzinfo=UTC)}
    assert risk.RiskAssessment(**data, outcome="allow").permits_advice
    with pytest.raises(ValueError):
        risk.RiskAssessment(**data, outcome="allow", reasons=("market_stale",))

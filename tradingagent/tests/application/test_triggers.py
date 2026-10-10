"""Strategy thresholds are explicit test settings; missing data produces rule alerts."""

import importlib
from datetime import timedelta
from decimal import Decimal

import pytest

from agent_platform.domain.sessions import AgentSession
from tests.application.test_risk import snapshot
from tests.domain.test_decisions import NOW


def module():
    return importlib.import_module("agent_platform.application.triggers")


def session(strength=50):
    return AgentSession(
        session_id="session-1", style={"strength": strength}, created_at=NOW, updated_at=NOW
    ).transition("running", NOW)


def test_300_quiet_feature_samples_with_no_configured_signal_do_not_emit_model_events():
    service = module().TriggerService()
    for index in range(300):
        features = snapshot().features.model_copy(
            update={"as_of": NOW + timedelta(seconds=index), "snapshot_id": f"features-{index}"}
        )
        assert service.evaluate(features, session()) == ()
    assert service.sample_count == 300


def test_missing_features_raise_one_immediate_rule_alert_until_quality_changes():
    service = module().TriggerService()
    features = snapshot().features.model_copy(update={"warmup_ready": False})
    alerts = service.evaluate(features, session())
    assert len(alerts) == 1 and alerts[0].kind == "hard_risk"
    assert service.evaluate(features, session()) == ()
    assert service.evaluate(snapshot().features, session()) == ()
    assert len(service.evaluate(features, session())) == 1


def test_explicit_signal_threshold_emits_only_a_crossing_and_never_scales_hard_limits():
    service = module().TriggerService(return_threshold="0.02", policy_version="test-return-v1")
    features = snapshot().features
    assert service.evaluate(features, session(0)) == ()
    high = features.model_copy(update={"interval_return": Decimal("0.03")})
    # Production feature DTOs are validated before use.
    high = type(features).model_validate_json(high.model_dump_json())
    assert service.evaluate(high, session(100))[0].kind == "market_change"
    assert service.evaluate(high, session(100)) == ()


def test_unversioned_threshold_or_terminated_session_is_rejected_or_silent():
    with pytest.raises(ValueError):
        module().TriggerService(return_threshold="0.02")
    closed = session().transition("paused", NOW).transition("closed", NOW)
    assert module().TriggerService().evaluate(snapshot().features, closed) == ()

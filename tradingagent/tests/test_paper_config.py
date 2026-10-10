"""Explicit local paid trial caps and expiry; no implicit next-day renewal."""

import importlib
from datetime import timedelta

import pytest
from pydantic import ValidationError

from agent_platform.config import RuntimeConfig
from tests.domain.test_paper_trading import NOW


def config_data():
    return dict(
        trial_total_usd="1",
        single_call_usd="0.02",
        issued_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        price=dict(
            version="jev-price-20261006",
            input_usd_per_million="0.042",
            output_usd_per_million="0",
            verified_at=NOW,
            valid_until=NOW + timedelta(hours=2),
        ),
    )


@pytest.mark.parametrize(
    "updates",
    [
        {"trial_total_usd": "0"},
        {"single_call_usd": "0.021"},
        {"trial_total_usd": "0.01"},
        {"expires_at": NOW + timedelta(days=1)},
        {"expires_at": NOW + timedelta(hours=3)},
        {"api_key": "never-in-config"},
    ],
)
def test_paid_configuration_rejects_missing_or_unsafe_limits(updates):
    module = importlib.import_module("agent_platform.bootstrap_paper")
    with pytest.raises(ValidationError):
        module.PaperModelConfig(**(config_data() | updates))


def test_trial_expiry_cannot_roll_over_on_restart():
    module = importlib.import_module("agent_platform.bootstrap_paper")
    configured = module.PaperModelConfig(**config_data())
    configured.validate_active(NOW)
    with pytest.raises(ValueError):
        configured.validate_active(NOW + timedelta(days=1))


@pytest.mark.parametrize(
    "values",
    [
        {"paper_mock": True},
        {"paper": True},
        {"paper": True, "paper_mock": True, "paper_model_config": "local.json"},
        {"paper": True, "paper_model_config": "local.json"},
    ],
)
def test_runtime_requires_explicit_source_and_real_public_quotes(values):
    with pytest.raises(ValidationError):
        RuntimeConfig(**values)


def test_mock_paper_is_opt_in_and_accepts_auto_paper():
    assert RuntimeConfig(
        paper=True, paper_mock=True, operation={"mode": "auto", "execution_environment": "paper"}
    ).paper

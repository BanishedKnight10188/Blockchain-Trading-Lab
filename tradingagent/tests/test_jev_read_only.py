"""No paid dispatch or trial renewal when loading an expired local wallet."""

from datetime import timedelta
from decimal import Decimal

import pytest

from agent_platform.adapters.fake.clock import FakeClock
from agent_platform.adapters.sqlite import open_store
from agent_platform.adapters.sqlite.paper import SqlitePaperStore
from agent_platform.bootstrap import build_application_services
from agent_platform.bootstrap_paper import PaperModelConfig, TrialDecisionModel, prepare_paper
from agent_platform.config import RuntimeConfig, RuntimeConfigurationError
from agent_platform.domain.costs import BudgetRequest
from agent_platform.ports.model import ModelCallFailed
from tests.application.test_decision_models import setup, valid_response
from tests.domain.test_paper_trading import NOW
from tests.test_paper_config import config_data


def test_expired_policy_requires_explicit_read_only(tmp_path):
    path = tmp_path / "trial.json"
    path.write_text(PaperModelConfig(**config_data()).model_dump_json(), encoding="utf-8")
    clock = FakeClock(NOW + timedelta(hours=1))
    config = RuntimeConfig(paper=True, live_public=True, paper_model_config=path)
    with pytest.raises(RuntimeConfigurationError):
        prepare_paper(config, clock, environment={"OPENROUTER_API_KEY": "sk-or-v1-offline-key"})
    read_only = RuntimeConfig(**config.model_dump() | {"paper_read_only": True})
    policy, _ = prepare_paper(
        read_only, clock, environment={"OPENROUTER_API_KEY": "sk-or-v1-offline-key"}
    )
    assert policy.expires_at == clock.utcnow()


@pytest.mark.parametrize("values", [{}, {"paper": True, "paper_mock": True}])
def test_read_only_requires_real_paper_source(values):
    with pytest.raises(ValueError):
        RuntimeConfig(**values, paper_read_only=True)


@pytest.mark.asyncio
async def test_read_only_blocks_enabled_valid_grant_before_reserving(tmp_path):
    class ForbiddenPort:
        async def decide(self, *args):
            pytest.fail("read-only must never dispatch")

    worker, store, request = await setup(tmp_path, port=ForbiddenPort())
    model = TrialDecisionModel(
        worker, PaperModelConfig(**config_data()), FakeClock(NOW), read_only=True
    )
    assert not model.enabled
    with pytest.raises(ValueError, match="paper model is read-only"):
        model.set_enabled(True)
    with pytest.raises(ModelCallFailed, match="model_read_only"):
        await model.decide(request)
    balance = await store.budget_balance(NOW, daily_limit_usd="1")
    assert balance.hourly_call_count == 0 and balance.reserved_usd == 0


@pytest.mark.asyncio
async def test_expired_read_only_assembly_preserves_shared_fee_and_policy(tmp_path, monkeypatch):
    from agent_platform.runtime.read_only import ReadOnlyRuntime

    async def no_network(self):
        pass

    monkeypatch.setattr(ReadOnlyRuntime, "start", no_network)
    monkeypatch.setattr(ReadOnlyRuntime, "stop", no_network)
    monkeypatch.setattr(
        "agent_platform.bootstrap.SystemClock", lambda: FakeClock(NOW + timedelta(hours=1))
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-offline-key")
    shared_path = tmp_path / "fees.sqlite3"
    shared = await open_store(shared_path)
    policy = PaperModelConfig(**config_data())
    store = SqlitePaperStore(shared_path)
    await store.ensure_trial(policy, NOW)
    await shared.reserve(
        BudgetRequest(
            request_id="old-held",
            route_id="old",
            purpose="advisory",
            price_version="old",
            estimated_cost_usd="0.03",
            daily_limit_usd="1",
            requested_at=NOW,
        )
    )
    prior = await store.load(policy.budget_key)
    path = tmp_path / "trial.json"
    path.write_text(policy.model_dump_json(), encoding="utf-8")
    config = RuntimeConfig(
        paper=True,
        live_public=True,
        market_archive=False,
        paper_model_config=path,
        model_budget_database=shared_path,
        paper_read_only=True,
    )
    async with build_application_services(tmp_path / "wallet.sqlite3", config) as app:
        view = await app.futures_trading.public_view()
        assert view["model_read_only"] and not view["paid_models_enabled"]
        assert view["model_budget"]["read_only"] and not view["model_budget"]["active"]
        assert view["model_budget"]["balance"]["reserved_usd"] == "0.03"
    assert await store.load(policy.budget_key) == prior
    # A static-valid but different configuration cannot replace the recorded policy.
    path.write_text(
        PaperModelConfig(**config_data() | {"trial_total_usd": "0.5"}).model_dump_json(),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeConfigurationError):
        async with build_application_services(tmp_path / "other.sqlite3", config):
            pytest.fail("mismatched policy must not load")


@pytest.mark.asyncio
async def test_budget_settlement_retains_safe_diagnostic(tmp_path):
    from agent_platform.domain.model_diagnostics import ModelDiagnostic

    class InvalidPort:
        async def decide(self, request, quote):
            usage = valid_response(request, quote).usage
            raise ModelCallFailed(
                "invalid_model_assessment",
                usage,
                ModelDiagnostic(stage="criteria", question_index=0),
            )

    worker, store, request = await setup(tmp_path, port=InvalidPort())
    with pytest.raises(ModelCallFailed) as caught:
        await worker.decide(request)
    assert caught.value.diagnostic.stage == "criteria"
    balance = await store.budget_balance(request.captured_at, daily_limit_usd="1")
    assert balance.spent_usd == Decimal("0.0000084") and balance.reserved_usd == 0

"""Default key isolation and explicit mock service lifecycle."""

from datetime import timedelta
from decimal import Decimal

import httpx
import pytest

from agent_platform.adapters.openrouter.jev import OpenRouterDecisionModel
from agent_platform.adapters.openrouter.transport import OpenRouterClient, OpenRouterCredentials
from agent_platform.application.decision_models import BudgetedDecisionModel
from agent_platform.bootstrap import build_application_services
from agent_platform.config import RuntimeConfig
from agent_platform.domain.model_modules import JevModuleSettings
from tests.adapters.test_paper_store import context as _context
from tests.application.test_paper_trading import service
from tests.domain.test_paper_trading import NOW
from tests.test_paper_config import config_data

context = _context


@pytest.mark.asyncio
async def test_real_assembly_owns_client_and_starts_without_dispatch(tmp_path, monkeypatch):
    from agent_platform.adapters.fake.clock import FakeClock
    from agent_platform.bootstrap_paper import PaperModelConfig
    from agent_platform.runtime.read_only import ReadOnlyRuntime

    monkeypatch.setattr("agent_platform.bootstrap.SystemClock", lambda: FakeClock(NOW))
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-offline-paper-key")

    async def no_network(self):
        pass

    monkeypatch.setattr(ReadOnlyRuntime, "start", no_network)
    monkeypatch.setattr(ReadOnlyRuntime, "stop", no_network)
    path = tmp_path / "trial.json"
    path.write_text(PaperModelConfig(**config_data()).model_dump_json(), encoding="utf-8")
    configured = RuntimeConfig(
        paper=True,
        paper_model_config=path,
        live_public=True,
        market_archive=False,
        operation={"mode": "auto", "execution_environment": "paper"},
    )
    async with build_application_services(
        tmp_path / "real.sqlite3", configured, clock=FakeClock(NOW)
    ) as services:
        assert services.paper.decision_source == "real_jev"
        assert not services.paper.model.enabled
        assert services.paper.model.model.hourly_call_limit == 60
        assert services.futures_trading.model.model.hourly_call_limit == 3600
        assert services.futures_trading.model is not services.paper.model
        assert services.futures_trading.model.model.budgets is services.paper.model.model.budgets
        client = services.paper.model.model.port.client
        assert not client._client.is_closed
        history_client = services.initial_analysis.history
        chart_client = services.initial_analysis.chart_provider
        assert chart_client is not history_client, "chart browsing must not block model history"
        assert not chart_client._client.is_closed
    assert client._client.is_closed
    assert history_client._client.is_closed and chart_client._client.is_closed


@pytest.mark.asyncio
async def test_default_does_not_assemble_paper_or_read_openrouter_key(tmp_path, monkeypatch):
    import os

    original = os.environ.get

    def checked(key, *args):
        assert key != "OPENROUTER_API_KEY"
        return original(key, *args)

    monkeypatch.setattr(os.environ, "get", checked)
    async with build_application_services(tmp_path / "default.sqlite3") as services:
        assert services.paper is None


@pytest.mark.asyncio
async def test_separate_wallet_uses_shared_existing_budget_and_exposes_it(tmp_path, monkeypatch):
    from agent_platform.adapters.fake.clock import FakeClock
    from agent_platform.adapters.sqlite import open_store
    from agent_platform.bootstrap_paper import PaperModelConfig
    from agent_platform.domain.costs import BudgetRequest
    from agent_platform.runtime.read_only import ReadOnlyRuntime

    monkeypatch.setattr("agent_platform.bootstrap.SystemClock", lambda: FakeClock(NOW))
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-offline-paper-key")

    async def no_network(self):
        pass

    monkeypatch.setattr(ReadOnlyRuntime, "start", no_network)
    monkeypatch.setattr(ReadOnlyRuntime, "stop", no_network)
    budget_path = tmp_path / "shared.sqlite3"
    shared = await open_store(budget_path)
    await shared.reserve(
        BudgetRequest(
            request_id="old-fee",
            route_id="old",
            purpose="advisory",
            price_version="old",
            estimated_cost_usd="0.03",
            daily_limit_usd="1",
            requested_at=NOW,
        )
    )
    config_path = tmp_path / "trial.json"
    config_path.write_text(PaperModelConfig(**config_data()).model_dump_json(), encoding="utf-8")
    config = RuntimeConfig(
        paper=True,
        live_public=True,
        market_archive=False,
        paper_model_config=config_path,
        model_budget_database=budget_path,
    )
    async with build_application_services(
        tmp_path / "wallet.sqlite3", config, clock=FakeClock(NOW)
    ) as app:
        view = await app.futures_trading.public_view()
        assert view["model_budget"]["balance"]["reserved_usd"] == "0.03"
        local = await open_store(tmp_path / "wallet.sqlite3")
        assert (await local.budget_balance(NOW, daily_limit_usd="1")).reserved_usd == 0


@pytest.mark.asyncio
async def test_explicit_mock_paper_worker_is_owned_and_starts_paused(tmp_path):
    config = RuntimeConfig(
        paper=True, paper_mock=True, operation={"mode": "auto", "execution_environment": "paper"}
    )
    async with build_application_services(tmp_path / "paper.sqlite3", config) as services:
        assert services.paper is not None
        view = await services.paper.public_view()
        assert view["decision_source"] == "offline_mock" and view["paid_models_enabled"] is False
        assert view["account"] is None
        assert any(w.name == "paper" for w in (await services.runtime.health()).workers)
    assert not services.runtime.running


@pytest.mark.asyncio
async def test_real_adapter_mock_http_records_budget_then_expiry_blocks_calls(context):
    from agent_platform.bootstrap_paper import PaperModelConfig, TrialDecisionModel

    svc, clock, _, _ = service(context)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "id": "paper-http-mock",
                "model": "typesafe/jev-1.13-20260917",
                "provider": "TypeSafe",
                "answers": {
                    "action": {
                        "type": "choice",
                        "choice": "BUY",
                        "probabilities": {"BUY": 0.8, "SELL": 0.1, "WAIT": 0.1},
                        "confidence": 0.9,
                    }
                },
                "usage": {"input_tokens": 200, "output_tokens": 0, "cost": 0.0000084},
            },
        )

    trial = PaperModelConfig(**config_data())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = OpenRouterClient(
            clock,
            credentials=OpenRouterCredentials(api_key="sk-or-v1-offline-paper-key"),
            enabled=True,
            client=http,
        )
        budgeted = BudgetedDecisionModel(
            port=OpenRouterDecisionModel(client, clock),
            budgets=context[3],
            clock=clock,
            price=trial.price,
            daily_limit_usd=trial.trial_total_usd,
            max_single_cost_usd=trial.single_call_usd,
            settings=JevModuleSettings(enabled=False),
        )
        svc.model = TrialDecisionModel(budgeted, trial, clock)
        svc.price_version = trial.price.version
        account = await svc.store.latest()
        await svc.start(account.account_ref, account.revision)
        cycle = await svc.step()
        assert cycle.status == "filled" and cycle.usage.actual_cost_usd == Decimal("0.0000084")
        balance = await context[3].budget_balance(NOW, daily_limit_usd=trial.trial_total_usd)
        assert balance.spent_usd == Decimal("0.0000084")
        clock.advance_to(NOW + timedelta(hours=1))
        await svc.step()
        assert len(calls) == 1
        assert calls[0].url.path == "/api/alpha/decisions"


@pytest.mark.asyncio
async def test_mock_http_never_exceeds_cumulative_or_single_ceiling(context):
    from agent_platform.bootstrap_paper import PaperModelConfig, TrialDecisionModel

    svc, clock, _, _ = service(context)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "id": "paper-http-cap",
                "model": "typesafe/jev-1.13",
                "provider": "TypeSafe",
                "answers": {
                    "action": {
                        "type": "choice",
                        "choice": "WAIT",
                        "probabilities": {"BUY": 0.1, "SELL": 0.1, "WAIT": 0.8},
                        "confidence": 0.9,
                    }
                },
                "usage": {"input_tokens": 200, "output_tokens": 0, "cost": 0.0000084},
            },
        )

    values = config_data() | {"trial_total_usd": "0.000001", "single_call_usd": "0.000001"}
    trial = PaperModelConfig(**values)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = OpenRouterClient(
            clock,
            credentials=OpenRouterCredentials(api_key="sk-or-v1-offline-paper-key"),
            enabled=True,
            client=http,
        )
        svc.model = TrialDecisionModel(
            BudgetedDecisionModel(
                port=OpenRouterDecisionModel(client, clock),
                budgets=context[3],
                clock=clock,
                price=trial.price,
                daily_limit_usd=trial.trial_total_usd,
                max_single_cost_usd=trial.single_call_usd,
                settings=JevModuleSettings(enabled=False),
            ),
            trial,
            clock,
        )
        svc.price_version = trial.price.version
        account = await svc.store.latest()
        await svc.start(account.account_ref, account.revision)
        assert (await svc.step()).status == "unavailable"
        assert not calls


@pytest.mark.asyncio
async def test_shared_existing_reservation_exhausts_trial_before_http(context):
    from agent_platform.bootstrap_paper import PaperModelConfig, TrialDecisionModel
    from agent_platform.domain.costs import BudgetRequest

    svc, clock, _, _ = service(context)
    trial = PaperModelConfig(**config_data())
    await context[3].reserve(
        BudgetRequest(
            request_id="another-module-reserve",
            route_id="another-route",
            purpose="advisory",
            price_version=trial.price.version,
            estimated_cost_usd="1",
            daily_limit_usd="1",
            requested_at=NOW,
        )
    )

    class ForbiddenPort:
        async def decide(self, *args):
            pytest.fail("durable total budget must reject before HTTP dispatch")

    svc.model = TrialDecisionModel(
        BudgetedDecisionModel(
            port=ForbiddenPort(),
            budgets=context[3],
            clock=clock,
            price=trial.price,
            daily_limit_usd=trial.trial_total_usd,
            max_single_cost_usd=trial.single_call_usd,
            settings=JevModuleSettings(enabled=False),
        ),
        trial,
        clock,
    )
    svc.price_version = trial.price.version
    account = await svc.store.latest()
    await svc.start(account.account_ref, account.revision)
    assert (await svc.step()).status == "unavailable"
    assert (await svc.store.latest()).usdt == 1000

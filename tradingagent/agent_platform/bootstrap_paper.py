"""Explicit local Paper assembly and expiring real-model trial configuration."""

import json
import os
from decimal import Decimal

from agent_platform.adapters.fake.paper_trading import DemoPaperMarket, MockPaperDecisionModel
from agent_platform.adapters.paper.trading import PaperExecution
from agent_platform.adapters.sqlite.paper import SqlitePaperStore
from agent_platform.application.decision_models import BudgetedDecisionModel
from agent_platform.application.paper_trading import PaperTradingService
from agent_platform.config import RuntimeConfigurationError
from agent_platform.domain.events import StateType
from agent_platform.domain.model_modules import JevModuleSettings
from agent_platform.domain.paper_trials import PaperTrialPolicy
from agent_platform.ports.model import ModelCallFailed
from agent_platform.runtime.paper_trading import PaperTradingRuntime

PaperModelConfig = PaperTrialPolicy


def prepare_paper(config, clock, *, environment=None):
    """Validate files and credentials before opening databases or network clients."""
    if not config.paper or config.paper_mock:
        return None
    from agent_platform.adapters.openrouter.transport import OpenRouterCredentials

    try:
        with config.paper_model_config.open("rb") as stream:
            raw = stream.read(16385)
        if len(raw) > 16384:
            raise ValueError("paper configuration too large")

        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ValueError("duplicate configuration field")
                result[key] = value
            return result

        parsed = json.loads(raw, object_pairs_hook=pairs)
        selected = PaperModelConfig.model_validate_json(json.dumps(parsed))
        if not config.paper_read_only:
            selected.validate_active(clock.utcnow())
        key = (os.environ if environment is None else environment).get("OPENROUTER_API_KEY")
        if not key:
            raise ValueError("missing credential")
        credentials = OpenRouterCredentials(api_key=key)
    except (OSError, ValueError, TypeError, RecursionError):
        raise RuntimeConfigurationError(
            "真实 JEV Paper 配置无效或已到期；请核对总费用、单次上限、"
            "时间、价格和本机 OPENROUTER_API_KEY。"
        ) from None
    return selected, credentials


class TrialDecisionModel:
    def __init__(self, model, trial, clock, *, read_only=False):
        if type(read_only) is not bool:
            raise ValueError("read-only flag must be boolean")
        self.model, self.trial, self.clock = model, trial, clock
        self.read_only = read_only

    def validate_active(self):
        if self.read_only:
            raise ValueError("paper model is read-only")
        self.trial.validate_active(self.clock.utcnow())

    @property
    def enabled(self):
        return not self.read_only and self.model.enabled

    @property
    def provider_deadline_managed(self):
        return getattr(self.model, "provider_deadline_managed", False) is True

    def set_enabled(self, enabled):
        if enabled:
            self.validate_active()
        self.model.set_enabled(enabled)

    async def budget_status(self):
        if getattr(self.model, "provider_managed", False):
            return {
                "provider_managed": True,
                "billing_source": "openrouter_key",
                "active": not self.read_only and self.trial.issued_at <= self.clock.utcnow(),
                "read_only": self.read_only,
            }
        options = {"cumulative": True} if self.trial.expires_at is None else {}
        balance = await self.model.budgets.budget_balance(
            self.clock.utcnow(), daily_limit_usd=self.trial.trial_total_usd, **options
        )
        return {
            "balance": balance.model_dump(mode="json"),
            "remaining_usd": str(
                max(Decimal(0), balance.daily_limit_usd - balance.spent_usd - balance.reserved_usd)
            ),
            "single_call_usd": str(self.trial.single_call_usd),
            "expires_at": self.trial.expires_at.isoformat()
            if self.trial.expires_at is not None
            else None,
            "budget_scope": "cumulative" if self.trial.expires_at is None else "daily",
            "active": not self.read_only
            and self.trial.issued_at <= self.clock.utcnow()
            and (self.trial.expires_at is None or self.clock.utcnow() < self.trial.expires_at),
            "read_only": self.read_only,
        }

    async def session_usage(self, session_id, *, wallet_database=None):
        return await self.model.budgets.session_usage(
            session_id, wallet_database=wallet_database
        )

    async def decide(self, request):
        if self.read_only:
            raise ModelCallFailed("model_read_only")
        self.validate_active()
        if self.trial.expires_at is not None and request.deadline > self.trial.expires_at:
            raise ModelCallFailed("quote_unavailable")
        response = await self.model.decide(request)
        try:
            self.validate_active()
        except ValueError:
            raise ModelCallFailed("provider_timeout", response.usage) from None
        return response


class CachedPublicPaperMarket:
    def __init__(self, cache):
        self.cache = cache

    async def sample(self):
        frame = await self.cache.latest()
        return frame.market if frame.market_source == "binance_direct" else None

    async def features(self):
        frame = await self.cache.latest()
        return frame.features if frame.market_source == "binance_direct" else None


async def assemble_paper(*, config, prepared, stack, database_path, core, sessions, cache, clock):
    if not config.paper:
        return None, None
    if config.paper_mock:
        model = MockPaperDecisionModel(clock)
        price_version, decision_source = "offline-mock-v1", "offline_mock"
    else:
        trial, credentials = prepared
        from agent_platform.adapters.openrouter.jev import OpenRouterDecisionModel
        from agent_platform.adapters.openrouter.transport import OpenRouterClient
        from agent_platform.ports.paper import PaperGuardConflict

        try:
            store = SqlitePaperStore(core.path)
            if config.paper_read_only:
                previous = await store.load(trial.budget_key)
                if (
                    previous is None
                    or previous.state_type != StateType.PAPER_TRIAL
                    or previous.state.policy != trial
                ):
                    raise PaperGuardConflict("read-only requires the exact existing trial")
            else:
                await store.ensure_trial(trial, clock.utcnow())
        except PaperGuardConflict:
            raise RuntimeConfigurationError(
                "本日 JEV 费用限额已固化；续期必须是独立授权并保留原上限、消费与预留。"
            ) from None
        client = OpenRouterClient(
            clock, credentials=credentials, enabled=True, proxy_url=config.openrouter_proxy
        )
        stack.push_async_callback(client.aclose)
        model = TrialDecisionModel(
            BudgetedDecisionModel(
                port=OpenRouterDecisionModel(client, clock),
                budgets=core,
                clock=clock,
                price=trial.price,
                daily_limit_usd=trial.trial_total_usd,
                max_single_cost_usd=trial.single_call_usd,
                hourly_call_limit=60,
                settings=JevModuleSettings(enabled=False),
                provider_managed=config.model_budget_provider_managed,
            ),
            trial,
            clock,
            read_only=config.paper_read_only,
        )
        price_version, decision_source = trial.price.version, "real_jev"
    market_source = "binance_public" if config.live_public else "offline_demo"
    market = CachedPublicPaperMarket(cache) if config.live_public else DemoPaperMarket(clock)
    service = PaperTradingService(
        store=SqlitePaperStore(database_path),
        sessions=sessions,
        clock=clock,
        market=market,
        model=model,
        execution=PaperExecution(),
        price_version=price_version,
        decision_source=decision_source,
        market_source=market_source,
    )
    return service, PaperTradingRuntime(service)

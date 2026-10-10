"""Futures assembly selects market and execution independently; no exchange writer."""

from agent_platform.adapters.paper.futures import PaperFuturesBackend
from agent_platform.adapters.sqlite.futures_paper import SqliteFuturesPaperStore
from agent_platform.adapters.sqlite.trade_archive import SqliteTradeArchiveStore
from agent_platform.adapters.sqlite.trading_execution import SqliteExecutionJournal
from agent_platform.adapters.sqlite.trading_runtime import SqliteTradingRuntimeStore
from agent_platform.application.futures_trading import FuturesTradingService
from agent_platform.application.trade_authorization import LegacyTradeAuthorizer
from agent_platform.application.trading_execution import TradeExecutionService
from agent_platform.application.trading_maintenance import TradingMaintenance
from agent_platform.runtime.futures_trading import FuturesTradingRuntime


async def assemble_futures(
    *, config, stack, database_path, sessions, controls, clock, paper, history
):
    if not config.paper:
        return None, None
    if config.live_public:
        from agent_platform.adapters.binance_direct.futures_stream import FuturesStreamMarket

        market = await stack.enter_async_context(
            FuturesStreamMarket(clock, proxy_url=config.futures_proxy)
        )
        market_source = "binance_futures_public"
    else:
        from agent_platform.adapters.fake.futures_trading import OfflineFuturesMarket

        market = OfflineFuturesMarket(clock)
        history = market
        market_source = "offline_replay"
    if config.paper_mock:
        from agent_platform.adapters.fake.futures_trading import OfflineFuturesDecisionModel

        model = OfflineFuturesDecisionModel(
            clock
        )  # WAIT by default, never random autonomous trades.
        decision_source, price_version = "offline_mock", "offline-mock-v1"
    else:
        from agent_platform.application.decision_models import BudgetedDecisionModel
        from agent_platform.bootstrap_paper import TrialDecisionModel

        shared = paper.model
        # Separate activation and cadence; retain the same durable fee ledger,
        # trial and HTTP connection pool. Spot's hourly default remains 60.
        model = TrialDecisionModel(
            BudgetedDecisionModel(
                port=shared.model.port,
                budgets=shared.model.budgets,
                clock=clock,
                price=shared.model.price,
                daily_limit_usd=shared.model.daily_limit_usd,
                max_single_cost_usd=shared.model.max_single_cost_usd,
                hourly_call_limit=config.futures_cadence.hourly_call_limit,
                provider_managed=config.model_budget_provider_managed,
            ),
            shared.trial,
            clock,
            read_only=shared.read_only,
        )
        decision_source, price_version = "real_jev", paper.price_version
    wallet = SqliteFuturesPaperStore(database_path)
    journal = SqliteExecutionJournal(database_path)
    state = SqliteTradingRuntimeStore(database_path)
    await wallet.initialize()
    await journal.initialize()
    await state.initialize()
    backend = PaperFuturesBackend(wallet, market_source=market_source)
    maintenance = TradingMaintenance(
        lifecycle=backend, store=state, market=market, clock=clock, market_source=market_source
    )
    execution = TradeExecutionService(
        journal=journal,
        execution=backend,
        accounts=backend,
        market=market,
        clock=clock,
        market_source=market_source,
        authorizer=LegacyTradeAuthorizer(state),
    )
    service = FuturesTradingService(
        sessions=sessions,
        controls=controls,
        store=state,
        backend=backend,
        maintenance=maintenance,
        execution=execution,
        model=model,
        history=history,
        clock=clock,
        market_source=market_source,
        decision_source=decision_source,
        price_version=price_version,
        cadence=config.futures_cadence,
        archive=SqliteTradeArchiveStore(database_path),
    )
    if config.futures_multiscale:
        service.multiscale = await assemble_multiscale_context(
            config=config, stack=stack, database_path=database_path, clock=clock, model=model
        )
    return service, FuturesTradingRuntime(
        service,
        decision_seconds=config.futures_cadence.decision_seconds,
        maintenance_seconds=config.futures_cadence.maintenance_seconds,
        max_predictions=config.futures_cadence.max_predictions,
    )


async def assemble_multiscale_context(*, config, stack, database_path, clock, model):
    """Build input resources independently of the existing wallet and runtime."""
    import json
    from pathlib import Path

    from agent_platform.adapters.sqlite.background import SqliteBackgroundStore
    from agent_platform.application.background import MultiScaleContext
    from agent_platform.config import RuntimeConfigurationError
    from agent_platform.domain.background import BackgroundSettings

    settings = BackgroundSettings()
    if config.background_model_config is not None:
        try:
            with config.background_model_config.open("rb") as stream:
                raw = stream.read(16385)
            if len(raw) > 16384:
                raise ValueError("background config exceeds bounds")

            def pairs(items):
                parsed = {}
                for key, value in items:
                    if key in parsed:
                        raise ValueError("duplicate background configuration")
                    parsed[key] = value
                return parsed

            settings = BackgroundSettings.model_validate_json(
                json.dumps(json.loads(raw, object_pairs_hook=pairs))
            )
        except (OSError, ValueError, TypeError):
            raise RuntimeConfigurationError(
                "背景模型配置无效；请核对模型、供应商和已验证价格。"
            ) from None
    public = None
    if config.live_public:
        from agent_platform.adapters.binance_direct.futures_public import FuturesPublicClient
        from agent_platform.adapters.binance_direct.multiscale import NativeFuturesKlines

        public = await stack.enter_async_context(
            FuturesPublicClient(clock, proxy_url=config.futures_proxy)
        )
        feed = NativeFuturesKlines(clock, public=public, proxy_url=config.futures_proxy)
    else:
        from agent_platform.adapters.fake.multiscale import OfflineKlines

        feed = OfflineKlines(clock)
    background_model = None
    if config.paper_mock:
        from agent_platform.adapters.fake.multiscale import OfflineBackground

        background_model = OfflineBackground(clock, settings.refresh_seconds)
    elif settings.enabled:
        from agent_platform.adapters.openrouter.background import OpenRouterBackground

        if model.trial.expires_at is not None:
            raise RuntimeConfigurationError("新背景模块需要沿用无到期时间的累计费用配置。")

        def active():
            try:
                model.validate_active()
                return model.enabled
            except ValueError:
                return False

        background_model = OpenRouterBackground(
            client=model.model.port.client,
            budgets=model.model.budgets,
            clock=clock,
            settings=settings,
            total_usd=model.trial.trial_total_usd,
            single_usd=model.trial.single_call_usd,
            active=active,
            hourly_call_limit=config.futures_cadence.hourly_call_limit,
            provider_managed=config.model_budget_provider_managed,
        )
    sidecar = SqliteBackgroundStore(Path(database_path).with_suffix(".background.sqlite3"))
    await sidecar.initialize()
    context = MultiScaleContext(
        clock=clock,
        feed=feed,
        model=background_model,
        store=sidecar,
        refresh_seconds=settings.refresh_seconds,
        on_close=public.aclose if public is not None else None,
    )
    stack.push_async_callback(context.aclose)
    return context

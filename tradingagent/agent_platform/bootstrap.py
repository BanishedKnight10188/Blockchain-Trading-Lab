"""The only assembly boundary for concrete read-only providers and credentials."""

from __future__ import annotations

import os
from collections.abc import Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from agent_platform.adapters.sqlite import open_store
from agent_platform.adapters.sqlite.sessions import SqliteSessionStore
from agent_platform.application.account_sync import AccountSyncService
from agent_platform.application.advice_queries import AdviceQueryService
from agent_platform.application.agent_controls import AgentControlService
from agent_platform.application.attribution import AttributionService
from agent_platform.application.decisions import DecisionService
from agent_platform.application.feedback import FeedbackService
from agent_platform.application.ledger_queries import LedgerQueryService
from agent_platform.application.queries import QueryService
from agent_platform.application.reports import ReportService
from agent_platform.application.review_queries import ReviewQueryService
from agent_platform.application.reviews import ReviewService
from agent_platform.application.routing import ModelRouter
from agent_platform.application.sessions import SessionService
from agent_platform.application.snapshots import SnapshotFactory
from agent_platform.application.system_queries import SystemQueryService
from agent_platform.bootstrap_futures import assemble_futures
from agent_platform.bootstrap_paper import assemble_paper, prepare_paper
from agent_platform.config import RuntimeConfig, RuntimeConfigurationError
from agent_platform.domain.routing import RoutingPolicy
from agent_platform.ports.runtime import RuntimePort
from agent_platform.runtime.account_signals import AccountSignalRuntime
from agent_platform.runtime.clock import CalibratedClock, SystemClock
from agent_platform.runtime.decisions import DecisionRuntime
from agent_platform.runtime.diagnostics import DiagnosticsRuntime
from agent_platform.runtime.latest import LatestOverview
from agent_platform.runtime.market_archive import MarketArchiveRuntime
from agent_platform.runtime.read_only import ReadOnlyRuntime
from agent_platform.runtime.review_jobs import ReviewJobService
from agent_platform.runtime.supervisor import RuntimeSupervisor

if TYPE_CHECKING:
    from agent_platform.adapters.binance_direct.signing import HmacCredentials


@dataclass(frozen=True)
class ApplicationServices:
    sessions: SessionService
    queries: QueryService
    runtime: RuntimePort
    feedback: FeedbackService | None = None
    attribution: AttributionService | None = None
    reports: ReportService | None = None
    ledger: LedgerQueryService | None = None
    reviews: ReviewService | None = None
    review_jobs: ReviewJobService | None = None
    review_queries: ReviewQueryService | None = None
    system: SystemQueryService | None = None
    controls: AgentControlService | None = None
    paper: object | None = None
    initial_analysis: object | None = None
    futures_trading: object | None = None
    event_agent: object | None = None


def load_credentials(
    config: RuntimeConfig, environment: Mapping[str, str] | None = None
) -> HmacCredentials | None:
    if not config.live_account:
        return None
    from agent_platform.adapters.binance_direct.signing import HmacCredentials

    environment = os.environ if environment is None else environment
    key, secret = environment.get("BINANCE_API_KEY"), environment.get("BINANCE_API_SECRET")
    if not key or not secret:
        raise RuntimeConfigurationError(
            "账户只读模式需要同时配置BINANCE_API_KEY和BINANCE_API_SECRET。"
        )
    try:
        return HmacCredentials(key, secret)
    except ValueError:
        raise RuntimeConfigurationError("账户只读凭据格式无效，请在本机运行环境中检查。") from None


@asynccontextmanager
async def build_application_services(
    database_path: Path, config: RuntimeConfig | None = None, *, clock=None
):
    config = config or RuntimeConfig()
    load_credentials(config)  # Reject invalid account configuration before any NTP access.
    async with AsyncExitStack() as stack:
        if clock is None:
            clock = (
                await stack.enter_async_context(CalibratedClock())
                if config.paper and config.live_public
                else SystemClock()
            )
        services = await stack.enter_async_context(_build_services(database_path, config, clock))
        yield services


@asynccontextmanager
async def _build_services(database_path, config, clock):
    config = config or RuntimeConfig()
    credentials = load_credentials(config)  # Fail before opening storage or network workers.
    prepared_paper = prepare_paper(config, clock)
    store = await open_store(database_path)
    controls = AgentControlService(
        store=store,
        clock=clock,
        operation=config.operation,
        jev=config.model_modules.jev,
        trader=config.model_modules.jev_trader,
        production_reads=config.live_public or config.live_account or config.live_user_stream,
    )
    selected = await controls.current()
    config = RuntimeConfig.model_validate(
        {
            **config.model_dump(),
            "operation": selected.operation,
            "model_modules": config.model_modules.model_copy(
                update={"jev": selected.jev, "jev_trader": selected.trader}
            ),
        }
    )
    async with AsyncExitStack() as stack:
        market = account_sync = user_stream = None
        if config.live_public and not config.futures_task_only:
            try:
                from agent_platform.adapters.binance_direct.market_stream import BinanceMarketStream
                from agent_platform.adapters.binance_direct.public_rest import PublicRestClient
            except ImportError:
                raise RuntimeConfigurationError(
                    "公共行情运行依赖缺失，请按环境文档由用户安装market依赖。"
                ) from None
            rest = await stack.enter_async_context(PublicRestClient(clock))
            market = BinanceMarketStream(rest, clock)
        if config.live_account:
            try:
                from agent_platform.adapters.binance_direct.account import BinanceAccount
                from agent_platform.adapters.binance_direct.read_client import ReadOnlyClient
            except ImportError:
                raise RuntimeConfigurationError(
                    "账户只读运行依赖缺失，请按环境文档由用户安装market依赖。"
                ) from None
            reader = await stack.enter_async_context(ReadOnlyClient(clock, credentials))
            account_sync = AccountSyncService(
                BinanceAccount(reader, clock, config.account_ref), store, clock
            )
            if config.live_user_stream:
                try:
                    from agent_platform.adapters.binance_direct.user_stream import BinanceUserStream
                except ImportError:
                    raise RuntimeConfigurationError(
                        "私流只读运行依赖缺失，请按环境文档由用户安装market依赖。"
                    ) from None

                user_stream = BinanceUserStream(reader, clock, config.account_ref, credentials)
        cache = LatestOverview(
            clock,
            mode="live_read_only" if market is not None or config.live_account else "disabled",
            market_source="binance_direct" if market is not None else "none",
            account_source="binance_direct" if config.live_account else "none",
        )
        read_only = ReadOnlyRuntime(
            cache,
            clock,
            market=market,
            account_sync=account_sync,
            account_ref=config.account_ref if config.live_account else None,
        )
        session_store = SqliteSessionStore(database_path)
        from agent_platform.adapters.sqlite.session_analysis import SqliteInitialAnalysisStore
        from agent_platform.application.session_analysis import InitialAnalysisService
        from agent_platform.runtime.session_analysis import InitialAnalysisRuntime

        history_provider = None
        chart_provider = None
        if config.live_public:
            from agent_platform.adapters.binance_direct.futures_public import FuturesPublicClient

            history_provider = await stack.enter_async_context(
                FuturesPublicClient(clock, proxy_url=config.futures_proxy)
            )
            chart_provider = await stack.enter_async_context(
                FuturesPublicClient(clock, proxy_url=config.futures_proxy)
            )
        elif config.paper and config.paper_mock:
            from agent_platform.adapters.fake.session_analysis import FakeHistoricalMarket

            history_provider = FakeHistoricalMarket(clock)
        initial_store = SqliteInitialAnalysisStore(database_path)
        await initial_store.initialize()
        initial_analysis = InitialAnalysisService(
            sessions=session_store,
            store=initial_store,
            clock=clock,
            history=history_provider,
            chart=chart_provider,
        )
        snapshots = SnapshotFactory(
            session_store,
            store,
            cache,
            clock,
            account_ref=config.account_ref if config.live_account else None,
        )
        decisions = DecisionService(
            clock=clock,
            router=ModelRouter(RoutingPolicy(), clock),
            decisions=store,
            budgets=store,
            current=snapshots,
            model_modules=config.model_modules,
        )
        decision_runtime = DecisionRuntime(session_store, cache, snapshots, decisions, clock)
        review_jobs = ReviewJobService(store=store, clock=clock, account_ref=config.account_ref)
        workers = [read_only, decision_runtime, review_jobs]
        if history_provider is not None:
            workers.append(InitialAnalysisRuntime(initial_analysis))
        model_budget = (
            await open_store(config.model_budget_database)
            if config.model_budget_database is not None
            else store
        )
        paper, paper_worker = await assemble_paper(
            config=config,
            prepared=prepared_paper,
            stack=stack,
            database_path=database_path,
            core=model_budget,
            sessions=session_store,
            cache=cache,
            clock=clock,
        )
        if paper_worker is not None:
            workers.append(paper_worker)
        futures_trading, futures_worker = await assemble_futures(
            config=config,
            stack=stack,
            database_path=database_path,
            sessions=session_store,
            controls=controls,
            clock=clock,
            paper=paper,
            history=history_provider,
        )
        if futures_worker is not None:
            workers.append(futures_worker)
        event_agent = None
        if config.event_agent:
            from agent_platform.bootstrap_event_agent import build_event_agent_services

            event_path = config.event_agent_database or database_path.with_suffix(
                ".event-agent.sqlite3"
            )
            if event_path.resolve() == database_path.resolve():
                raise RuntimeConfigurationError("event Agent requires a separate SQLite database")
            event_agent = await build_event_agent_services(event_path, clock=clock)
            workers.append(event_agent)
        archive = None
        if market is not None and config.market_archive:
            from agent_platform.adapters.sqlite.retention import SqliteMarketArchive

            archive = MarketArchiveRuntime(
                cache,
                SqliteMarketArchive(
                    database_path.with_suffix(".market.sqlite3"), retention=config.market_retention
                ),
                clock,
            )
            workers.append(archive)
        if user_stream is not None:
            workers.append(
                AccountSignalRuntime(
                    user_stream, read_only.request_sync, clock, account_ref=config.account_ref
                )
            )
        from agent_platform.adapters.operational_log import SafeOperationalLog

        diagnostics = DiagnosticsRuntime(
            SafeOperationalLog(database_path.with_suffix(".operations.jsonl")),
            clock,
            lambda: runtime.health(),
        )
        workers.append(diagnostics)
        runtime = RuntimeSupervisor(*workers)
        stack.push_async_callback(runtime.stop)
        await runtime.start()
        queries = QueryService(
            cache,
            clock,
            advice=AdviceQueryService(session_store, store, snapshots, clock),
            decision_runtime=decision_runtime,
            sessions=session_store,
        )
        yield ApplicationServices(
            SessionService(session_store, clock),
            queries,
            runtime,
            feedback=FeedbackService(
                store=store,
                states=store,
                snapshots=snapshots,
                clock=clock,
                account_ref=config.account_ref,
            ),
            attribution=AttributionService(
                store=store, clock=clock, account_ref=config.account_ref
            ),
            reports=ReportService(store=store, clock=clock, account_ref=config.account_ref),
            ledger=LedgerQueryService(store=store, source=cache, account_ref=config.account_ref),
            reviews=ReviewService(store=store, clock=clock, account_ref=config.account_ref),
            review_jobs=review_jobs,
            review_queries=ReviewQueryService(
                store=store, source=cache, account_ref=config.account_ref
            ),
            system=SystemQueryService(
                policy=RoutingPolicy(daily_limit_usd=prepared_paper[0].trial_total_usd)
                if prepared_paper is not None
                else None,
                queries=queries,
                budgets=store,
                clock=clock,
                review_jobs=review_jobs,
                runtime=runtime,
                archive=archive,
                market_retention=config.market_retention,
                model_modules=config.model_modules,
                operation=config.operation,
                controls=controls,
                paper=paper,
            ),
            controls=controls,
            paper=paper,
            initial_analysis=initial_analysis,
            futures_trading=futures_trading,
            event_agent=event_agent,
        )


async def build_session_service(database_path: Path) -> SessionService:
    store = SqliteSessionStore(database_path)
    await store.initialize()
    return SessionService(store, SystemClock())

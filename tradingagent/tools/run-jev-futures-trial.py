"""One explicitly enabled live JEV cycle, isolated funds and shared durable fees.

Default is a free preflight. Never retries inference or resets an existing account.
Credentials remain in the process; report and config contain no key.
"""
# ruff: noqa: E402 -- standalone tool must add its workspace before application imports.

import argparse
import asyncio
import json
import os
import re
import sqlite3
import sys
from contextlib import AsyncExitStack
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_platform.adapters.binance_direct.futures_market import FuturesMarketClient
from agent_platform.adapters.binance_direct.futures_public import FuturesPublicClient
from agent_platform.adapters.openrouter.jev import OpenRouterDecisionModel
from agent_platform.adapters.openrouter.transport import (
    OpenRouterClient,
    OpenRouterCredentials,
    OpenRouterError,
)
from agent_platform.adapters.sqlite import open_store
from agent_platform.adapters.sqlite.paper import SqlitePaperStore
from agent_platform.adapters.sqlite.sessions import SqliteSessionStore
from agent_platform.application.agent_controls import AgentControlService
from agent_platform.application.decision_models import BudgetedDecisionModel
from agent_platform.application.sessions import SessionService
from agent_platform.bootstrap_futures import assemble_futures
from agent_platform.bootstrap_paper import TrialDecisionModel
from agent_platform.config import RuntimeConfig
from agent_platform.domain.agent_controls import TraderSelection
from agent_platform.domain.costs import BudgetReservation
from agent_platform.domain.model_modules import (
    JevModuleSettings,
    JevTraderSettings,
    ModelModulesConfig,
)
from agent_platform.domain.operating_modes import OperatingSettings
from agent_platform.domain.paper_trials import PaperTrialPolicy
from agent_platform.domain.routing import ModelPrice
from agent_platform.domain.session_market import SessionAnalysisTarget
from agent_platform.domain.sessions import TradingStyle
from agent_platform.domain.trading_runtime import TradingLimits, TradingPolicy
from agent_platform.ports.futures_market import FuturesMarketUnavailable
from agent_platform.ports.model import ModelCallFailed
from agent_platform.runtime.clock import SystemClock

BUDGET_DATABASES = (
    ROOT / "data/jev-paper-20261007.sqlite3",
    ROOT / "data/futures-core-preview-20261008.sqlite3",
)
SHARED_BUDGET = BUDGET_DATABASES[1]
PROXY = "http://127.0.0.1:7897"
TRACE_EVENTS = frozenset(
    f"{operation}.{status}"
    for operation in (
        "connection.connect_tcp",
        "connection.start_tls",
        "http11.send_request_headers",
        "http11.send_request_body",
        "http11.receive_response_headers",
        "http11.receive_response_body",
    )
    for status in ("started", "complete", "failed")
)
SAFE_EXCEPTION_TYPES = frozenset(
    {
        "ConnectError",
        "ConnectTimeout",
        "PoolTimeout",
        "ReadError",
        "ReadTimeout",
        "WriteError",
        "WriteTimeout",
        "RemoteProtocolError",
        "SSLError",
        "SSLEOFError",
        "TimeoutError",
        "OSError",
        "ValueError",
        "TypeError",
        "AttributeError",
        "RuntimeError",
    }
)


def read_key():
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key and os.name == "nt":
        import winreg

        for root, location in (
            (winreg.HKEY_CURRENT_USER, "Environment"),
            (
                winreg.HKEY_LOCAL_MACHINE,
                r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
            ),
        ):
            try:
                with winreg.OpenKey(root, location) as source:
                    key = winreg.QueryValueEx(source, "OPENROUTER_API_KEY")[0]
                if key:
                    break
            except OSError:
                pass
    if not key:
        raise ValueError("local_key_missing")
    return OpenRouterCredentials(api_key=key)


def prior_fees():
    seen = set()
    spent = held = Decimal(0)
    for path in BUDGET_DATABASES:
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
            if db.execute("SELECT billing_frozen FROM budget_settings").fetchone()[0]:
                raise ValueError("existing_billing_frozen")
            for (body,) in db.execute("SELECT body FROM budget_requests"):
                reservation = BudgetReservation.model_validate_json(body)
                if reservation.request.request_id in seen:
                    continue
                seen.add(reservation.request.request_id)
                spent += reservation.actual_cost_usd or Decimal(0)
                held += reservation.held_cost_usd
    if held or spent >= 1:
        raise ValueError("existing_fee_exposure_requires_reconciliation")
    return spent


class SingleDispatch:
    """Hard one-call ceiling even if a runtime event accidentally wakes twice."""

    def __init__(self, model):
        self.model, self.calls = model, 0

    def validate_active(self):
        self.model.validate_active()

    def set_enabled(self, value):
        self.model.set_enabled(value)

    @property
    def enabled(self):
        return self.model.enabled

    async def decide(self, request):
        if self.calls:
            raise ModelCallFailed("model_module_disabled")
        self.calls += 1
        return await self.model.decide(request)


def provider_response_observer(report):
    async def observe(response):
        # Keep only numeric status, never provider body, headers, or credentials.
        report["provider_http_status"] = response.status_code

    return observe


def provider_request_observer(report):
    async def trace(event, info):
        entries = report.setdefault("http_trace", [])
        if event in TRACE_EVENTS and len(entries) < 32:
            entry = {"event": event}
            if "exception" in info:
                name = type(info["exception"]).__name__
                entry["exception_type"] = name if name in SAFE_EXCEPTION_TYPES else "OtherError"
            entries.append(entry)

    async def observe(request):
        # Prepared does not mean sent. Only HTTP trace events establish send progress.
        report["http_request_prepared"] = True
        report["http_trace"] = []
        previous = request.extensions.get("trace")

        async def combined(event, info):
            if previous is not None:
                await previous(event, info)
            await trace(event, info)

        request.extensions["trace"] = combined

    return observe


class DiagnosticOpenRouterClient(OpenRouterClient):
    def __init__(self, *args, report, **kwargs):
        super().__init__(*args, **kwargs)
        self.report = report

    async def post(self, *args, **kwargs):
        try:
            return await super().post(*args, **kwargs)
        except OpenRouterError as error:
            code = str(error)
            self.report["provider_failure_code"] = (
                code
                if code
                in {
                    "model_calls_disabled",
                    "unsupported_model_endpoint",
                    "invalid_model_request",
                    "provider_rate_limited",
                    "provider_rejected",
                    "provider_access_denied",
                    "provider_transport_error",
                    "provider_timeout",
                    "invalid_model_response",
                    "model_response_too_large",
                }
                else code
                if type(code) is str and re.fullmatch(r"provider_http_[1-5][0-9]{2}", code)
                else "unknown_transport_failure"
            )
            raise
        except Exception as error:
            name = type(error).__name__
            self.report["provider_exception_type"] = (
                name if name in SAFE_EXCEPTION_TYPES else "OtherError"
            )
            raise


async def run(args):
    clock = SystemClock()
    credentials = read_key()
    prior = prior_fees()
    original_sessions = SqliteSessionStore(SHARED_BUDGET)
    original = await original_sessions.active()
    if original is None or original.analysis_target.market != "usdt_perpetual":
        raise ValueError("select_futures_session_first")
    if args.resume:
        if not args.config.exists() or not args.database.exists():
            raise ValueError("resume_requires_original_config_and_database")
        trial = PaperTrialPolicy.model_validate_json(args.config.read_text("utf-8"))
        trial.validate_active(clock.utcnow())
        price = trial.price
        # Resume free preparation only. A possibly dispatched request is never replayed.
        with sqlite3.connect(SHARED_BUDGET.as_uri() + "?mode=ro", uri=True) as db:
            if any(
                BudgetReservation.model_validate_json(row[0]).request.requested_at
                >= trial.issued_at
                for row in db.execute("SELECT body FROM budget_requests")
            ):
                raise ValueError("resume_blocked_after_any_paid_reservation")
    else:
        price = ModelPrice(
            version="jev-verified-20261008-trial",
            input_usd_per_million="0.042",
            output_usd_per_million="0",
            verified_at=clock.utcnow(),
            valid_until=clock.utcnow() + timedelta(minutes=30),
        )
        trial = PaperTrialPolicy(
            trial_total_usd=str(Decimal(1) - prior),
            single_call_usd="0.02",
            issued_at=clock.utcnow(),
            expires_at=price.valid_until,
            price=price,
        )
    report = {
        "started_at": clock.utcnow().isoformat(),
        "execute_requested": args.execute,
        "resume_requested": args.resume,
        "original_session": original.model_dump(mode="json"),
        "prior_actual_cost_usd": str(prior),
        "trial": trial.model_dump(mode="json"),
        "price_source": "https://openrouter.ai/typesafe/jev-1.13/",
        "key_present": True,
        "exchange_orders": 0,
    }
    async with FuturesPublicClient(clock, proxy_url=PROXY) as public:
        before = clock.utcnow()
        exchange_time = await public.get("/fapi/v1/time")
        after = clock.utcnow()
        report["server_minus_local_mid_ms"] = str(
            round(exchange_time["serverTime"] - (before.timestamp() + after.timestamp()) * 500, 2)
        )
        history = await public.history(original.analysis_target)
        report["history"] = {
            "source": history.source,
            "count": len(history.candles),
            "content_hash": history.content_hash,
            "requested_end": history.requested_end.isoformat(),
        }
    async with FuturesMarketClient(clock, proxy_url=PROXY) as market:
        try:
            snapshot = await market.snapshot(original.analysis_target.symbol)
            report["market_snapshot_readable"] = True
            report["quote"] = snapshot.quote.model_dump(mode="json")
        except FuturesMarketUnavailable:
            report["market_snapshot_readable"] = False
    if not args.execute:
        return report
    if not report["market_snapshot_readable"]:
        raise ValueError("public_snapshot_guard_failed_before_paid_dispatch")
    if args.report.exists() or (
        not args.resume and (args.database.exists() or args.config.exists())
    ):
        raise ValueError("trial_output_already_exists_no_reset")
    if not args.resume:
        args.config.write_text(trial.model_dump_json(indent=2) + "\n", encoding="utf-8")
    core = await open_store(SHARED_BUDGET)
    await SqlitePaperStore(SHARED_BUDGET).ensure_trial(trial, clock.utcnow())
    trial_core = await open_store(args.database)
    session_store = SqliteSessionStore(args.database)
    await session_store.initialize()
    sessions = SessionService(session_store, clock)
    if args.resume:
        session = await session_store.active()
        if (
            session is None
            or session.style != original.style
            or session.analysis_target != original.analysis_target
        ):
            raise ValueError("resume_session_selection_changed")
    else:
        session = await sessions.create(
            TradingStyle.model_validate_json(original.style.model_dump_json()),
            SessionAnalysisTarget.model_validate_json(original.analysis_target.model_dump_json()),
        )
    if session.status != "running":
        session = await sessions.transition(session.session_id, "running", session.revision)
    config = RuntimeConfig(
        paper=True,
        paper_model_config=args.config,
        live_public=True,
        futures_proxy=PROXY,
        market_archive=False,
        operation=OperatingSettings(mode="auto", execution_environment="paper"),
        model_modules=ModelModulesConfig(jev_trader=JevTraderSettings(enabled=True)),
    )
    controls = AgentControlService(
        store=trial_core,
        clock=clock,
        operation=config.operation,
        trader=config.model_modules.jev_trader,
        production_reads=True,
    )
    selected = await controls.current()
    if selected.revision == 0:
        # Execution checks durable intent, not the assembly's in-memory defaults.
        selected = await controls.update_trader(
            TraderSelection(
                mode="auto", execution_environment="paper", enabled=True, confirmed=True
            ),
            expected_revision=selected.revision,
        )
    report["persisted_controls"] = selected.model_dump(mode="json")
    async with AsyncExitStack() as stack:
        http = await stack.enter_async_context(
            httpx.AsyncClient(
                trust_env=False,
                follow_redirects=False,
                event_hooks={
                    "request": [provider_request_observer(report)],
                    "response": [provider_response_observer(report)],
                },
            )
        )
        client = DiagnosticOpenRouterClient(
            clock, credentials=credentials, enabled=True, client=http, report=report
        )
        stack.push_async_callback(client.aclose)
        model = SingleDispatch(
            TrialDecisionModel(
                BudgetedDecisionModel(
                    port=OpenRouterDecisionModel(client, clock),
                    budgets=core,
                    clock=clock,
                    price=price,
                    daily_limit_usd=trial.trial_total_usd,
                    max_single_cost_usd=trial.single_call_usd,
                    hourly_call_limit=1,
                    settings=JevModuleSettings(enabled=False),
                ),
                trial,
                clock,
            )
        )
        history_client = await stack.enter_async_context(
            FuturesPublicClient(clock, proxy_url=PROXY)
        )
        service, _worker = await assemble_futures(
            config=config,
            stack=stack,
            database_path=args.database,
            sessions=session_store,
            controls=controls,
            clock=clock,
            paper=SimpleNamespace(model=model, price_version=price.version),
            history=history_client,
        )
        # Real history already confirmed; preserve the same immutable window.
        service._history = history
        await service.store.acquire_owner()
        service.ready = True
        scope = service.scope(session)
        cycle = None
        try:
            await service.recover()
            run = await service.configure(
                TradingLimits(
                    initial_usdt="1000",
                    leverage=2,
                    max_position_notional="500",
                    max_run_loss_usdt="20",
                    fee_bps="4",
                    slippage_bps="2",
                ),
                TradingPolicy(
                    order_notional_usdt="250",
                    max_price_drift_bps="20",
                    min_confidence="0.7",
                    strategy_instructions=(
                        "Paper integration trial. Assess the closed hourly OHLCV history and "
                        "current mark/book. Prefer WAIT when direction lacks clear evidence. "
                        "Open only with a clear consistent trend; never chase an unsupported "
                        "reversal. REDUCE an existing position when its direction is invalidated. "
                        "Hard funds and loss limits always override style. No profitability claims."
                    ),
                ),
                session_id=session.session_id,
                style_revision=session.style_revision,
            )
            account = await service.backend.account(scope, clock.utcnow())
            selected = await controls.current()
            await service.start(
                account_ref=scope.account_ref,
                expected_revision=account.revision,
                style_revision=session.style_revision,
                trader_revision=selected.trader_revision,
            )
            report["run"] = run.model_dump(mode="json")
            cycle = await service.step()
            report["cycle"] = cycle.model_dump(mode="json") if cycle else None
            report["runtime_failure"] = service.last_failure
        finally:
            service.ready = False
            await service.recover()
            await service.store.release_owner()
            report["model_dispatch_attempts"] = model.calls
            report["final_view"] = await service.public_view()
            report["budget"] = (
                await core.budget_balance(clock.utcnow(), daily_limit_usd=trial.trial_total_usd)
            ).model_dump(mode="json")
            report["original_session_unchanged"] = await original_sessions.active() == original
            report["finished_at"] = clock.utcnow().isoformat()
            report["integration_accepted"] = bool(
                cycle
                and cycle.status in {"wait", "filled", "rejected"}
                and cycle.decision is not None
                and cycle.usage is not None
                and cycle.usage.billing_status == "confirmed"
                and Decimal(report["budget"]["reserved_usd"]) == 0
                and report["final_view"]["account"]["status"] == "paused"
                and report["original_session_unchanged"]
            )
            args.report.write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--database", type=Path, default=ROOT / "data/jev-futures-real-20261008.sqlite3"
    )
    parser.add_argument(
        "--config", type=Path, default=ROOT / "data/jev-futures-real-20261008.local.json"
    )
    parser.add_argument(
        "--report", type=Path, default=ROOT / "output/verification/jev-futures-real-20261008.json"
    )
    args = parser.parse_args()
    try:
        report = asyncio.run(run(args))
        print(json.dumps(report, ensure_ascii=False))
        if args.execute and not report.get("integration_accepted", False):
            raise SystemExit(2)
    except Exception as error:
        # No exception string or traceback: malformed provider data may contain secrets.
        print(json.dumps({"trial_failed": type(error).__name__, "report": str(args.report)}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()

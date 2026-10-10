"""One Flash + one JEV trial, real public feed, separate virtual wallet, original fees.

Default is a free preflight. Never starts a scheduler or replays an output path.
Keys stay inside the existing local credential loader and bounded HTTP adapter.
"""
# ruff: noqa: E402 -- standalone tool uses the workspace application.

import argparse
import asyncio
import hashlib
import importlib.util
import json
import sqlite3
import sys
from contextlib import AsyncExitStack, closing
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from time import perf_counter
from types import SimpleNamespace
from uuid import uuid4

import httpx
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location(
    "bounded_trial", ROOT / "tools/run-jev-futures-trial.py"
)
trial = importlib.util.module_from_spec(spec)
spec.loader.exec_module(trial)

from agent_platform.adapters.binance_direct.futures_public import FuturesPublicClient
from agent_platform.adapters.openrouter.jev import OpenRouterDecisionModel
from agent_platform.adapters.openrouter.transport import OpenRouterClient, OpenRouterError
from agent_platform.adapters.sqlite import open_store
from agent_platform.adapters.sqlite.sessions import SqliteSessionStore
from agent_platform.application.agent_controls import AgentControlService
from agent_platform.application.decision_models import BudgetedDecisionModel
from agent_platform.application.sessions import SessionService
from agent_platform.bootstrap_futures import assemble_futures
from agent_platform.bootstrap_paper import TrialDecisionModel
from agent_platform.config import RuntimeConfig
from agent_platform.domain.agent_controls import TraderSelection
from agent_platform.domain.background import BackgroundRequest, BackgroundSettings
from agent_platform.domain.multiscale import BACKGROUND_WINDOWS
from agent_platform.domain.paper_trials import PaperTrialPolicy
from agent_platform.domain.session_market import SessionAnalysisTarget
from agent_platform.domain.sessions import TradingStyle
from agent_platform.domain.trading_runtime import TradingLimits, TradingPolicy
from agent_platform.ports.futures_market import FuturesMarketUnavailable
from agent_platform.ports.model import ModelCallFailed
from agent_platform.ports.trading_execution import ExecutionRejected
from agent_platform.runtime.clock import CalibratedClock


def joint_bound(flash_bytes, input_price, output_price, jev_input, jev_output):
    return (
        Decimal(flash_bytes) * input_price
        + Decimal(2048) * output_price
        + Decimal(32000) * jev_input
        + Decimal(2048) * jev_output
    ) / Decimal(1000000)


def preservation_snapshot():
    """Hash every old row separately, allowing only append-only new fee rows."""
    paths = [
        ROOT / "data" / name
        for name in (
            "jev-futures-real-20261008.sqlite3",
            "futures-core-preview-20261008.sqlite3",
            "jev-paper-20261007.sqlite3",
            "jev-futures-real-20261008.tasks.sqlite3",
        )
    ] + sorted((ROOT / "data/jev-futures-real-20261008-tasks").glob("*.sqlite3"))
    result = {}
    for path in paths:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
            for table in (
                "sessions",
                "domain_states",
                "budget_requests",
                "futures_paper_wallets",
                "futures_trading_runs",
                "futures_trading_cycles",
                "futures_policy_migrations",
                "jev_tasks",
                "futures_paper_operations",
                "futures_trade_archive",
            ):
                if not db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
                ).fetchone():
                    continue
                rows = db.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
                result[str(path.relative_to(ROOT)) + ":" + table] = {
                    str(row[0]): hashlib.sha256(
                        json.dumps(row, ensure_ascii=False).encode()
                    ).hexdigest()
                    for row in rows
                }
    for file in ("readme.md", "data/jev-paper-continuous.local.json"):
        result[file] = {"file": hashlib.sha256((ROOT / file).read_bytes()).hexdigest()}
    return result


def preserved(before, after):
    for name, rows in before.items():
        current = after.get(name, {})
        if any(current.get(key) != value for key, value in rows.items()):
            return False
        if not name.endswith(":budget_requests") and current != rows:
            return False
    return True


class OnceBackground:
    def __init__(self, model):
        self.model, self.calls = model, 0

    async def analyze(self, request):
        if self.calls:
            raise ModelCallFailed("model_module_disabled")
        self.calls += 1
        return await self.model.analyze(request)


class RecordedClient(OpenRouterClient):
    def __init__(self, *args, report, **kwargs):
        super().__init__(*args, **kwargs)
        self.report = report
        self.calls = {}

    async def post(self, path, payload, deadline, **kwargs):
        name = "background" if path == "/api/v1/chat/completions" else "jev"
        if self.calls.get(name, 0):
            raise OpenRouterError("model_calls_disabled")
        self.calls[name] = 1  # Includes ambiguous failures; never replay.
        record = self.report.setdefault("provider_calls", {}).setdefault(name, {})
        record["payload_bytes"] = len(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        )
        started = perf_counter()
        try:
            response = await super().post(path, payload, deadline, **kwargs)
            # The transport has bounded/parsed JSON and rejected credential echoes.
            record["response"] = json.loads(json.dumps(response, default=str))
            evidence = getattr(response, "transport_evidence", None)
            record["transport"] = evidence.model_dump(mode="json") if evidence else None
            return response
        except OpenRouterError as error:
            record["failure"] = str(error)
            evidence = error.transport_evidence
            record["transport"] = evidence.model_dump(mode="json") if evidence else None
            raise
        finally:
            record["elapsed_ms"] = round((perf_counter() - started) * 1000)


async def verify_prices(settings, policy, proxy):
    sources = {}
    async with httpx.AsyncClient(proxy=proxy, trust_env=False, timeout=12) as http:
        for model in (settings.model_id, "typesafe/jev-1.13"):
            url = "https://openrouter.ai/api/v1/models/" + model + "/endpoints"
            response = await public_get(http, url)
            response.raise_for_status()
            data = response.json()["data"]
            endpoints = data["endpoints"]
            selected = (
                [e for e in endpoints if e.get("tag") in settings.providers]
                if model == settings.model_id
                else endpoints
            )
            if not selected:
                raise ValueError("verified_provider_missing")
            price = settings.price if model == settings.model_id else policy.price
            for endpoint in selected:
                pricing = endpoint["pricing"]
                if (
                    Decimal(pricing["prompt"]) * 1000000 > price.input_usd_per_million
                    or Decimal(pricing["completion"]) * 1000000 > price.output_usd_per_million
                ):
                    raise ValueError("configured_price_below_public_price")
                if model == settings.model_id and "structured_outputs" not in endpoint.get(
                    "supported_parameters", []
                ):
                    raise ValueError("structured_output_not_supported")
            sources[model] = {"url": url, "selected_endpoints": selected}
    return sources


async def public_get(http, url):
    for attempt in range(4):
        try:
            return await http.get(url)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            if attempt == 3:
                raise
            await asyncio.sleep(0.25)


async def run(args, report):
    before = preservation_snapshot()
    policy = PaperTrialPolicy.model_validate_json(
        (ROOT / "data/jev-paper-continuous.local.json").read_text("utf-8")
    )
    settings = BackgroundSettings.model_validate_json(
        (ROOT / "data/jev-background-flash.local.json").read_text("utf-8")
    )
    clock = CalibratedClock()
    await clock.refresh()
    policy.validate_active(clock.utcnow())
    if not clock.public_status["ready"] or not settings.enabled:
        raise ValueError("clock_or_background_unavailable")
    report.update({"clock": clock.public_status, "policy": policy.model_dump(mode="json")})
    report["verified_prices"] = await verify_prices(settings, policy, args.proxy)
    core = await open_store(trial.SHARED_BUDGET)
    balance = await core.budget_balance(
        clock.utcnow(), daily_limit_usd=policy.trial_total_usd, cumulative=True
    )
    report["budget_before"] = balance.model_dump(mode="json")
    if balance.billing_frozen:
        raise ValueError("billing_frozen")
    remaining = balance.daily_limit_usd - balance.spent_usd - balance.reserved_usd
    # Worst allowed Flash payload + complete allowed JEV context, before any paid call.
    worst = joint_bound(
        120000,
        settings.price.input_usd_per_million,
        settings.price.output_usd_per_million,
        policy.price.input_usd_per_million,
        policy.price.output_usd_per_million,
    )
    report["joint_worst_bound_usd"] = str(worst)
    if remaining < worst:
        raise ValueError("insufficient_remaining_joint_budget")
    dbpath = args.report.with_suffix(".sqlite3")
    if dbpath.exists():
        raise ValueError("existing_trial_wallet_cannot_be_replayed")
    credentials = trial.read_key()
    local = await open_store(dbpath)
    sessions = SqliteSessionStore(dbpath)
    await sessions.initialize()
    session_service = SessionService(sessions, clock)
    session = await session_service.create(
        TradingStyle(strength=79),
        SessionAnalysisTarget(market="usdt_perpetual", symbol=args.symbol),
    )
    session = await session_service.transition(session.session_id, "running", session.revision)
    config = RuntimeConfig(
        paper=True,
        paper_model_config=ROOT / "data/jev-paper-continuous.local.json",
        live_public=True,
        futures_proxy=args.proxy,
        futures_multiscale=True,
        background_model_config=ROOT / "data/jev-background-flash.local.json",
        operation={"mode": "auto", "execution_environment": "paper"},
        model_modules={"jev_trader": {"enabled": True}},
    )
    controls = AgentControlService(
        store=local,
        clock=clock,
        operation=config.operation,
        trader=config.model_modules.jev_trader,
        production_reads=True,
    )
    current = await controls.current()
    await controls.update_trader(
        TraderSelection(mode="auto", execution_environment="paper", enabled=True, confirmed=True),
        expected_revision=current.revision,
    )
    async with AsyncExitStack() as stack:
        client = RecordedClient(
            clock,
            credentials=credentials,
            enabled=args.execute,
            proxy_url=args.proxy,
            report=report,
        )
        stack.push_async_callback(client.aclose)
        shared = TrialDecisionModel(
            BudgetedDecisionModel(
                port=OpenRouterDecisionModel(client, clock),
                budgets=core,
                clock=clock,
                price=policy.price,
                daily_limit_usd=policy.trial_total_usd,
                max_single_cost_usd=policy.single_call_usd,
            ),
            policy,
            clock,
        )
        public = await stack.enter_async_context(FuturesPublicClient(clock, proxy_url=args.proxy))
        svc, _unused_worker = await assemble_futures(
            config=config,
            stack=stack,
            database_path=dbpath,
            sessions=sessions,
            controls=controls,
            clock=clock,
            paper=SimpleNamespace(model=shared, price_version=policy.price.version),
            history=public,
        )
        svc.model = trial.SingleDispatch(svc.model)
        context = svc.multiscale
        context.model = OnceBackground(context.model)
        report["database"] = str(dbpath)
        await svc.store.acquire_owner()
        svc.ready = True
        try:
            await svc.recover()
            await context.prepare(session, enabled=False)
            # A cold proxy/TLS + WS subscription can outlast the quote's 2s wait.
            # Only public preparation is retried, before any paid activation.
            for attempt in range(8):
                try:
                    await svc.maintenance.observe(args.symbol)
                    break
                except FuturesMarketUnavailable:
                    if attempt == 7:
                        raise
                    await asyncio.sleep(0.5)
            limits = TradingLimits(
                initial_usdt="1000",
                leverage=1,
                max_leverage=10,
                max_position_notional="2000",
                max_run_loss_usdt="20",
                fee_bps="4",
                slippage_bps="2",
            )
            policy_selection = TradingPolicy(
                order_notional_usdt="100",
                max_price_drift_bps="20",
                min_confidence="0.7",
                strategy_instructions=(
                    "Evaluate cached multi-scale background plus the 20 closed 3m and "
                    "60 closed 1s bars and current bid/ask/mark. Choose the full parameter "
                    "plan. WAIT if evidence is weak; manage exits based on changing "
                    "evidence. Hard funds limits always apply. This is one isolated "
                    "virtual-funds trial."
                ),
                decision_mode="parameterized",
            )
            for attempt in range(10):
                try:
                    run_policy = await svc.configure(
                        limits,
                        policy_selection,
                        session_id=session.session_id,
                        style_revision=session.style_revision,
                    )
                    break
                except ExecutionRejected as error:
                    if str(error) != "funding_clock_regressed" or attempt == 9:
                        raise
                    await asyncio.sleep(0.3)
            # Native history remains uncharged throughout preparation.
            report["run"] = run_policy.model_dump(mode="json")
            # No paid model is enabled during native seconds preparation.
            for second in range(85):
                await context.prepare(session, enabled=False)
                windows = context.status(session)["windows"]
                if len(windows) == 6 and all(w["complete"] and w["fresh"] for w in windows):
                    break
                if second % 10 == 0:
                    print(
                        json.dumps(
                            {
                                "phase": "public_warmup",
                                "elapsed_s": second,
                                "counts": [w["count"] for w in windows],
                            }
                        ),
                        flush=True,
                    )
                await asyncio.sleep(1)
            else:
                raise ValueError("six_real_windows_not_ready")
            report["windows_before_paid"] = windows
            captured = clock.utcnow()
            request = BackgroundRequest(
                request_id=uuid4().hex,
                session_id=session.session_id,
                style_revision=session.style_revision,
                symbol=args.symbol,
                style_strength=session.style.strength,
                created_at=captured,
                deadline=captured + timedelta(seconds=60),
                windows=tuple(
                    context.feed.buffer.window(i, n, captured) for i, n in BACKGROUND_WINDOWS
                ),
            )
            flash_bytes = (
                len(
                    json.dumps(
                        context.model.model._payload(request),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode()
                )
                + 1024
            )
            report["joint_input_bound_usd"] = str(
                joint_bound(
                    flash_bytes,
                    settings.price.input_usd_per_million,
                    settings.price.output_usd_per_million,
                    policy.price.input_usd_per_million,
                    policy.price.output_usd_per_million,
                )
            )
            if not args.execute:
                report["free_preflight_ready"] = True
                return
            account = await svc.backend.account(run_policy.scope, clock.utcnow())
            current = await controls.current()
            await svc.start(
                account_ref=run_policy.scope.account_ref,
                expected_revision=account.revision,
                style_revision=session.style_revision,
                trader_revision=current.trader_revision,
            )
            print(json.dumps({"phase": "one_background_dispatch"}), flush=True)
            # Warm the very same inference pool with a free GET before reserving fees.
            response = await public_get(client._client, "https://openrouter.ai/api/v1/models")
            response.raise_for_status()
            await context.prepare(session, enabled=True)
            if context._job is None:
                raise ValueError("background_not_scheduled")
            await asyncio.wait_for(asyncio.shield(context._job), 62)
            report["background_failure"] = context.failure
            if context.result is None:
                raise ValueError("background_model_failed")
            report["background_result"] = context.result.model_dump(mode="json")
            if not context.status(session)["ready"]:
                raise ValueError("context_became_stale_after_background")
            print(json.dumps({"phase": "one_jev_dispatch"}), flush=True)
            started = perf_counter()
            cycle = await svc.step()
            report["cycle_elapsed_ms"] = round((perf_counter() - started) * 1000)
            report["cycle"] = cycle.model_dump(mode="json") if cycle else None
            report["runtime_failure"] = svc.last_failure
            page = await svc.archive.page(run_policy.scope.account_ref)
            report["trade_archive"] = page.model_dump(mode="json")
            report["accepted"] = bool(
                cycle
                and cycle.status in {"wait", "filled", "rejected"}
                and cycle.model_response
                and cycle.usage.billing_status == "confirmed"
            )
        finally:
            svc.ready = False
            await svc.recover()
            await svc.store.release_owner()
            report["calls"] = dict(client.calls)
            try:
                report["wallet_after"] = (
                    await svc.backend.account(svc.scope(session), clock.utcnow())
                ).model_dump(mode="json")
            except LookupError:
                report["wallet_after"] = None
            report["budget_after"] = (
                await core.budget_balance(
                    clock.utcnow(), daily_limit_usd=policy.trial_total_usd, cumulative=True
                )
            ).model_dump(mode="json")
            after = preservation_snapshot()
            report["original_rows_preserved"] = preserved(before, after)
            report["before_row_hashes"], report["after_row_hashes"] = before, after


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--proxy", default=trial.PROXY)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive marker prevents replay after an interrupted or ambiguous request.
    with args.report.open("x", encoding="utf-8") as output:
        output.write('{"state":"started"}\n')
    report = {"execute": args.execute, "symbol": args.symbol, "exchange_orders": 0}
    try:
        asyncio.run(run(args, report))
    except Exception as error:
        report["failure_type"] = type(error).__name__
        trace, frames = error.__traceback__, []
        while trace is not None:
            frames.append(
                {
                    "file": Path(trace.tb_frame.f_code.co_filename).name,
                    "function": trace.tb_frame.f_code.co_name,
                    "line": trace.tb_lineno,
                }
            )
            trace = trace.tb_next
        report["failure_frames"] = frames
        if isinstance(error, ValidationError):
            report["validation_errors"] = [
                {"location": e["loc"], "type": e["type"]}
                for e in error.errors(include_input=False, include_context=False)
            ]
        if type(error) is ValueError and str(error).replace("_", "").isalnum():
            report["failure_code"] = str(error)
    finally:
        args.report.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(
        json.dumps(
            {
                key: report.get(key)
                for key in (
                    "accepted",
                    "free_preflight_ready",
                    "calls",
                    "budget_after",
                    "original_rows_preserved",
                    "failure_type",
                    "failure_code",
                )
            }
            | {"report": str(args.report)}
        ),
        flush=True,
    )
    if not report.get("accepted" if args.execute else "free_preflight_ready"):
        raise SystemExit(2)


if __name__ == "__main__":
    main()

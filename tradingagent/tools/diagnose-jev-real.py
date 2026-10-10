"""One newly authorized diagnostic decision; no wallet start or order execution.

Old Paper policies and unknown reservations remain intact. All inference fees use
the existing shared ledger. Default is free preflight; --execute consumes this
independent authorization exactly once by creating its config exclusively.
"""
# ruff: noqa: E402 -- standalone workspace entry point.

import argparse
import asyncio
import importlib.util
import json
import sqlite3
import sys
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("jev_trial", ROOT / "tools/run-jev-futures-trial.py")
trial_tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(trial_tool)

from agent_platform.adapters.binance_direct.futures_market import FuturesMarketClient
from agent_platform.adapters.openrouter.jev import OpenRouterDecisionModel
from agent_platform.adapters.openrouter.transport import _contains_credential
from agent_platform.adapters.sqlite import open_store
from agent_platform.application.decision_models import BudgetedDecisionModel
from agent_platform.application.futures_trading import FuturesTradingService
from agent_platform.bootstrap_paper import TrialDecisionModel
from agent_platform.config import RuntimeConfig
from agent_platform.domain.agent_controls import AgentControlState
from agent_platform.domain.costs import BudgetReservation
from agent_platform.domain.futures_paper import FuturesPaperState
from agent_platform.domain.model_modules import JevModuleSettings
from agent_platform.domain.paper_trials import PaperTrialPolicy
from agent_platform.domain.routing import ModelPrice
from agent_platform.domain.sessions import AgentSession
from agent_platform.domain.trading_execution import TradingAccountSnapshot
from agent_platform.domain.trading_runtime import TradingCycle, TradingRun
from agent_platform.ports.model import ModelCallFailed
from agent_platform.runtime.clock import SystemClock

WALLET_DATABASE = ROOT / "data/jev-futures-real-20261008.sqlite3"


class ResponseDiagnosticClient(trial_tool.DiagnosticOpenRouterClient):
    async def post(self, *args, **kwargs):
        value = await super().post(*args, **kwargs)
        # Transport has already bounded JSON and excluded credential echoes.
        # Keep the real typed wire response if downstream validation fails.
        self.report["provider_response"] = json.loads(
            json.dumps(
                {
                    key: value[key]
                    for key in ("id", "model", "provider", "answers", "usage")
                    if key in value
                },
                default=str,
            )
        )
        return value


def read_inputs():
    with sqlite3.connect(WALLET_DATABASE.as_uri() + "?mode=ro", uri=True) as db:
        session = AgentSession.model_validate_json(
            db.execute("SELECT body FROM sessions WHERE status<>'closed'").fetchone()[0]
        )
        wallet = FuturesPaperState.model_validate_json(
            db.execute("SELECT body FROM futures_paper_wallets").fetchone()[0]
        )
        run = TradingRun.model_validate_json(
            db.execute("SELECT body FROM futures_trading_runs").fetchone()[0]
        )
        controls = json.loads(
            db.execute("SELECT body FROM domain_states WHERE key='agent-controls'").fetchone()[0]
        )
        controls = AgentControlState.model_validate_json(json.dumps(controls["state"]))
    with sqlite3.connect(trial_tool.SHARED_BUDGET.as_uri() + "?mode=ro", uri=True) as db:
        original = AgentSession.model_validate_json(
            db.execute("SELECT body FROM sessions WHERE status<>'closed'").fetchone()[0]
        )
    if (
        wallet.status != "paused"
        or wallet.quantity != 0
        or wallet.session_id != session.session_id
        or run.scope.account_ref != wallet.account_ref
        or session.style != original.style
        or session.analysis_target != original.analysis_target
    ):
        raise ValueError("diagnostic_requires_same_selection_and_paused_flat_wallet")
    return session, wallet, run, controls, original


def exposure():
    seen, spent, held = set(), Decimal(0), Decimal(0)
    for path in trial_tool.BUDGET_DATABASES:
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
            if db.execute("SELECT billing_frozen FROM budget_settings").fetchone()[0]:
                raise ValueError("billing_is_frozen")
            for (body,) in db.execute("SELECT body FROM budget_requests"):
                r = BudgetReservation.model_validate_json(body)
                if r.request.request_id not in seen:
                    spent += r.actual_cost_usd or Decimal(0)
                    held += r.held_cost_usd
                    seen.add(r.request.request_id)
    if spent + held >= 1:
        raise ValueError("cumulative_budget_exhausted")
    return spent, held


def error_observer(report, key):
    async def observe(response):
        report["provider_http_status"] = response.status_code
        if response.status_code == 200:
            return
        content = bytearray()
        async for chunk in response.aiter_raw():
            if len(content) + len(chunk) > 16384:
                report["error_diagnostic_truncated"] = True
                return
            content.extend(chunk)
        try:
            value = json.loads(content)
            if _contains_credential(value, key):
                report["error_diagnostic_redacted"] = True
                return
            error = value.get("error", {})
            if type(error) is dict:
                if type(error.get("code")) is int:
                    report["provider_error_code"] = error["code"]
                if type(error.get("message")) is str:
                    report["provider_error_message"] = error["message"][:1000]
        except (ValueError, TypeError, UnicodeError, AttributeError):
            report["error_diagnostic_unreadable"] = True

    return observe


async def run(args):
    if args.execute and (args.config.exists() or args.report.exists()):
        raise ValueError("existing_diagnostic_cannot_be_replayed")
    RuntimeConfig(openrouter_proxy=args.openrouter_proxy)
    clock = SystemClock()
    credentials = trial_tool.read_key()
    session, wallet, run_policy, controls, original = read_inputs()
    spent, held = exposure()
    now = clock.utcnow()
    price = ModelPrice(
        version="jev-verified-20261008-diagnostic",
        input_usd_per_million="0.042",
        output_usd_per_million="0",
        verified_at=now,
        valid_until=now + timedelta(minutes=30),
    )
    grant = PaperTrialPolicy(
        trial_total_usd="1",
        single_call_usd="0.02",
        issued_at=now,
        expires_at=price.valid_until,
        price=price,
    )
    report = {
        "started_at": now.isoformat(),
        "scope": "one_decision_diagnostic_only",
        "user_authorization": (
            "2026-10-08 requested VPN-proxy diagnostic, original budget retained"
            if args.openrouter_proxy
            else "2026-10-08 confirmed one fresh diagnostic, original budget retained"
        ),
        "openrouter_proxy": args.openrouter_proxy,
        "policy": grant.model_dump(mode="json"),
        "prior_actual_usd": str(spent),
        "prior_held_usd": str(held),
        "model_dispatch_attempts": 0,
        "exchange_orders": 0,
        "wallet_started": False,
    }
    async with FuturesMarketClient(clock, proxy_url=trial_tool.PROXY) as market:
        # Reuse the public connection warmed by history; snapshot still performs
        # its normal first-reception and five-second freshness guards.
        history = await market._public.history(session.analysis_target)
        snapshot = await market.snapshot(session.analysis_target.symbol)
    captured = clock.utcnow()
    account = TradingAccountSnapshot(
        scope=run_policy.scope,
        revision=wallet.revision,
        status=wallet.status,
        free_usdt=wallet.free_usdt,
        margin_usdt=wallet.margin_usdt,
        quantity=wallet.quantity,
        side=wallet.side,
        entry_notional=wallet.entry_notional,
        realized_pnl_usdt=wallet.realized_pnl_usdt,
        funding_usdt=wallet.funding_usdt,
        fees_usdt=wallet.fees_usdt,
        quote=snapshot.quote,
        equity_usdt=wallet.free_usdt,
        unrealized_pnl_usdt="0",
        captured_at=captured,
    )
    cycle = TradingCycle(
        request_id=uuid4().hex,
        scope=run_policy.scope,
        style_revision=session.style_revision,
        trader_revision=controls.trader_revision,
        account_revision=wallet.revision,
        created_at=captured,
        quote=snapshot.quote,
    )
    request = FuturesTradingService._request(
        SimpleNamespace(price_version=price.version),
        cycle,
        run_policy,
        session,
        account,
        snapshot,
        history,
    )
    core = await open_store(trial_tool.SHARED_BUDGET)
    balance = await core.budget_balance(captured, daily_limit_usd="1")
    # Today's shared exposure is already counted by the store. Subtract other
    # days/databases as well, so a new diagnostic never resets the global 1 USD cap.
    limit = Decimal(1) - spent - held + balance.spent_usd + balance.reserved_usd
    if not 0 < limit <= 1 or balance.hourly_call_count >= 60:
        raise ValueError("diagnostic_budget_unavailable")
    async with httpx.AsyncClient(
        proxy=args.openrouter_proxy,
        trust_env=False,
        follow_redirects=False,
        event_hooks={
            "request": [trial_tool.provider_request_observer(report)],
            "response": [error_observer(report, credentials.api_key.get_secret_value())],
        },
    ) as http:
        client = ResponseDiagnosticClient(
            clock,
            credentials=credentials,
            enabled=True,
            client=http,
            report=report,
        )
        budgeted = BudgetedDecisionModel(
            port=OpenRouterDecisionModel(client, clock),
            budgets=core,
            clock=clock,
            price=price,
            daily_limit_usd=limit,
            max_single_cost_usd=grant.single_call_usd,
            hourly_call_limit=balance.hourly_call_count + 1,
            settings=JevModuleSettings(enabled=True),
        )
        model = trial_tool.SingleDispatch(TrialDecisionModel(budgeted, grant, clock))
        quote = budgeted.quote(request)
        report["cost_upper_bound_usd"] = str(quote.estimated_cost_usd)
        report["request_id"] = request.request_id
        report["history"] = {"count": len(history.candles), "hash": history.content_hash}
        report["original_session_id"] = original.session_id
        if not args.execute:
            report["preflight_passed"] = True
            return report
        # This is a new single-decision authorization, not a rewrite/extension of
        # the expired Paper-run policy. Never starts a backend or its supervisor.
        with args.config.open("x", encoding="utf-8") as output:
            output.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        report["request"] = request.model_dump(mode="json")
        try:
            read_inputs()  # Confirm owned input identity again before reservation.
            response = await model.decide(request)
            report["response"] = response.model_dump(mode="json")
            report["accepted"] = response.usage.billing_status == "confirmed"
        except ModelCallFailed as error:
            report["failure"] = error.reason
            report["usage"] = error.usage.model_dump(mode="json") if error.usage else None
            report["accepted"] = False
        finally:
            report["model_dispatch_attempts"] = model.calls
            report["budget"] = (
                await core.budget_balance(clock.utcnow(), daily_limit_usd=limit)
            ).model_dump(mode="json")
            current = read_inputs()
            report["original_session_unchanged"] = current[4] == original
            report["wallet_unchanged"] = current[1] == wallet
            report["finished_at"] = clock.utcnow().isoformat()
            with args.report.open("x", encoding="utf-8") as output:
                output.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--openrouter-proxy")
    parser.add_argument(
        "--config", type=Path, default=ROOT / "data/jev-diagnostic-20261008.local.json"
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=ROOT / "output/verification/jev-real-diagnostic-20261008.json",
    )
    args = parser.parse_args()
    try:
        report = asyncio.run(run(args))
        print(
            json.dumps(
                {k: v for k, v in report.items() if k not in {"request", "policy"}},
                ensure_ascii=False,
            )
        )
        if args.execute and not report.get("accepted"):
            raise SystemExit(2)
    except Exception as error:
        print(json.dumps({"diagnostic_failed": type(error).__name__}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()

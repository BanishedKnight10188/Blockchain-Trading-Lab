"""Bounded real-model / virtual-funds verification; defaults to free preflight.

Stop this task's 8776 service before using the same wallet here. The original
runtime owner lock prevents two owners. No new wallet, budget or grant is created.
"""

# ruff: noqa: E402 -- workspace standalone tool.
import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
import sqlite3
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location(
    "jev_existing_trial", ROOT / "tools/run-jev-futures-trial.py"
)
existing = importlib.util.module_from_spec(spec)
spec.loader.exec_module(existing)

from agent_platform.application.prediction_verification import BoundedDecisionModel
from agent_platform.bootstrap import build_application_services
from agent_platform.config import RuntimeConfig
from agent_platform.domain.costs import BudgetReservation
from agent_platform.ports.futures_market import FuturesMarketUnavailable

WALLET = ROOT / "data/jev-futures-real-20261008.sqlite3"
CONFIG = ROOT / "data/jev-paper-continuous.local.json"
BUDGET = existing.SHARED_BUDGET


def ledger_snapshot():
    with sqlite3.connect(BUDGET.as_uri() + "?mode=ro", uri=True) as db:
        rows = db.execute("SELECT body FROM budget_requests").fetchall()
    entries = [BudgetReservation.model_validate_json(row[0]) for row in rows]
    return {
        "count": len(entries),
        "spent_usd": str(sum((e.actual_cost_usd or Decimal(0) for e in entries), Decimal(0))),
        "held_usd": str(sum((e.held_cost_usd for e in entries), Decimal(0))),
        "hashes": {
            e.request.request_id: hashlib.sha256(row[0].encode()).hexdigest()
            for e, row in zip(entries, rows, strict=True)
        },
    }


def backup(source, target):
    if target.exists():
        raise ValueError("verification_backup_already_exists")
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as db:
        with sqlite3.connect(target) as dest:
            db.backup(dest)


async def run(args, report):
    # Read-only preconditions before acquiring ownership or preparing any call.
    with sqlite3.connect(WALLET.as_uri() + "?mode=ro", uri=True) as db:
        wallets = [
            json.loads(row[0]) for row in db.execute("SELECT body FROM futures_paper_wallets")
        ]
        if len(wallets) != 1 or wallets[0]["status"] != "paused":
            raise ValueError("pause_existing_wallet_before_verification")
        prior_ids = {row[0] for row in db.execute("SELECT request_id FROM futures_trading_cycles")}
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    out = ROOT / "output/verification"
    out.mkdir(parents=True, exist_ok=True)
    report["before_fees"] = ledger_snapshot()
    report["preserved_files"] = {
        str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (CONFIG, ROOT / "readme.md")
    }
    for source in (WALLET, BUDGET):
        backup(source, out / (f"before-paper-verification-{stamp}-" + source.name))
    credentials = existing.read_key()
    os.environ["OPENROUTER_API_KEY"] = credentials.api_key.get_secret_value()
    config = RuntimeConfig(
        paper=True,
        live_public=True,
        market_archive=False,
        paper_model_config=CONFIG,
        paper_read_only=not args.execute,
        futures_proxy=existing.PROXY,
        openrouter_proxy=existing.PROXY,
        model_budget_database=BUDGET,
        operation={"mode": "auto", "execution_environment": "paper"},
        model_modules={"jev_trader": {"enabled": True}},
    )
    async with build_application_services(WALLET, config) as services:
        svc = services.futures_trading
        session = await svc.sessions.active()
        if session is None or session.analysis_target.market != "usdt_perpetual":
            raise ValueError("existing_futures_session_missing")
        scope = svc.scope(session)
        run_policy = await svc.store.run(scope.account_ref)
        if run_policy is None or run_policy.policy.decision_mode != "parameterized":
            raise ValueError("parameterized_wallet_required")
        report["before_wallet"] = (await svc.backend.account(scope, svc.clock.utcnow())).model_dump(
            mode="json"
        )
        report["cadence"] = svc.cadence.model_dump(mode="json")
        report["budget"] = await svc.model.budget_status()
        await asyncio.wait_for(svc._history_for(session), 30)
        for attempt in range(15):
            try:
                async with svc.gate:
                    snapshot = await svc.maintenance.maintain(scope, run_policy.created_at)
                report["preflight_quote"] = snapshot.quote.model_dump(mode="json")
                break
            except Exception:
                if attempt == 14:
                    raise ValueError("free_market_preflight_failed") from None
                await asyncio.sleep(1)
        # Full archive validation precedes any new paid call.
        report["archive_before"] = (await svc.archive.page(scope.account_ref)).total
        bounded = BoundedDecisionModel(svc.model, max_calls=args.max_calls)
        svc.model = bounded
        try:
            if args.execute:
                controls = await svc.controls.current()
                # Retry free quote/start preconditions only. Inference is never
                # retried and every actual call still consumes its bounded slot.
                for attempt in range(5):
                    account = await svc.backend.account(scope, svc.clock.utcnow())
                    try:
                        await svc.start(
                            account_ref=scope.account_ref,
                            expected_revision=account.revision,
                            style_revision=session.style_revision,
                            trader_revision=controls.trader_revision,
                        )
                        break
                    except FuturesMarketUnavailable:
                        if attempt == 4:
                            raise
                        await asyncio.sleep(1)
                deadline = asyncio.get_running_loop().time() + args.seconds
                while asyncio.get_running_loop().time() < deadline:
                    await asyncio.sleep(0.1)
                    cycles = [
                        c
                        for c in await svc.store.recent(scope.account_ref)
                        if c.request_id not in prior_ids
                    ]
                    account = await svc.backend.account(scope, svc.clock.utcnow())
                    if account.status != "running" or not bounded.enabled:
                        report["stop_reason"] = "runtime_paused"
                        break
                    if (
                        bounded.limit_reached
                        and bounded.in_flight == 0
                        and all(c.status != "pending" for c in cycles)
                    ):
                        report["stop_reason"] = "request_limit_completed"
                        break
                else:
                    report["stop_reason"] = "duration_completed"
        finally:
            # Stop the owned runtime, wait for cancellation/fee recording, then
            # read the final account. Context exit is safe to call stop again.
            bounded.set_enabled(False)
            await services.runtime.stop()
            report["dispatches"] = bounded.observations
            report["actual_dispatches"] = bounded.calls
            report["runtime_metrics"] = dict(svc.runtime_metrics)
            report["prediction_metrics"] = dict(svc.prediction_metrics)
            report["market_status"] = getattr(svc.maintenance.market, "public_status", None)
            report["worker_failure"] = svc.last_failure
            report["maintenance_failures"] = dict(svc.maintenance.failures)
            report["after_wallet"] = (
                await svc.backend.account(scope, svc.clock.utcnow())
            ).model_dump(mode="json")
            cycles = [
                c
                for c in await svc.store.recent(scope.account_ref)
                if c.request_id not in prior_ids
            ]
            report["cycles"] = [c.model_dump(mode="json") for c in reversed(cycles)]
            report["archive_after"] = (await svc.archive.page(scope.account_ref)).total
            output = await svc.archive.export(scope.account_ref, format="jsonl")
            try:
                archive_path = out / f"jev-paper-archive-{stamp}.jsonl"
                with archive_path.open("xb") as dest:
                    while chunk := output.read(65536):
                        dest.write(chunk)
                report["archive_export"] = str(archive_path)
            finally:
                output.close()
    after = ledger_snapshot()
    report["after_fees"] = after
    report["old_fee_entries_preserved"] = all(
        after["hashes"].get(k) == v for k, v in report["before_fees"]["hashes"].items()
    )
    report["preserved_files_unchanged"] = all(
        hashlib.sha256((ROOT / k).read_bytes()).hexdigest() == v
        for k, v in report["preserved_files"].items()
    )
    starts = [d["started_monotonic"] for d in bounded.observations]
    report["dispatch_intervals_ms"] = [
        round((b - a) * 1000, 1) for a, b in zip(starts, starts[1:], strict=False)
    ]
    report["new_confirmed_cost_usd"] = str(
        Decimal(after["spent_usd"]) - Decimal(report["before_fees"]["spent_usd"])
    )
    report["exchange_orders"] = 0
    assert bounded.calls <= args.max_calls
    assert report["after_wallet"]["status"] in ("paused", "liquidated")
    assert report["old_fee_entries_preserved"] and report["preserved_files_unchanged"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--execute", action="store_true", help="Consume the existing paid-call authorization"
    )
    parser.add_argument("--max-calls", type=int, default=12)
    parser.add_argument("--seconds", type=int, default=20)
    args = parser.parse_args()
    if not 1 <= args.max_calls <= 30 or not 1 <= args.seconds <= 120:
        parser.error("max-calls must be 1..30; seconds must be 1..120")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    path = ROOT / "output/verification" / f"jev-paper-verification-{stamp}.json"
    report = {
        "started_at": datetime.now(UTC).isoformat(),
        "mode": "real_bounded" if args.execute else "free_preflight",
        "max_calls": args.max_calls,
        "max_seconds": args.seconds,
        "exchange_orders": 0,
    }
    code = 0
    try:
        asyncio.run(run(args, report))
    except BaseException as error:
        report["failure_type"] = type(error).__name__
        # Fixed, owned setup reason codes only; never print a provider exception.
        if isinstance(error, ValueError) and str(error) in {
            "pause_existing_wallet_before_verification",
            "existing_futures_session_missing",
            "parameterized_wallet_required",
            "free_market_preflight_failed",
        }:
            report["failure_code"] = str(error)
        code = 1
    report["completed_at"] = datetime.now(UTC).isoformat()
    if "before_fees" in report:
        # Record exposure even on setup/runtime failure after entering a batch.
        report["after_fees"] = ledger_snapshot()
        report["new_confirmed_cost_usd"] = str(
            Decimal(report["after_fees"]["spent_usd"]) - Decimal(report["before_fees"]["spent_usd"])
        )
        report["old_fee_entries_preserved"] = all(
            report["after_fees"]["hashes"].get(k) == v
            for k, v in report["before_fees"]["hashes"].items()
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as output:
        json.dump(report, output, ensure_ascii=False, indent=2)
        output.write("\n")
    print(
        json.dumps(
            {
                "report": str(path),
                "actual_dispatches": report.get("actual_dispatches", 0),
                "new_confirmed_cost_usd": report.get("new_confirmed_cost_usd"),
                "failure_type": report.get("failure_type"),
                "wallet_status": report.get("after_wallet", {}).get("status"),
            }
        )
    )
    raise SystemExit(code)


if __name__ == "__main__":
    main()

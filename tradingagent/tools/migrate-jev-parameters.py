"""Audited offline upgrade of the existing flat paused trial, with no model calls."""

# ruff: noqa: E402
import argparse
import asyncio
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent_platform.adapters.sqlite.futures_parameters import SqliteFuturesParameterMigration
from agent_platform.adapters.sqlite.trading_runtime import SqliteTradingRuntimeStore
from agent_platform.domain.futures_paper import FuturesPaperState
from agent_platform.domain.sessions import AgentSession
from agent_platform.runtime.clock import SystemClock


async def run(args):
    database = ROOT / "data/jev-futures-real-20261008.sqlite3"
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
        session = AgentSession.model_validate_json(
            db.execute("SELECT body FROM sessions WHERE status<>'closed'").fetchone()[0]
        )
        wallet = FuturesPaperState.model_validate_json(
            db.execute("SELECT body FROM futures_paper_wallets").fetchone()[0]
        )
    if wallet.status != "paused" or wallet.quantity or session.session_id != wallet.session_id:
        raise ValueError("migration_requires_paused_flat_active_wallet")
    owner = SqliteTradingRuntimeStore(database)
    await owner.acquire_owner()
    try:
        report = {
            "execute": args.execute,
            "account_ref": wallet.account_ref,
            "prior_free_usdt": str(wallet.free_usdt),
            "prior_revision": wallet.revision,
            "leverage_choices": [1, 2, 5, 10],
            "max_leverage": 10,
            "model_calls": 0,
            "exchange_orders": 0,
        }
        if args.execute:
            with args.output.open("x", encoding="utf-8") as output:
                updated = await SqliteFuturesParameterMigration(database).enable(
                    wallet.account_ref,
                    wallet.revision,
                    SystemClock().utcnow(),
                    style_revision=session.style_revision,
                    leverage_choices=(1, 2, 5, 10),
                )
                assert (
                    updated.free_usdt == wallet.free_usdt
                    and updated.margin_usdt == wallet.margin_usdt
                    and updated.fees_usdt == wallet.fees_usdt
                    and updated.funding_usdt == wallet.funding_usdt
                )
                report |= {
                    "new_revision": updated.revision,
                    "mode": "parameterized",
                    "free_usdt": str(updated.free_usdt),
                    "status": updated.status,
                }
                json.dump(report, output, ensure_ascii=False, indent=2)
        print(json.dumps(report, ensure_ascii=False))
    finally:
        await owner.release_owner()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.execute and args.output is None:
        parser.error("--execute requires --output")
    asyncio.run(run(args))

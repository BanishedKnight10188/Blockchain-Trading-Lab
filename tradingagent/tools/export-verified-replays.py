"""Preserve verified offline fill evidence separately from real-model trials."""

# ruff: noqa: E402 -- workspace standalone tool.
import asyncio
import json
import sqlite3
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent_platform.adapters.sqlite.trade_archive import SqliteTradeArchiveStore


async def main():
    out = ROOT / "output/verification"
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    sources = sorted(
        (out / "tmp-archive-final-1").glob("test_each_fill_has_atomic_arch*/futures.sqlite3")
    )
    sources += sorted(
        (out / "tmp-archive-core-2").glob("test_jev_can_reduce_or_close_p*/futures.sqlite3")
    )
    if len(sources) != 6:
        raise ValueError("verified_replay_evidence_missing")
    report = {
        "source": "offline_scripted_acceptance",
        "model_calls": 0,
        "exchange_orders": 0,
        "cases": [],
    }
    for i, source in enumerate(sources):
        target = out / f"verified-replay-{stamp}-{i}.sqlite3"
        with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as db:
            wallet = json.loads(db.execute("SELECT body FROM futures_paper_wallets").fetchone()[0])
            with sqlite3.connect(target) as dest:
                db.backup(dest)
        archive = SqliteTradeArchiveStore(target)
        page = await archive.page(wallet["account_ref"])
        assert page.total == (4 if i < 2 else 2)
        evidence = [e.record.execution_command.decision_evidence for e in page.entries]
        assert all(e is not None and e.decision_source == "offline_mock" for e in evidence)
        assert all(e.record.before_state is not None for e in page.entries)
        assert Decimal(wallet["free_usdt"]) + Decimal(wallet["margin_usdt"]) == (
            Decimal(wallet["settings"]["initial_usdt"])
            + Decimal(wallet["realized_pnl_usdt"])
            + Decimal(wallet["funding_usdt"])
            - Decimal(wallet["fees_usdt"])
        )
        if i < 2:
            assert [e.plan.intent for e in evidence] == [
                "open_long" if i == 0 else "open_short",
                "add_long" if i == 0 else "add_short",
                "reduce",
                "close",
            ]
            assert (
                Decimal(wallet["free_usdt"]) == Decimal("997.6")
                and Decimal(wallet["quantity"]) == 0
            )
        exports = {}
        for format in ("jsonl", "csv"):
            stream = await archive.export(wallet["account_ref"], format=format)
            file = target.with_suffix("." + format)
            try:
                with file.open("xb") as dest:
                    while chunk := stream.read(65536):
                        dest.write(chunk)
            finally:
                stream.close()
            exports[format] = str(file)
        report["cases"].append(
            {
                "test_evidence": str(source),
                "database": str(target),
                "fills": page.total,
                "intents": [e.plan.intent for e in evidence],
                "quantity": wallet["quantity"],
                "free_usdt": wallet["free_usdt"],
                "realized_pnl_usdt": wallet["realized_pnl_usdt"],
                "fees_usdt": wallet["fees_usdt"],
                "archive_integrity": "verified",
                "exports": exports,
            }
        )
    file = out / f"verified-replay-{stamp}.json"
    with file.open("x", encoding="utf-8") as output:
        json.dump(report, output, ensure_ascii=False, indent=2)
        output.write("\n")
    print(
        json.dumps(
            {
                "report": str(file),
                "cases": len(report["cases"]),
                "fills": sum(c["fills"] for c in report["cases"]),
                "model_calls": 0,
                "exchange_orders": 0,
            }
        )
    )


if __name__ == "__main__":
    asyncio.run(main())

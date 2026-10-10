"""Offline fee timing on online SQLite backups; never writes the original ledger."""

# ruff: noqa: E402 -- standalone workspace tool.
import asyncio
import hashlib
import json
import sqlite3
import sys
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from time import perf_counter_ns
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent_platform.adapters.sqlite.budgets import SqliteBudgetStore
from agent_platform.application.settlement import settle_owned
from agent_platform.domain.costs import BudgetRequest, ModelUsage

SOURCE = ROOT / "data/futures-core-preview-20261008.sqlite3"


def digest():
    with closing(sqlite3.connect(SOURCE.as_uri() + "?mode=ro", uri=True)) as db:
        rows = db.execute("SELECT * FROM budget_requests ORDER BY 1").fetchall()
        return len(rows), hashlib.sha256(json.dumps(rows).encode()).hexdigest()


def percentiles(rows, key):
    values = sorted(row[key] for row in rows)
    return {
        "median_ms": round(median(values), 3),
        "p95_ms": round(values[(len(values) * 95 + 99) // 100 - 1], 3),
        "max_ms": round(max(values), 3),
    }


async def measure(folder, parallelism):
    copy = folder / f"fees-{parallelism}-writers.sqlite3"
    with closing(sqlite3.connect(SOURCE.as_uri() + "?mode=ro", uri=True)) as source:
        with closing(sqlite3.connect(copy)) as target:
            source.backup(target)
    stores = [SqliteBudgetStore(copy) for _ in range(parallelism)]
    for store in stores:
        await store.initialize()
    samples = []

    async def one(store, retain):
        now = datetime.now(UTC)
        request = BudgetRequest(
            request_id="offline-fee-timing-" + uuid4().hex,
            route_id="offline-jev-timing",
            purpose="advisory",
            price_version="offline-fee-probe",
            estimated_cost_usd="0.0009",
            daily_limit_usd="0.1",
            hourly_call_limit=3600,
            requested_at=now,
        )
        began = perf_counter_ns()
        reservation = await store.reserve(request, require_new=True)
        reserved = perf_counter_ns()
        usage = ModelUsage(
            request_id=request.request_id,
            route_id=request.route_id,
            model_version="offline-no-provider",
            input_tokens=0,
            output_tokens=0,
            estimated_cost_usd=request.estimated_cost_usd,
            actual_cost_usd="0",
            billing_status="confirmed",
            recorded_at=datetime.now(UTC),
        )
        settling = perf_counter_ns()
        await settle_owned(store, reservation.reservation_id, usage)
        ended = perf_counter_ns()
        if retain:
            samples.append(
                {
                    "reserve_ms": (reserved - began) / 1e6,
                    "settle_ms": (ended - settling) / 1e6,
                    "total_ms": (ended - began) / 1e6,
                }
            )

    for index in range(35):
        await asyncio.gather(*(one(store, index >= 5) for store in stores))
    return {
        "parallelism": parallelism,
        "samples": len(samples),
        "reserve": percentiles(samples, "reserve_ms"),
        "settle": percentiles(samples, "settle_ms"),
        "total": percentiles(samples, "total_ms"),
        "raw": samples,
    }


async def main():
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    folder = ROOT / "output/verification" / ("local-fee-timing-" + stamp)
    folder.mkdir()
    before = digest()
    results = [await measure(folder, n) for n in (1, 3)]
    after = digest()
    report = {
        "measured_at": datetime.now(UTC).isoformat(),
        "original_fee_rows": before[0],
        "original_ledger_unchanged": before == after,
        "model_calls": 0,
        "scope": "offline online-backup, production reserve and cancellation-safe settle",
        "excludes": ["provider", "wallet/cycle archival", "live app contention"],
        "results": results,
    }
    path = folder / "report.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "report": str(path),
                **{k: v for k, v in report.items() if k != "results"},
                "results": [{k: v for k, v in r.items() if k != "raw"} for r in results],
            }
        )
    )
    assert before == after, "original ledger changed during the measurement"


if __name__ == "__main__":
    asyncio.run(main())

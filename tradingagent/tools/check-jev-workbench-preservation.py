"""Read-only snapshots and online backups around a workbench reload, no model calls."""

import argparse
import hashlib
import http.cookiejar
import json
import sqlite3
import urllib.request
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output/verification"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path)
    args = parser.parse_args()
    before = json.loads(args.baseline.read_text("utf-8")) if args.baseline else None
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    report = {
        "measured_at": datetime.now(UTC).isoformat(),
        "immutable": {},
        "wallets": [],
        "operation_counts": {},
    }
    paths = [
        ROOT / "data" / name
        for name in (
            "jev-futures-real-20261008.sqlite3",
            "futures-core-preview-20261008.sqlite3",
            "jev-futures-real-20261008.tasks.sqlite3",
        )
    ] + sorted((ROOT / "data/jev-futures-real-20261008-tasks").glob("*.sqlite3"))
    for path in paths:
        name = str(path.relative_to(ROOT / "data"))
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
            for table in (
                "sessions",
                "domain_states",
                "budget_requests",
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
                if table == "futures_paper_operations":
                    report["operation_counts"][name] = len(rows)
                    if before:
                        original_count = before["operation_counts"][name]
                        assert len(rows) >= original_count, "operation history shrank"
                        for row in rows[original_count:]:
                            assert json.loads(row[-1])["kind"] == "mark", "new non-mark operation"
                        rows = rows[:original_count]
                report["immutable"][name + ":" + table] = hashlib.sha256(
                    json.dumps(rows, ensure_ascii=False).encode()
                ).hexdigest()
                if table == "budget_requests" and name.startswith("futures-core"):
                    report["budget_rows"] = len(rows)
            if db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='futures_paper_wallets'"
            ).fetchone():
                for (body,) in db.execute(
                    "SELECT body FROM futures_paper_wallets ORDER BY account_ref"
                ):
                    wallet = json.loads(body)
                    report["wallets"].append(
                        {
                            key: wallet[key]
                            for key in (
                                "account_ref",
                                "status",
                                "quantity",
                                "free_usdt",
                                "margin_usdt",
                                "fees_usdt",
                                "funding_usdt",
                                "realized_pnl_usdt",
                                "settings",
                            )
                        }
                    )
            if args.baseline is None:
                with closing(
                    sqlite3.connect(OUT / ("workbench-backup-" + stamp + "-" + path.name))
                ) as dest:
                    db.backup(dest)
    for file in ("readme.md", "data/jev-paper-continuous.local.json"):
        report["immutable"][file] = hashlib.sha256((ROOT / file).read_bytes()).hexdigest()
    client = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
    )
    for port in (8775, 8776):
        with client.open(f"http://127.0.0.1:{port}/jev-trader", timeout=15) as response:
            report[f"page_{port}"] = response.status
        with client.open(f"http://127.0.0.1:{port}/api/futures-trading", timeout=15) as response:
            view = json.load(response)
        report[f"active_{port}"] = view["session"]
        if port == 8776:
            report["budget"] = view["model_budget"]
            report["paid_models_enabled"] = view["paid_models_enabled"]
    if args.baseline:
        for key in (
            "immutable",
            "wallets",
            "budget_rows",
            "paid_models_enabled",
            "active_8776",
        ):
            assert before[key] == report[key], "preservation failed: " + key
        previous_budget = json.loads(json.dumps(before["budget"]))
        current_budget = json.loads(json.dumps(report["budget"]))
        previous_count = previous_budget["balance"].pop("hourly_call_count")
        current_count = current_budget["balance"].pop("hourly_call_count")
        # The rolling hour naturally expires old calls during this offline work.
        # Exact request-table hashes above still prove that no new call was added.
        assert current_count <= previous_count, "unexpected new hourly calls"
        assert current_budget == previous_budget, "fees or allowance changed"
        report["rolling_hourly_calls"] = {"before": previous_count, "after": current_count}
        with client.open("http://127.0.0.1:8776/workbench", timeout=15) as response:
            assert b"jev-workspace.js" in response.read()
        with client.open("http://127.0.0.1:8776/api/jev-tasks", timeout=15) as response:
            report["catalog"] = json.load(response)
        for row in report["catalog"]["tasks"]:
            with client.open(
                "http://127.0.0.1:8776/api/jev-tasks/" + row["task_id"], timeout=15
            ) as response:
                view = json.load(response)
            assert not view["paid_models_enabled"]
            with client.open(
                "http://127.0.0.1:8776/api/jev-tasks/" + row["task_id"] + "/archive", timeout=15
            ) as response:
                row["archive"] = json.load(response)["total"]
        report["preserved"] = True
    target = OUT / ("jev-workbench-" + ("after-" if args.baseline else "before-") + stamp + ".json")
    with target.open("x", encoding="utf-8") as output:
        json.dump(report, output, ensure_ascii=False, indent=2)
    print(
        json.dumps(
            {
                "report": str(target),
                "wallets": len(report["wallets"]),
                "fee_rows": report["budget_rows"],
                "preserved": bool(report.get("preserved")),
                "paid": report["paid_models_enabled"],
            }
        )
    )


if __name__ == "__main__":
    main()

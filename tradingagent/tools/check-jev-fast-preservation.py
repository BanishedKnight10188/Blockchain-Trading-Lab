"""Compare original fees, flat wallet and user configuration across a local reload."""

import argparse
import hashlib
import http.cookiejar
import json
import sqlite3
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output/verification"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("before", "after"))
    phase = parser.parse_args().phase
    report = {"measured_at": datetime.now(UTC).isoformat(), "immutable": {}}
    for name in ("jev-futures-real-20261008.sqlite3", "futures-core-preview-20261008.sqlite3"):
        source = ROOT / "data" / name
        with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as db:
            for table in (
                "sessions",
                "domain_states",
                "budget_requests",
                "futures_trading_runs",
                "futures_trading_cycles",
                "futures_policy_migrations",
            ):
                if not db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
                ).fetchone():
                    continue
                rows = db.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
                report["immutable"][name + ":" + table] = hashlib.sha256(
                    json.dumps(rows, ensure_ascii=False).encode()
                ).hexdigest()
            if name.startswith("jev-"):
                wallet = json.loads(
                    db.execute("SELECT body FROM futures_paper_wallets").fetchone()[0]
                )
                report["wallet"] = {
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
            if phase == "before":
                target = OUT / ("before-fast-" + name)
                if not target.exists():
                    with sqlite3.connect(target) as backup:
                        db.backup(backup)
    for file in ("README.md", "data/jev-paper-continuous.local.json"):
        report["immutable"][file] = hashlib.sha256((ROOT / file).read_bytes()).hexdigest()
    client = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
        urllib.request.ProxyHandler({}),
    )
    with client.open("http://127.0.0.1:8776/jev-trader", timeout=15) as response:
        report["page_status"] = response.status
    with client.open("http://127.0.0.1:8776/api/futures-trading", timeout=15) as response:
        view = json.load(response)
    report["model_budget"] = view["model_budget"]
    report["paid_models_enabled"] = view["paid_models_enabled"]
    report["real_orders_enabled"] = view["real_orders_enabled"]
    report["cadence"] = view.get("cadence")
    report["prediction_metrics"] = view.get("prediction_metrics")
    report["market_status"] = view.get("market_status")
    if phase == "after":
        baseline = json.loads((OUT / "jev-fast-before-20261008.json").read_text("utf-8"))
        for key in (
            "immutable",
            "wallet",
            "model_budget",
            "paid_models_enabled",
            "real_orders_enabled",
        ):
            assert report[key] == baseline[key], "state changed: " + key
        assert report["cadence"]["decision_seconds"] == 1
        assert report["prediction_metrics"]["requests_started"] == 0
        assert report["wallet"]["status"] == "paused" and report["wallet"]["quantity"] == "0"
    (OUT / f"jev-fast-{phase}-20261008.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "phase": phase,
                "wallet_status": report["wallet"]["status"],
                "model_enabled": report["paid_models_enabled"],
                "cadence": report["cadence"],
                "preserved": phase == "after",
            }
        )
    )


if __name__ == "__main__":
    main()

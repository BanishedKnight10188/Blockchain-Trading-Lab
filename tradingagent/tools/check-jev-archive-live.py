"""Read-only HTTP archive verification against a preserved real-trial snapshot."""

import argparse
import csv
import hashlib
import http.cookiejar
import io
import json
import sqlite3
import urllib.request
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, required=True)
    args = parser.parse_args()
    baseline = json.loads(args.baseline.read_text("utf-8"))
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    prefix = ROOT / "output/verification" / f"jev-archive-live-{stamp}"
    client = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
        urllib.request.ProxyHandler({}),
    )

    def read(path):
        with client.open("http://127.0.0.1:8776" + path, timeout=15) as response:
            assert response.status == 200
            return response.read()

    page = read("/jev-trader").decode()
    assert "逐笔合约档案" in page and "futures-archive.js" in page
    view = json.loads(read("/api/futures-trading"))
    archive = json.loads(read("/api/futures-trading/archive"))
    files = {}
    exported = {}
    for format in ("jsonl", "csv"):
        content = read("/api/futures-trading/archive/export?format=" + format)
        target = prefix.with_suffix("." + format)
        with target.open("xb") as output:
            output.write(content)
        files[format] = str(target)
        exported[format] = content
    lines = [json.loads(line) for line in exported["jsonl"].splitlines()]
    assert lines[0]["type"] == "manifest" and lines[-1]["type"] == "complete"
    operations = lines[1:-1]
    previous = "0" * 64
    for operation in operations:
        assert operation["type"] == "operation"
        entry = dict(operation["entry"])
        digest = entry.pop("content_hash")
        assert entry["previous_hash"] == previous
        assert hashlib.sha256(canonical(entry).encode()).hexdigest() == digest
        previous = digest
    assert lines[-1]["last_hash"] == previous
    assert lines[-1]["count"] == archive["total"] == len(operations)
    assert len(list(csv.DictReader(io.StringIO(exported["csv"].decode("utf-8-sig"))))) == len(
        operations
    )
    assert not view["paid_models_enabled"] and not view["real_orders_enabled"]
    for key in ("status", "quantity", "free_usdt", "margin_usdt", "fees_usdt"):
        assert view["account"][key] == baseline["after_wallet"][key]
    assert view["session"]["style_strength"] == 92
    assert view["session"]["style_revision"] == 1
    assert view["account"]["status"] == "paused"
    assert view["prediction_metrics"]["requests_started"] == 0
    for name, digest in baseline["preserved_files"].items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest
    budget = ROOT / "data/futures-core-preview-20261008.sqlite3"
    with sqlite3.connect(budget.as_uri() + "?mode=ro", uri=True) as db:
        rows = db.execute("SELECT request_id,body FROM budget_requests").fetchall()
    actual = {row[0]: hashlib.sha256(row[1].encode()).hexdigest() for row in rows}
    assert actual == baseline["after_fees"]["hashes"]
    assert Decimal(view["model_budget"]["remaining_usd"]) >= 0
    with client.open("http://localhost:8775/overview", timeout=15) as response:
        assert response.status == 200
    report = {
        "measured_at": datetime.now(UTC).isoformat(),
        "baseline": str(args.baseline),
        "page_status": 200,
        "original_8775_status": 200,
        "archive": archive,
        "independent_export_hash_check": True,
        "exports": files,
        "account": view["account"],
        "session": view["session"],
        "model_budget": view["model_budget"],
        "market_status": view["market_status"],
        "maintenance_failure": view["maintenance_failure"],
        "paid_models_enabled": view["paid_models_enabled"],
        "real_orders_enabled": view["real_orders_enabled"],
        "all_fee_rows_preserved": True,
        "preserved_files_unchanged": True,
        "model_calls": 0,
        "exchange_orders": 0,
    }
    target = prefix.with_suffix(".json")
    with target.open("x", encoding="utf-8") as output:
        json.dump(report, output, ensure_ascii=False, indent=2)
        output.write("\n")
    print(json.dumps({"report": str(target), "entries": len(operations), "preserved": True}))


if __name__ == "__main__":
    main()

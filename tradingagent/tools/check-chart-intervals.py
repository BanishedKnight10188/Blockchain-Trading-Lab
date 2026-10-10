"""Compare the current preview session before and after public chart changes."""

import argparse
import hashlib
import http.cookiejar
import json
import urllib.request
from pathlib import Path

BASE = "http://localhost:8775"
OUTPUT = Path("output/verification")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("before", "after"))
    phase = parser.parse_args().phase
    client = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
        urllib.request.ProxyHandler({}),
    )
    with client.open(BASE + "/overview", timeout=10) as response:
        response.read()

    def read(path):
        with client.open(BASE + path, timeout=25) as response:
            return json.load(response)

    session = read("/api/session")["session"]
    analysis = read("/api/analysis/current")
    trading = read("/api/futures-trading")
    report = {
        "session": session,
        "initial_record_hash": hashlib.sha256(
            json.dumps(analysis["record"], sort_keys=True).encode()
        ).hexdigest(),
        "paid_model_configured": analysis["paid_model_configured"],
        "wallet_exists": trading["account"] is not None,
    }
    if phase == "after":
        before = json.loads((OUTPUT / "chart-intervals-before-20261008.json").read_text("utf-8"))
        assert report == before, "original session, style, model record or wallet changed"
        report["charts"] = []
        for interval in ("5m", "15m"):
            chart = read(f"/api/analysis/chart?interval={interval}&limit=100")
            history = chart["history"]
            assert chart["session_id"] == session["session_id"]
            assert history["symbol"] == session["analysis_target"]["symbol"]
            assert history["interval"] == interval
            assert history["source"] == "binance_futures_public"
            assert len(history["candles"]) == 100
            report["charts"].append(
                {
                    "interval": interval,
                    "source": history["source"],
                    "count": len(history["candles"]),
                    "captured_at": history["captured_at"],
                }
            )
    (OUTPUT / f"chart-intervals-{phase}-20261008.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()

"""Read-only evidence from the owned local futures preview; never call a model."""

import argparse
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
        with client.open(BASE + path, timeout=15) as response:
            return json.load(response)

    session = read("/api/session")["session"]
    analysis = read("/api/analysis/current")
    trading = read("/api/futures-trading")
    record = analysis.get("record")
    history = record and record.get("history")
    report = {
        "preview": BASE,
        "session_id": session["session_id"],
        "style": session["style"],
        "style_revision": session["style_revision"],
        "target": session["analysis_target"],
        "session_status": session["status"],
        "analysis_status": analysis["reason"],
        "history_source": history and history["source"],
        "candle_count": len(history["candles"]) if history else 0,
        "history_start": history and history["requested_start"],
        "history_end_exclusive": history and history["requested_end"],
        "paid_model_configured": analysis["paid_model_configured"],
        "wallet_exists": trading["account"] is not None,
        "decision_source": trading["decision_source"],
    }
    if phase == "after":
        before = json.loads((OUTPUT / "history-recovery-before-20261008.json").read_text("utf-8"))
        for key in ("session_id", "style", "style_revision", "target", "wallet_exists"):
            assert report[key] == before[key], f"user state changed: {key}"
        assert report["history_source"] == "binance_futures_public"
        assert report["candle_count"] == report["target"]["history_days"] * 24
        assert not report["paid_model_configured"]
        assert report["analysis_status"] == "model_unconfigured"
    (OUTPUT / f"history-recovery-{phase}-20261008.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
